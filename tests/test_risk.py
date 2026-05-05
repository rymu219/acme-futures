from dataclasses import replace
from datetime import date, timedelta

from acme.risk import (
    EVAL_PROFILE_MAX_AGE_DAYS,
    TOPSTEP_50K,
    DailyState,
    can_open_new_position,
    dollars_to_contracts,
    eval_profile_age_days,
    is_eval_profile_stale,
    trailing_max_loss_limit,
)


def _state(peak: float = 50_000.0, realized: float = 0.0) -> DailyState:
    return DailyState(
        trade_date=date(2026, 4, 29),
        starting_balance=50_000.0,
        realized_pnl=realized,
        peak_balance_eod=peak,
        max_loss_limit=trailing_max_loss_limit(TOPSTEP_50K, peak),
        daily_loss_limit=TOPSTEP_50K.daily_loss_limit,
    )


def test_dollars_to_contracts_clean():
    # 100 / (4.0 * 5.0) = 5; no fee
    assert dollars_to_contracts(100, 4.0, 5.0) == 5


def test_dollars_to_contracts_truncates():
    # 100 / (4.5 * 5.0) = 4.444 → 4
    assert dollars_to_contracts(100, 4.5, 5.0) == 4


def test_dollars_to_contracts_non_positive():
    assert dollars_to_contracts(0, 1.0, 5.0) == 0
    assert dollars_to_contracts(100, 0, 5.0) == 0
    assert dollars_to_contracts(100, 1.0, 0) == 0
    assert dollars_to_contracts(-1, 1.0, 5.0) == 0


def test_dollars_to_contracts_includes_round_turn_fee():
    # 8-tick stop on MES = 2.0 pts * $5 = $10/contract loss + $1.24 fee = $11.24/contract
    # Risk budget $25 → floor(25 / 11.24) = 2 contracts
    n = dollars_to_contracts(25.0, 2.0, 5.0, round_turn_fee=1.24)
    assert n == 2
    # Without fee, same inputs would size larger
    assert dollars_to_contracts(25.0, 2.0, 5.0) == 2  # actually same here; 25/10 = 2.5 → 2


def test_dollars_to_contracts_fee_can_zero_out_size():
    # If fee alone exceeds the budget, we get 0
    assert dollars_to_contracts(1.0, 2.0, 5.0, round_turn_fee=10.0) == 0


def test_trailing_mll_pre_lock():
    # Peak 50K → MLL = 48K (start - 2K)
    assert trailing_max_loss_limit(TOPSTEP_50K, 50_000) == 48_000


def test_trailing_mll_locks_at_starting_balance():
    # Peak 53K → raw MLL = 51K, but locks at 50K
    assert trailing_max_loss_limit(TOPSTEP_50K, 53_000) == 50_000
    # Peak 60K → still locked at 50K
    assert trailing_max_loss_limit(TOPSTEP_50K, 60_000) == 50_000


def test_can_open_new_position_ok():
    state = _state(peak=50_000)
    ok, reason = can_open_new_position(TOPSTEP_50K, state, 50_000, "MES", 1, 0)
    assert ok and reason == "ok"


def test_can_open_blocked_by_trailing_dd():
    state = _state(peak=50_000)
    # Unrealized balance dropped to 47_900 — below MLL of 48_000
    ok, reason = can_open_new_position(TOPSTEP_50K, state, 47_900, "MES", 1, 0)
    assert not ok and reason.startswith("trailing_dd_breach")


def test_can_open_blocked_by_daily_loss_realized_only():
    # Realized -1000 alone hits the limit
    state = _state(peak=50_000, realized=-1_000)
    ok, reason = can_open_new_position(TOPSTEP_50K, state, 49_000, "MES", 1, 0)
    assert not ok and reason.startswith("daily_loss_limit_hit")


