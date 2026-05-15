"""GO/NO-GO with BOUNDARY-style level proximity — fourth fleet keeper.

Composition:
  - GO/NO-GO three-AND gate (EMA(9/14) separation, volume-ratio,
    slope alignment) — same engine as `go_no_go.py`
  - Direction-correlated key-level proximity:
      LONG  signal needs bar.low  within `level_buffer_ticks` of a
                          low-side level (PDL, ONL, ORL)
      SHORT signal needs bar.high within `level_buffer_ticks` of a
                          high-side level (PDH, ONH, ORH)
  - Time gate: 08:00-12:00 CT (morning window)
  - Bracket: 1.5 × ATR stop, 2.5 × ATR target — same as IGNITION
  - Direction: bidirectional by default
  - No min-bars-held opposite-exit; reverses immediately on opposite signal

Day-levels (PDH/PDL/ONH/ONL/ORH/ORL) are supplied via `set_levels()`
at trade-date rollover, same contract as BOUNDARY. The conductor /
backtest harness owns the rollover wiring.

Lifecycle: SHADOW. Promotion through PILOT → LIVE goes through
PerfTracker as usual.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo

from acme.broker.base import Bar, BracketSpec
from acme.contracts import MES, FuturesContract
from acme.indicators import ATR
from acme.levels import DayLevels
from acme.risk import DailyState, EvalProfile, can_open_new_position, dollars_to_contracts
from acme.strategies.base import Signal, StrategyMetadata
from acme.strategies.go_no_go import GoNoGoConfig, GoNoGoEngine
from acme.strategies.params import ParameterSpec

CT = ZoneInfo("America/Chicago")


@dataclass(frozen=True)
class LevelsTimeWindow:
    start: time
    end: time

    def contains(self, ts: datetime) -> bool:
        ct = ts.astimezone(CT).time()
        return self.start <= ct < self.end

    @classmethod
    def from_hours(cls, start_hour: int, end_hour: int) -> LevelsTimeWindow:
        return cls(time(start_hour, 0), time(end_hour, 0))


@dataclass
class GoNoGoLevelsConfig:
    go_no_go: GoNoGoConfig = field(default_factory=GoNoGoConfig)

    # Morning window — on by default; the level-proximity edge is
    # window-specific (08-12 CT morning thesis).
    time_windows: tuple[LevelsTimeWindow, ...] = field(
        default_factory=lambda: (LevelsTimeWindow.from_hours(8, 12),),
    )

    # Level proximity — same default as BoundaryConfig.level_buffer_ticks.
    level_buffer_ticks: int = 4

    # Bracket / sizing
    atr_period: int = 4
    atr_stop_multiple: float = 1.5
    atr_target_multiple: float = 2.5

    # Direction policy — bidirectional
    allow_longs: bool = True
    allow_shorts: bool = True

    # Sizing
    risk_dollars_per_trade: float = 25.0


class GoNoGoLevelsStrategy:
    """GO/NO-GO + direction-correlated key-level proximity."""

    name = "go_no_go_levels"
    version = "1"
    metadata = StrategyMetadata(
        tier=2,
        regime_fit={"trending": 1.0, "volatile": 0.7, "ranging": 0.5, "quiet": 0.2},
        time_buckets=["08:00-12:00 CT"],
        default_lifecycle="SHADOW",
        timeframe_minutes=2,
    )

    @classmethod
    def tunable_params(cls) -> list[ParameterSpec]:
        return [
            ParameterSpec("level_buffer_ticks", int, 4, 1, 16, 1,
                          "Bar high/low must be within this many ticks of a level"),
            ParameterSpec("atr_stop_multiple", float, 1.5, 0.5, 4.0, 0.1,
                          "ATR multiple for stop loss"),
            ParameterSpec("atr_target_multiple", float, 2.5, 1.0, 6.0, 0.1,
                          "ATR multiple for take profit"),
            ParameterSpec("risk_dollars_per_trade", float, 25.0, 10.0, 200.0, 5.0,
                          "Risk per trade ($)"),
        ]

    def __init__(
        self,
        config: GoNoGoLevelsConfig | None = None,
        contract: FuturesContract = MES,
    ) -> None:
        self.config = config or GoNoGoLevelsConfig()
        self.contract = contract
        self.timeframe_minutes = self.metadata.timeframe_minutes
        self._gng = GoNoGoEngine(self.config.go_no_go)
        self._atr = ATR(self.config.atr_period)
        self._levels: DayLevels | None = None

    def required_history_bars(self) -> int:
        return max(
            self._gng.required_history_bars(),
            self.config.atr_period,
        ) + 2

    # ───────────────────────── levels ──────────────────────────

    def set_levels(self, levels: DayLevels) -> None:
        """Called at each trading-day rollover with the day's six levels."""
        self._levels = levels

    # ───────────────────────── helpers ─────────────────────────

    def _in_window(self, ts: datetime) -> bool:
        if not self.config.time_windows:
            return True
        return any(w.contains(ts) for w in self.config.time_windows)

    def _level_proximity_ok(self, bar: Bar, side: str) -> bool:
        """Direction-correlated level proximity.

          LONG  → bar.low  within buffer of any low-side level
                  (PDL, ONL, ORL)
          SHORT → bar.high within buffer of any high-side level
                  (PDH, ONH, ORH)
        """
        if self._levels is None:
            return False
        buf_pts = self.config.level_buffer_ticks * self.contract.tick_size
        if side == "buy":
            candidates = (self._levels.pdl, self._levels.onl, self._levels.orl)
            price = bar.l
        else:
            candidates = (self._levels.pdh, self._levels.onh, self._levels.orh)
            price = bar.h
        for lvl in candidates:
            if lvl is None:
                continue
            if abs(price - lvl) <= buf_pts:
                return True
        return False

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
        gng = self._gng.update(bar)
        self._atr.update(bar)

        if gng is None or not self._atr.is_warm:
            return None

        signal_raw = gng.signal

        # ────── In-position: reverse on opposite signal ──────
        if current_position != 0:
            if signal_raw == 0:
                return None
            holding_long = current_position > 0
            new_long = signal_raw == 1
            if holding_long == new_long:
                return None
            side = "buy" if new_long else "sell"
            if not self._in_window(bar.t):
                return None
            if not self._level_proximity_ok(bar, side):
                return None
            return self._build_signal(
                bar=bar, side=side, state=state, profile=profile,
                current_position=current_position,
                current_balance_unrealized=current_balance_unrealized,
                reason="gng_levels_reverse",
            )

        # ────── Flat: enter on a fresh signal ──────
        if signal_raw == 0:
            return None
        if not self._in_window(bar.t):
            return None
        if signal_raw == 1 and not self.config.allow_longs:
            return None
        if signal_raw == -1 and not self.config.allow_shorts:
            return None

        side = "buy" if signal_raw == 1 else "sell"
        if not self._level_proximity_ok(bar, side):
            return None

        return self._build_signal(
            bar=bar, side=side, state=state, profile=profile,
            current_position=current_position,
            current_balance_unrealized=current_balance_unrealized,
            reason="gng_levels_entry",
        )

    # ───────────────────────── signal construction ──────────────

    def _build_signal(
        self, *, bar: Bar, side: str, state: DailyState, profile: EvalProfile,
        current_position: int, current_balance_unrealized: float,
        reason: str,
    ) -> Signal | None:
        atr_val = self._atr.value
        if atr_val is None or atr_val <= 0:
            return None

        stop_distance = atr_val * self.config.atr_stop_multiple
        target_distance = atr_val * self.config.atr_target_multiple
        if stop_distance <= 0:
            return None

        stop_ticks = max(1, int(round(stop_distance / self.contract.tick_size)))
        target_ticks = max(1, int(round(target_distance / self.contract.tick_size)))

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
            side=side,                # type: ignore[arg-type]
            size=size,
            bracket=BracketSpec(
                stop_loss_offset_ticks=stop_ticks,
                take_profit_offset_ticks=target_ticks,
            ),
            reason=reason,
        )
