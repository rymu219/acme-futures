"""ORB_PULLBACK — opening-range break with pullback re-entry.

Logic:
  1. Track the high and low of the first N minutes of RTH (default 30,
     starting at 09:30 CT). Configurable via `opening_range_minutes`.
  2. After the OR window closes, on a confirmed break (close beyond
     ORH/ORL on any post-OR bar), mark the direction as live.
  3. Wait for a pullback — a subsequent bar whose low (long) or high
     (short) returns to within `pullback_tolerance_ticks` of the
     broken level. The bar's close is irrelevant; only the wick into
     the zone matters for the pullback.
  4. After the pullback is registered, wait for a *separate later bar*
     that closes back through the broken level in the break direction.
     That's the continuation confirmation; enter on that bar's close.
     A pullback bar that also closes through the level does NOT count
     as its own confirmation — the confirmation must be a subsequent
     bar.
  5. Bracket exit: `stop_ticks` and `target_ticks` from entry price
     (fixed, no volatility scaling).
  6. One entry per direction per session. Trades only during the
     09:00-13:00 CT window (the slot BOUNDARY skips).

No trend filter, no EMA, no volume confirmation. Raw signal only —
filtering happens *after* we see the 2-year data.

Lifecycle: SHADOW.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from datetime import time as dtime
from zoneinfo import ZoneInfo

from acme.broker.base import Bar, BracketSpec
from acme.contracts import MES, FuturesContract
from acme.risk import DailyState, EvalProfile, can_open_new_position, dollars_to_contracts
from acme.strategies.base import Signal, StrategyMetadata
from acme.strategies.params import ParameterSpec

CT = ZoneInfo("America/Chicago")

# OR window always starts at the NYSE cash open (09:30 CT). Width is
# configurable; start is not — by design, ORB is anchored to the open.
RTH_OPEN_CT = dtime(9, 30)


@dataclass
class ORBPullbackConfig:
    opening_range_minutes: int = 30
    pullback_tolerance_ticks: int = 4
    stop_ticks: int = 8
    target_ticks: int = 16
    # CT hour bounds (end exclusive). Default is the 09-13 CT window
    # that BOUNDARY's entry_hour_blacklist_ct excludes.
    session_start_ct: int = 9
    session_end_ct: int = 13
    risk_dollars_per_trade: float = 25.0
    allow_longs: bool = True
    allow_shorts: bool = True


class _DayState:
    """Per-CT-session OR + per-direction state-machine bookkeeping."""
    __slots__ = ("trade_date", "or_high", "or_low",
                 "long_break", "long_pullback", "long_entered",
                 "short_break", "short_pullback", "short_entered")

    def __init__(self, trade_date: date) -> None:
        self.trade_date = trade_date
        self.or_high: float | None = None
        self.or_low: float | None = None
        self.long_break = False
        self.long_pullback = False
        self.long_entered = False
        self.short_break = False
        self.short_pullback = False
        self.short_entered = False


class ORBPullbackStrategy:
    name = "orb_pullback"
    version = "1"
    metadata = StrategyMetadata(
        tier=2,
        regime_fit={"trending": 1.0, "volatile": 0.7, "ranging": 0.2, "quiet": 0.3},
        time_buckets=["09:00-13:00 CT"],
        default_lifecycle="SHADOW",
        timeframe_minutes=2,
    )

    @classmethod
    def tunable_params(cls) -> list[ParameterSpec]:
        return [
            ParameterSpec("opening_range_minutes", int, 30, 5, 90, 5,
                          "Opening range window (min)"),
            ParameterSpec("pullback_tolerance_ticks", int, 4, 1, 20, 1,
                          "Pullback proximity to broken level (ticks)"),
            ParameterSpec("stop_ticks", int, 8, 1, 40, 1,
                          "Stop distance from entry (ticks)"),
            ParameterSpec("target_ticks", int, 16, 1, 80, 1,
                          "Target distance from entry (ticks)"),
            ParameterSpec("risk_dollars_per_trade", float, 25.0, 10.0, 200.0, 5.0,
                          "Risk per trade ($)"),
        ]

    def __init__(
        self,
        config: ORBPullbackConfig | None = None,
        contract: FuturesContract = MES,
    ) -> None:
        self.config = config or ORBPullbackConfig()
        self.contract = contract
        self.timeframe_minutes = self.metadata.timeframe_minutes
        self._day: _DayState | None = None
        # Precompute OR window minute-of-day bounds (relative to CT midnight).
        or_start_min = RTH_OPEN_CT.hour * 60 + RTH_OPEN_CT.minute
        self._or_start_min = or_start_min
        self._or_end_min = or_start_min + self.config.opening_range_minutes

    def required_history_bars(self) -> int:
        # OR is built live each session — no historical warmup needed.
        return 0

    # ───────────────────────── helpers ─────────────────────────

    def _ct_parts(self, bar: Bar) -> tuple[date, int]:
        """Return (CT date, CT minute-of-day) for the bar's start time."""
        ct = bar.t.astimezone(CT)
        return ct.date(), ct.hour * 60 + ct.minute

    def _ensure_day_state(self, trade_date: date) -> _DayState:
        if self._day is None or self._day.trade_date != trade_date:
            self._day = _DayState(trade_date)
        return self._day

    def _in_or_window(self, minute_of_day: int) -> bool:
        return self._or_start_min <= minute_of_day < self._or_end_min

    def _in_session(self, ct_hour: int) -> bool:
        return self.config.session_start_ct <= ct_hour < self.config.session_end_ct

    # ───────────────────────── on_bar ──────────────────────────

    def on_bar(
        self,
        bar: Bar,
        *,
        state: DailyState,
        profile: EvalProfile,
        current_position: int,
        current_balance_unrealized: float,
    ) -> Signal | None:
        # Bracket-handled exits; stay quiet while in position.
        if current_position != 0:
            return None

        ct_date, ct_minute = self._ct_parts(bar)
        day = self._ensure_day_state(ct_date)

        # OR window: accumulate the range, no entries.
        if self._in_or_window(ct_minute):
            day.or_high = bar.h if day.or_high is None else max(day.or_high, bar.h)
            day.or_low = bar.l if day.or_low is None else min(day.or_low, bar.l)
            return None

        # OR must have completed (we've seen at least one bar in the window).
        if day.or_high is None or day.or_low is None:
            return None

        # Session gate.
        ct_hour = ct_minute // 60
        if not self._in_session(ct_hour):
            return None

        tol_pts = self.config.pullback_tolerance_ticks * self.contract.tick_size

        # ── LONG path ─────────────────────────────────────────
        # Three phases enforced via elif so each bar advances at most one
        # phase. The continuation bar must therefore be strictly after the
        # pullback bar — preventing the "enter at the level touch" failure
        # where the same bar that pulls back also triggers entry.
        if self.config.allow_longs and not day.long_entered:
            orh = day.or_high
            if not day.long_break:
                if bar.c > orh:
                    day.long_break = True
            elif not day.long_pullback:
                if bar.l <= orh + tol_pts:
                    day.long_pullback = True
            elif bar.c > orh:
                sig = self._build_signal(
                    bar=bar, side="buy", state=state, profile=profile,
                    current_position=current_position,
                    current_balance_unrealized=current_balance_unrealized,
                    reason="orb_pullback_long",
                )
                if sig is not None and sig.size > 0:
                    day.long_entered = True
                    return sig

        # ── SHORT path ────────────────────────────────────────
        if self.config.allow_shorts and not day.short_entered:
            orl = day.or_low
            if not day.short_break:
                if bar.c < orl:
                    day.short_break = True
            elif not day.short_pullback:
                if bar.h >= orl - tol_pts:
                    day.short_pullback = True
            elif bar.c < orl:
                sig = self._build_signal(
                    bar=bar, side="sell", state=state, profile=profile,
                    current_position=current_position,
                    current_balance_unrealized=current_balance_unrealized,
                    reason="orb_pullback_short",
                )
                if sig is not None and sig.size > 0:
                    day.short_entered = True
                    return sig

        return None

    # ───────────────────────── signal construction ──────────────

    def _build_signal(
        self, *, bar: Bar, side: str, state: DailyState, profile: EvalProfile,
        current_position: int, current_balance_unrealized: float, reason: str,
    ) -> Signal | None:
        stop_distance = self.config.stop_ticks * self.contract.tick_size
        target_distance = self.config.target_ticks * self.contract.tick_size
        if stop_distance <= 0 or target_distance <= 0:
            return None

        round_turn_fee = profile.round_turn_fees.get(self.contract.symbol, 0.0)
        size = dollars_to_contracts(
            self.config.risk_dollars_per_trade,
            stop_distance,
            self.contract.point_value,
            round_turn_fee=round_turn_fee,
        )
        if size <= 0:
            return None

        allowed, block_reason = can_open_new_position(
            profile, state, current_balance_unrealized,
            self.contract.symbol, size, current_position,
        )
        if not allowed:
            return Signal(side=side, size=0, reason=f"blocked: {block_reason}")  # type: ignore[arg-type]

        return Signal(
            side=side,  # type: ignore[arg-type]
            size=size,
            bracket=BracketSpec(
                stop_loss_offset_ticks=self.config.stop_ticks,
                take_profit_offset_ticks=self.config.target_ticks,
            ),
            reason=reason,
        )
