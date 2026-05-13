"""OVERNIGHT_MOMENTUM — overnight continuation of the 15:30-16:00 CT RTH close.

Logic:
  1. Bias window: 15:30-16:00 CT. Track the period's open (first bar)
     and close (last bar). Body = close - open. Bias = sign(body).
     A zero body (doji) blocks entry — no trade that session.
  2. CME maintenance break 16:00-17:00 CT — no entries; the strategy
     simply does nothing during this window.
  3. Entry: the first bar at or after 17:00 CT on the bias day. Side
     follows the bias (long on bullish body, short on bearish).
     "Market open at 17:00 CT" — entry price is the entry bar's close
     (harness convention; ~2-min slip from spec).
  4. Stop: fixed `stop_points` (default 8pt) from entry.
  5. Target: `rr_ratio` × stop distance (default 3.0 → 24pt).
  6. Hard close at 08:00 CT next morning — any open position is
     force-closed at the 08:00 bar's open via `wants_force_flat`.
  7. One trade per session, no re-entries.

Strong vs weak bias: |body| > strong_body_points → "strong"; else
"weak". Encoded in the trade `reason` for downstream slicing.

Live fleet coordination (not enforced by the backtest):
  - At 16:59 CT, BOUNDARY positions must close unconditionally.
  - While this strategy holds, BOUNDARY suppresses new entries.
  - BOUNDARY resumes after the 08:00 force-close.
  The backtest runs strategies in isolation; the coordination rule
  lives in the conductor for live mode.

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
class OvernightMomentumConfig:
    bias_window_start_ct: time = field(default_factory=lambda: time(15, 30))
    bias_window_end_ct: time = field(default_factory=lambda: time(16, 0))
    entry_time_ct: time = field(default_factory=lambda: time(17, 0))
    hard_close_ct: time = field(default_factory=lambda: time(8, 0))
    stop_points: float = 8.0
    rr_ratio: float = 3.0
    # Bias-strength cutoff. |close - open| > this is "strong".
    strong_body_points: float = 3.0
    # Sized to give 5 contracts at the 8pt stop. The Topstep spec calls
    # for 5 MES: 5 * 8pt * $5/pt = $200 plus 5 * $1.24 commission =
    # $206.20. $210 is just over that.
    risk_dollars_per_trade: float = 210.0
    allow_longs: bool = True
    allow_shorts: bool = True


class _DayState:
    """Per-CT-calendar-date state: bias bar + entry flag.

    Bias bar tracking and entry both live on the same calendar date
    (15:30 -> 17:00 CT same day). The position rolls past midnight, but
    by then the strategy short-circuits on current_position != 0 and
    doesn't touch state until force-close happens at 08:00 the next day.
    """
    __slots__ = ("bias_date", "bias_open", "bias_close", "entered")

    def __init__(self, bias_date: date) -> None:
        self.bias_date = bias_date
        self.bias_open: float | None = None
        self.bias_close: float | None = None
        self.entered = False


class OvernightMomentumStrategy:
    name = "overnight_momentum"
    version = "1"
    metadata = StrategyMetadata(
        tier=2,
        regime_fit={"trending": 1.0, "volatile": 0.7, "ranging": 0.3, "quiet": 0.5},
        time_buckets=["17:00-08:00 CT"],
        default_lifecycle="SHADOW",
        timeframe_minutes=2,
    )

    @classmethod
    def tunable_params(cls) -> list[ParameterSpec]:
        return [
            ParameterSpec("stop_points", float, 8.0, 1.0, 30.0, 0.5,
                          "Stop distance from entry (points)"),
            ParameterSpec("rr_ratio", float, 3.0, 0.5, 6.0, 0.1,
                          "Target multiple of stop distance"),
            ParameterSpec("strong_body_points", float, 3.0, 0.0, 15.0, 0.5,
                          "Cutoff between strong and weak bias bars"),
            ParameterSpec("risk_dollars_per_trade", float, 210.0, 25.0, 1000.0, 10.0,
                          "Risk per trade ($) — sized for ~5 contracts at default"),
        ]

    def __init__(
        self,
        config: OvernightMomentumConfig | None = None,
        contract: FuturesContract = MES,
    ) -> None:
        self.config = config or OvernightMomentumConfig()
        self.contract = contract
        self.timeframe_minutes = self.metadata.timeframe_minutes
        self._day: _DayState | None = None

        self._bias_start_min = _to_minutes(self.config.bias_window_start_ct)
        self._bias_end_min = _to_minutes(self.config.bias_window_end_ct)
        self._entry_min = _to_minutes(self.config.entry_time_ct)
        self._hard_close_min = _to_minutes(self.config.hard_close_ct)

        if not (self._bias_start_min < self._bias_end_min
                <= self._entry_min):
            raise ValueError(
                "OvernightMomentumConfig: require "
                "bias_start < bias_end <= entry_time"
            )
        if not (0 <= self._hard_close_min < self._entry_min):
            raise ValueError(
                "OvernightMomentumConfig: hard_close must precede "
                "entry_time within the same CT day"
            )

    def required_history_bars(self) -> int:
        return 0

    # ───────────────────────── helpers ─────────────────────────

    def _ct_parts(self, bar: Bar) -> tuple[date, int]:
        ct = bar.t.astimezone(CT)
        return ct.date(), ct.hour * 60 + ct.minute

    def _ensure_day_state(self, bias_date: date) -> _DayState:
        if self._day is None or self._day.bias_date != bias_date:
            self._day = _DayState(bias_date)
        return self._day

    def _in_bias_window(self, minute_of_day: int) -> bool:
        return self._bias_start_min <= minute_of_day < self._bias_end_min

    # ───────────────────────── harness hook ─────────────────────

    def wants_force_flat(self, bar: Bar) -> bool:
        """True during [08:00, 17:00) CT — the daytime window where any
        still-open overnight position must be flattened. Outside that
        window (17:00 evening through 07:58 morning) returns False so
        the bracket runs uninterrupted overnight."""
        _, minute = self._ct_parts(bar)
        return self._hard_close_min <= minute < self._entry_min

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

        # Build the bias bar across 15:30-16:00 CT.
        if self._in_bias_window(ct_minute):
            day = self._ensure_day_state(ct_date)
            if day.bias_open is None:
                day.bias_open = bar.o
            day.bias_close = bar.c
            return None

        # Outside the bias and entry phases — nothing to do.
        # Skip 16:00-16:59 CT (maintenance break) and pre-17:00 hours
        # of an unrelated day.
        if ct_minute < self._entry_min:
            return None

        # Entry phase: 17:00 CT or later, on the same CT date as the
        # bias bar. (No bias today → no trade today.)
        day = self._day
        if day is None or day.bias_date != ct_date:
            return None
        if day.entered:
            return None
        if day.bias_open is None or day.bias_close is None:
            return None

        body = day.bias_close - day.bias_open
        if body > 0 and self.config.allow_longs:
            side = "buy"
        elif body < 0 and self.config.allow_shorts:
            side = "sell"
        else:
            return None  # doji or disallowed side

        strong = abs(body) > self.config.strong_body_points
        strength_tag = "strong" if strong else "weak"
        direction = "long" if side == "buy" else "short"

        stop_distance = self.config.stop_points
        target_distance = stop_distance * self.config.rr_ratio
        tick = self.contract.tick_size
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
            return Signal(side=side, size=0,  # type: ignore[arg-type]
                          reason=f"blocked: {block_reason}")

        day.entered = True
        return Signal(
            side=side,  # type: ignore[arg-type]
            size=size,
            bracket=BracketSpec(
                stop_loss_offset_ticks=stop_ticks,
                take_profit_offset_ticks=target_ticks,
            ),
            reason=f"overnight_{direction}_{strength_tag}",
        )