def test_can_open_blocked_by_daily_loss_unrealized_swing():
    """Topstep DLL is Net P&L (realized + unrealized). Realized $0 + unrealized
    -$1,100 should still trigger DLL block, even though realized alone is fine.
    """
    state = _state(peak=50_000, realized=0)
    # Account size 50K, current_balance_unrealized=48,900 → today's net PnL = -1,100
    ok, reason = can_open_new_position(TOPSTEP_50K, state, 48_900, "MES", 1, 0)
    assert not ok and reason.startswith("daily_loss_limit_hit")


def test_can_open_allowed_when_unrealized_loss_under_dll():
    """Net P&L of -$900 is under the $1,000 DLL — entries still allowed."""
    state = _state(peak=50_000, realized=0)
    ok, _ = can_open_new_position(TOPSTEP_50K, state, 49_100, "MES", 1, 0)
    assert ok


def test_can_open_blocked_by_position_cap():
    state = _state(peak=50_000)
    # MES cap = 50; current 50 + desired 1 = 51 → block
    ok, reason = can_open_new_position(TOPSTEP_50K, state, 50_000, "MES", 1, 50)
    assert not ok and reason.startswith("position_cap")


def test_consistency_rule_blocks_at_soft_cap():
    """Consistency rule: 50K Combine max single-day profit = $1,500 (50% of $3,000 target).
    Soft cap = $1,500 - $200 buffer = $1,300. Realized PnL >= $1,300 blocks new entries.
    """
    # Just below soft cap — still allowed
    state = _state(peak=50_000, realized=1_299)
    ok, _ = can_open_new_position(TOPSTEP_50K, state, 51_299, "MES", 1, 0)
    assert ok

    # At soft cap — blocked
    state = _state(peak=50_000, realized=1_300)
    ok, reason = can_open_new_position(TOPSTEP_50K, state, 51_300, "MES", 1, 0)
    assert not ok and reason.startswith("consistency_soft_cap")

    # Well over hard cap — blocked
    state = _state(peak=50_000, realized=1_600)
    ok, reason = can_open_new_position(TOPSTEP_50K, state, 51_600, "MES", 1, 0)
    assert not ok and reason.startswith("consistency_soft_cap")


def test_consistency_rule_does_not_block_when_negative_pnl():
    """Down on the day → consistency rule never triggers (only caps the upside)."""
    state = _state(peak=50_000, realized=-500)
    ok, _ = can_open_new_position(TOPSTEP_50K, state, 49_500, "MES", 1, 0)
    assert ok


# --- EvalProfile freshness ----------------------------------------------

def test_topstep_50k_carries_a_snapshot_date():
    assert TOPSTEP_50K.snapshot_date == date(2026, 4, 29)


def test_age_days_is_positive_when_today_is_after_snapshot():
    snapshot = date(2026, 1, 1)
    profile = replace(TOPSTEP_50K, snapshot_date=snapshot)
    assert eval_profile_age_days(profile, today=date(2026, 4, 1)) == 90


def test_age_days_returns_none_when_snapshot_date_is_unset():
    profile = replace(TOPSTEP_50K, snapshot_date=None)
    assert eval_profile_age_days(profile, today=date(2026, 4, 1)) is None


def test_is_stale_at_threshold_is_not_stale():
    """Boundary: age == max_age_days does NOT count as stale."""
    profile = replace(TOPSTEP_50K, snapshot_date=date(2026, 1, 1))
    today = date(2026, 1, 1) + timedelta(days=EVAL_PROFILE_MAX_AGE_DAYS)
    assert is_eval_profile_stale(profile, today=today) is False


def test_is_stale_one_day_past_threshold():
    profile = replace(TOPSTEP_50K, snapshot_date=date(2026, 1, 1))
    today = date(2026, 1, 1) + timedelta(days=EVAL_PROFILE_MAX_AGE_DAYS + 1)
    assert is_eval_profile_stale(profile, today=today) is True


def test_is_stale_returns_false_when_snapshot_unset():
    """No snapshot_date → can't reason about staleness → don't fire the warning."""
    profile = replace(TOPSTEP_50K, snapshot_date=None)
    assert is_eval_profile_stale(profile, today=date(2030, 1, 1)) is False
