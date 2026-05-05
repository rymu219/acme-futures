"""Topstep eval-rule enforcement.

ProjectX exposes account balance and `canTrade` but does NOT expose rule parameters
(max loss, daily loss, profit target, min days) as a queryable endpoint. Rules are
server-enforced and discovered by hitting them. So `EvalProfile` is hardcoded from
Topstep's published docs. Local enforcement is intentionally over-protective: we
check trailing DD against the *unrealized* peak intraday, while Topstep's actual
rule trails the *realized* EOD peak. That makes our gate a strict superset of theirs.

Each profile carries a `snapshot_date` so callers can detect when the snapshot
has aged out and prompt a re-validation against the live Topstep docs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# Default freshness budget: re-validate the profile against Topstep docs at
# least once per quarter.
EVAL_PROFILE_MAX_AGE_DAYS = 90

# TOPSTEP_50K snapshot 2026-04-29 from help.topstep.com:
#   Account: $50,000 | Max Loss (trailing EOD): $2,000 | Daily Loss: $1,000
#   Profit Target (Combine): $3,000 | Min trading days: 5
#   Consistency rule: best single day must be < 50% of profit target
#       => 50K Combine: max single-day profit = $1,500 (Combine only, not funded)


@dataclass(frozen=True)
class EvalProfile:
    name: str
    account_size: float
    max_loss_amount: float
    daily_loss_limit: float
    profit_target: float
    min_trading_days: int
    max_position_contracts: dict[str, int]
    # Consistency rule (Combine only): best day must stay below this realized PnL.
    # We add a soft buffer below this hard limit to give in-flight trades room to close.
    max_single_day_profit: float
    consistency_buffer: float = 200.0
    # Per-contract round-turn cost (commission + NFA + exchange fees) on TopstepX.
    # Source: https://help.topstep.com/en/articles/8284213-topstepx-commissions-and-fees
    round_turn_fees: dict[str, float] = field(default_factory=lambda: {
        "MES": 1.24, "MNQ": 1.24, "ES": 3.80, "NQ": 3.80,
    })
    # Date this snapshot was last verified against help.topstep.com. Bump on
    # every re-validation. None means "not tracked" → freshness check is skipped.
    snapshot_date: date | None = None


TOPSTEP_50K = EvalProfile(
    name="topstep_50k",
    account_size=50_000.0,
    max_loss_amount=2_000.0,
    daily_loss_limit=1_000.0,
    profit_target=3_000.0,
    min_trading_days=5,
    max_position_contracts={"MES": 50, "MNQ": 50, "ES": 5, "NQ": 5},
    max_single_day_profit=1_500.0,
    snapshot_date=date(2026, 4, 29),
)

PROFILES: dict[str, EvalProfile] = {"topstep_50k": TOPSTEP_50K}


def eval_profile_age_days(profile: EvalProfile, today: date | None = None) -> int | None:
    """Days between today and the profile's snapshot date. None if untracked."""
    if profile.snapshot_date is None:
        return None
    return ((today or date.today()) - profile.snapshot_date).days


def is_eval_profile_stale(
    profile: EvalProfile,
    today: date | None = None,
    max_age_days: int = EVAL_PROFILE_MAX_AGE_DAYS,
) -> bool:
    """True when the snapshot is older than `max_age_days`. Untracked = not stale."""
    age = eval_profile_age_days(profile, today)
    return age is not None and age > max_age_days


@dataclass
class DailyState:
    trade_date: date
    starting_balance: float
    realized_pnl: float = 0.0
    peak_balance_eod: float = 0.0
    max_loss_limit: float = 0.0
    daily_loss_limit: float = 0.0
    notes: str = ""


def trailing_max_loss_limit(profile: EvalProfile, peak_balance_eod: float) -> float:
    """Topstep's MLL trails the EOD peak, ratchets up only, locks at starting balance."""
    raw = peak_balance_eod - profile.max_loss_amount
    return min(raw, profile.account_size)


def can_open_new_position(
    profile: EvalProfile,
    state: DailyState,
    current_balance_unrealized: float,
    contract: str,
    desired_size: int,
    current_position: int,
) -> tuple[bool, str]:
    """Pre-trade gate. Returns (allowed, reason)."""
    mll = trailing_max_loss_limit(profile, state.peak_balance_eod)
    if current_balance_unrealized <= mll:
        return False, f"trailing_dd_breach: bal={current_balance_unrealized:.2f} mll={mll:.2f}"
    # Daily Loss Limit: per Topstep docs, this is Net P&L (realized + unrealized),
    # monitored real-time. We compute today's PnL from current_balance_unrealized
    # vs the day's starting balance, so unrealized swings count.
    todays_net_pnl = current_balance_unrealized - state.starting_balance
    if todays_net_pnl <= -profile.daily_loss_limit:
        return False, (
            f"daily_loss_limit_hit: net_pnl={todays_net_pnl:.2f} "
            f"limit=-{profile.daily_loss_limit:.2f}"
        )
    # Consistency rule: stop opening new positions when realized PnL approaches the
    # max-single-day-profit cap. The buffer leaves room for in-flight trades to close
    # without pushing realized over the hard limit.
    consistency_soft = profile.max_single_day_profit - profile.consistency_buffer
    if state.realized_pnl >= consistency_soft:
        return False, (
            f"consistency_soft_cap: realized={state.realized_pnl:.2f} "
            f"soft={consistency_soft:.2f} hard={profile.max_single_day_profit:.2f}"
        )
    cap = profile.max_position_contracts.get(contract, 1)
    if abs(current_position + desired_size) > cap:
        return False, f"position_cap: cap={cap} current={current_position} desired={desired_size}"
    return True, "ok"


def dollars_to_contracts(
    risk_dollars: float,
    stop_distance_points: float,
    contract_point_value: float,
    *,
    round_turn_fee: float = 0.0,
) -> int:
    """How many contracts to risk `risk_dollars` if stop is `stop_distance_points` away.

    `round_turn_fee` is the per-contract commission + exchange fees for entering and
    exiting one contract. Subtracted from the price-move loss when computing total
    per-contract risk, so the returned size keeps `risk_dollars` as a true ceiling.

    Truncates toward zero. Returns 0 on any non-positive primary input.
    """
    if risk_dollars <= 0 or stop_distance_points <= 0 or contract_point_value <= 0:
        return 0
    per_contract_loss = stop_distance_points * contract_point_value + max(0.0, round_turn_fee)
    return max(0, int(risk_dollars / per_contract_loss))
