"""GO/NO-GO + BOUNDARY-style level proximity, applied to 08:00-12:00 CT.

Phase 5 test variant. Backtest-only. Not registered, not bound by the
fleet runner.

Hypothesis: Phase 2 RUN B showed the morning window (08-12 CT) lifts
GO/NO-GO from PF 0.85 to PF 0.95 — monotonically improved economics
from filtering. The residual ~1.3 pp WR gap to the bracket-implied
break-even (40.2%) might close if a structurally independent edge is
stacked on top. BOUNDARY's levels alone produced PF ~3.80 in the same
25-month window. EMA/volume primitives (GO/NO-GO) and raw-OHLC levels
(BOUNDARY) are disjoint — this strategy tests whether they compound.

Entry rule:
    GO/NO-GO signal       — three-AND gate from go_no_go.py (unchanged)
  + time window check     — default ((08, 12),) CT
  + level proximity check — direction-correlated:
      LONG  signal needs bar.low  within `level_buffer_ticks` of a
                         low-side level (PDL, ONL, ORL)
      SHORT signal needs bar.high within `level_buffer_ticks` of a
                         high-side level (PDH, ONH, ORH)

Day-levels are supplied via `set_levels(DayLevels)` exactly like
BOUNDARY. The backtest harness (`scripts/backtest_new_fleet.py`)
detects `set_levels` and the `compute_levels_for_all_days(bars)` path
already feeds it per trading-day rollover — no harness change needed.

Everything else matches `GoNoGoStrategy`:
  - bidirectional default (allow_longs=True, allow_shorts=True)
  - no min-bars-held opposite-exit (reverse immediately)
  - 1.5/2.5 × ATR bracket, $25 risk per trade
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
    """Defaults reflect the Phase 5 hypothesis: morning window on by
    default (this strategy's whole thesis), level buffer matches
    BOUNDARY's default."""
    go_no_go: GoNoGoConfig = field(default_factory=GoNoGoConfig)

    # Morning window — on by default. The strategy is *named* for the
    # morning thesis; running it without a time window defeats the test.
    time_windows: tuple[LevelsTimeWindow, ...] = field(
        default_factory=lambda: (LevelsTimeWindow.from_hours(8, 12),),
    )

    # Level proximity — same default as BoundaryConfig.level_buffer_ticks.
    level_buffer_ticks: int = 4

    # Bracket / sizing — copied from IGNITION & GoNoGoStrategy.
    atr_period: int = 4
    atr_stop_multiple: float = 1.5
    atr_target_multiple: float = 2.5

    # Direction policy — bidirectional like GoNoGoStrategy default.
    allow_longs: bool = True
    allow_shorts: bool = True

    # Sizing
    risk_dollars_per_trade: float = 25.0


class GoNoGoLevelsStrategy:
    """GO/NO-GO + direction-correlated level proximity. Test variant."""

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
        """Called by the backtest harness (or conductor in live) at each
        trading-day rollover. Same contract as BoundaryStrategy."""
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

        Returns False if no levels are set (no qualifying day yet) or
        none of the relevant levels are within buffer.
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
        # Reversal also has to pass level-proximity in the new direction.
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
