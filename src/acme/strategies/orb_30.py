"""ORB_30 — pre-cash-open range break, midpoint stop, hard 9:30 close.

Logic:
  1. Range window: 08:30-09:00 CT. RangeHigh and RangeLow are the high
     and low across all bars in that window.
  2. Midpoint = (RangeHigh + RangeLow) / 2.
  3. Trade window: 09:00-09:30 CT only. No entries outside.
  4. Entry: first bar that closes above RangeHigh (long) or below
     RangeLow (short). One trade per day; first break wins.
  5. Stop: the range midpoint. The stop *distance* therefore scales
     with that day's range, not a fixed tick count.
  6. Target: entry +/- rr_ratio × stop_distance (default 1.5R).
  7. Hard close at 09:30 CT — any open position is force-closed at
     the open of the 09:30 bar regardless of P&L. The harness's
     `wants_force_flat(bar)` hook handles this.

No trend filter, no volume gate, no confluence overlay. Raw signal
only; filter after we see the data.

Lifecycle: SHADOW.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from zoneinfo import ZoneInfo

from acme.broker.base import Bar, BracketSpec
from acme.contracts import MES, FuturesContract
from acme.risk import DailyState, EvalProfile, can_open_new_position, dollars_to_contracts
from acme.strategies.base import Signal, StrategyMetadata
from acme.strategies.params import ParameterSpec

CT = ZoneInfo("America/Chicago")


def _to_minutes(t: time) -> int:
    return t.hour * 60 + t.minute


@dataclass
class ORB30Config:
    range_start_ct: time = field(default_factory=lambda: time(8, 30))
    range_end_ct: time = field(default_factory=lambda: time(9, 0))
    trade_window_end_ct: time = field(default_factory=lambda: time(9, 30))
    rr_ratio: float = 1.5
    # Larger than other strategies' $25 because the midpoint stop is
    # naturally wide (~half the day's 08:30-09:00 range). At $25 with a
    # typical 10pt stop, dollars_to_contracts returns 0 and the signal
    # silently drops; only sub-10pt range days fire. $100 lets a 10pt
    # stop size to 1 contract; tighter ranges get 2-3.
    risk_dollars_per_trade: float = 100.0
    allow_longs: bool = True
    allow_shorts: bool = True


class _DayState:
    """Per-CT-session range + entry bookkeeping."""
    __slots__ = ("trade_date", "range_high", "range_low", "entered")

    def __init__(self, trade_date: date) -> None:
        self.trade_date = trade_date
        self.range_high: float | None = None
        self.range_low: float | None = None
        self.entered = False


class ORB30Strategy:
    name = "orb_30"
    version = "1"
    metadata = StrategyMetadata(
        tier=2,
        regime_fit={"trending": 1.0, "volatile": 0.8, "ranging": 0.2, "quiet": 0.3},
        time_buckets=["09:00-09:30 CT"],
        default_lifecycle="SHADOW",
        timeframe_minutes=2,
    )

    @classmethod
    def tunable_params(cls) -> list[ParameterSpec]:
        return [
            ParameterSpec("rr_ratio", float, 1.5, 0.5, 4.0, 0.1,
                          "Reward-to-risk ratio (target / stop distance)"),
            ParameterSpec("risk_dollars_per_trade", float, 25.0, 10.0, 200.0, 5.0,
                          "Risk per trade ($)"),
        ]

    def __init__(
        self,
        config: ORB30Config | None = None,
        contract: FuturesContract = MES,
    ) -> None:
        self.config = config or ORB30Config()
        self.contract = contract
        self.timeframe_minutes = self.metadata.timeframe_minutes
        self._day: _DayState | None = None

        self._range_start_min = _to_minutes(self.config.range_start_ct)
        self._range_end_min = _to_minutes(self.config.range_end_ct)
        self._trade_window_end_min = _to_minutes(self.config.trade_window_end_ct)

        if not (self._range_start_min < self._range_end_min
                <= self._trade_window_end_min):
            raise ValueError(
                "ORB30Config: require range_start < range_end <= trade_window_end"
            )

    def required_history_bars(self) -> int:
        # Range is built live each session — no warmup needed.
        return 0

    # ───────────────────────── helpers ─────────────────────────

    def _ct_parts(self, bar: Bar) -> tuple[date, int]:
        ct = bar.t.astimezone(CT)
        return ct.date(), ct.hour * 60 + ct.minute

    def _ensure_day_state(self, trade_date: date) -> _DayState:
        if self._day is None or self._day.trade_date != trade_date:
            self._day = _DayState(trade_date)
        return self._day

    def _in_range_window(self, minute_of_day: int) -> bool:
        return self._range_start_min <= minute_of_day < self._range_end_min

    def _in_trade_window(self, minute_of_day: int) -> bool:
        return self._range_end_min <= minute_of_day < self._trade_window_end_min

    # ───────────────────────── harness hook ─────────────────────

    def wants_force_flat(self, bar: Bar) -> bool:
        """Harness calls this after bracket checks. Returns True iff
        the bar is at or past the trade-window end, signalling that any
        still-open position should be flattened at this bar's open."""
        _, minute = self._ct_parts(bar)
        return minute >= self._trade_window_end_min

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
        # Bracket / force-flat handle exits; stay quiet while in position.
        if current_position != 0:
            return None

        ct_date, ct_minute = self._ct_parts(bar)
        day = self._ensure_day_state(ct_date)

        # Build the range during the 08:30-09:00 window.
        if self._in_range_window(ct_minute):
            day.range_high = (bar.h if day.range_high is None
                              else max(day.range_high, bar.h))
            day.range_low = (bar.l if day.range_low is None
                             else min(day.range_low, bar.l))
            return None

        # Only fire during 09:00-09:30, only once per day, only with a
        # valid range.
        if day.entered:
            return None
        if not self._in_trade_window(ct_minute):
            return None
        if day.range_high is None or day.range_low is None:
            return None

        midpoint = (day.range_high + day.range_low) / 2.0
        tick = self.contract.tick_size

        side: str | None = None
        if self.config.allow_longs and bar.c > day.range_high:
            side = "buy"
            stop_distance = bar.c - midpoint
        elif self.config.allow_shorts and bar.c < day.range_low:
            side = "sell"
            stop_distance = midpoint - bar.c
        else:
            return None

        if stop_distance <= 0:
            return None
        target_distance = stop_distance * self.config.rr_ratio

        stop_ticks = max(1, int(round(stop_distance / tick)))
        target_ticks = max(1, int(round(target_distance / tick)))

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

        day.entered = True
        direction = "long" if side == "buy" else "short"
        return Signal(
            side=side,  # type: ignore[arg-type]
            size=size,
            bracket=BracketSpec(
                stop_loss_offset_ticks=stop_ticks,
                take_profit_offset_ticks=target_ticks,
            ),
            reason=f"orb30_{direction}",
        )
