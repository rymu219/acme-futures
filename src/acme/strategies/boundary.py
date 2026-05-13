"""BOUNDARY — level-rejection strategy.

Entry: price is within `level_buffer_ticks` of a defined day-level
(PDH, PDL, ONH, ONL, ORH, ORL) AND an exhaustion bar has just printed
in the correct direction (top-exhaustion near a high-side level →
short; bottom-exhaustion near a low-side level → long).

Exit:
  - **Target** = midpoint between the level and the opposite-side level
    (e.g. fade off ONH → target ONL midpoint, simplified to VWAP-style
    target = level ± `target_distance_ticks` if no opposite level
    available).
  - **Stop** = the level itself, plus `stop_buffer_ticks` past it.
  - **Time-out**: handled by the conductor's flatten policy; BOUNDARY
    doesn't emit its own time exit.

Day-levels are supplied by the caller via `set_levels(DayLevels)`. The
caller is responsible for refreshing levels at session-roll boundaries
(typically once at the start of a new trade date).

Lifecycle: SHADOW. Allows BOTH long and short — BOUNDARY is the one
strategy in the new fleet that's natively bidirectional because
high-side and low-side levels are symmetric.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time as dtime

from acme.broker.base import Bar, BracketSpec
from acme.contracts import MES, FuturesContract
from acme.indicators import ATR
from acme.levels import DayLevels
from acme.risk import DailyState, EvalProfile, can_open_new_position, dollars_to_contracts
from acme.strategies.base import Signal, StrategyMetadata
from acme.strategies.exhaustion import ExhaustionDetector
from acme.strategies.params import ParameterSpec


@dataclass
class BoundaryConfig:
    # Proximity to count as "at the level"
    level_buffer_ticks: int = 4
    # Stop placed `stop_buffer_ticks` past the level
    stop_buffer_ticks: int = 4
    # Target: distance in ticks from level toward the opposite side
    target_distance_ticks: int = 20
    # Exhaustion detector knobs (defaults match exhaustion.py)
    exh_lookback_bars: int = 10
    exh_volume_avg_period: int = 20
    exh_body_max_ratio: float = 0.30
    exh_volume_max_ratio: float = 1.00
    # ATR for warm-up only (not used for sizing — BOUNDARY uses level-based
    # ticks directly)
    atr_period: int = 14
    # Sizing
    risk_dollars_per_trade: float = 25.0
    # Direction (symmetric by default)
    allow_longs: bool = True
    allow_shorts: bool = True
    # Entry-hour blacklist (CT). The 2-year backtest showed BOUNDARY's
    # edge is concentrated in low-liquidity hours (03-08 CT, 17-23 CT)
    # and reverses during peak RTH volume (09-13 CT: PF 0.31-0.80,
    # -$712 across that window over 2 years). Skip those hours by
    # default; pass `()` to disable.
    entry_hour_blacklist_ct: tuple[int, ...] = (9, 10, 11, 12, 13)
    # Level classes to consider. "pd" = previous-day H/L, "on" = overnight
    # H/L, "or" = opening-range H/L. The 2-year breakdown showed PD levels
    # are a clear drag (PF 0.69, -$289), ON levels carry the edge
    # (PF 27, +$3,228), and OR levels are marginally positive
    # (PF 1.28, +$511). Default drops PD; pass ("pd","on","or") to
    # restore the old fully-pooled behavior.
    level_classes_enabled: tuple[str, ...] = ("on", "or")
    # Per-class hour blacklist (CT). 2-year OR-by-hour breakdown:
    # hours {0, 15, 18, 19, 23} CT are net-negative (PF 0.00-0.70,
    # -$291 combined). Skipping them lifts OR's PF from 1.30 toward
    # ~1.8-2.0 with no profit lost. ON is left unfiltered — its edge
    # is uniformly strong across the non-RTH window.
    # Map: class name → tuple of CT hours where that class is suppressed.
    level_class_hour_blacklist_ct: dict[str, tuple[int, ...]] = field(
        default_factory=lambda: {"or": (0, 15, 18, 19, 23)}
    )
    # Fleet coordination — CT clock times at which any open BOUNDARY
    # position must be force-flattened to make way for higher-priority
    # strategies on the next bar. Defaults:
    #   08:29 CT → clear before GAP_FILL evaluates at 08:30 (priority
    #             during the 08:30-08:59 BD-allowed overlap)
    #   16:59 CT → clear before OVERNIGHT_DRIFT evaluates at 17:00
    # Empty tuple disables the rule (and restores the prior behaviour).
    # The conductor honors `wants_force_flat(bar)` to call broker.flatten_all
    # in live mode or to close phantom positions in dry-run.
    fleet_coordination_close_times_ct: tuple[dtime, ...] = field(
        default_factory=lambda: (dtime(8, 29), dtime(16, 59))
    )


# Level class → level names. A high-side level (ending in 'h') is a
# resistance candidate — fade short. A low-side level (ending in 'l') is
# a support candidate — fade long.
_CLASS_LEVELS: dict[str, tuple[str, str]] = {
    "pd": ("pdh", "pdl"),
    "on": ("onh", "onl"),
    "or": ("orh", "orl"),
}


class BoundaryStrategy:
    name = "boundary"
    version = "1"
    metadata = StrategyMetadata(
        tier=2,
        regime_fit={"trending": 0.3, "volatile": 0.8, "ranging": 1.0, "quiet": 0.7},
        time_buckets=[],
        default_lifecycle="SHADOW",
        timeframe_minutes=2,
    )

    @classmethod
    def tunable_params(cls) -> list[ParameterSpec]:
        return [
            ParameterSpec("level_buffer_ticks", int, 4, 1, 20, 1,
                          "Proximity to a level counted as 'at' it"),
            ParameterSpec("stop_buffer_ticks", int, 4, 1, 20, 1,
                          "Stop offset past the level"),
            ParameterSpec("target_distance_ticks", int, 20, 4, 80, 1,
                          "Target distance toward the opposite side"),
            ParameterSpec("risk_dollars_per_trade", float, 25.0, 10.0, 200.0, 5.0,
                          "Risk per trade ($)"),
        ]

    def __init__(
        self,
        config: BoundaryConfig | None = None,
        contract: FuturesContract = MES,
    ) -> None:
        self.config = config or BoundaryConfig()
        self.contract = contract
        self.timeframe_minutes = self.metadata.timeframe_minutes
        self._exh = ExhaustionDetector(
            lookback_bars=self.config.exh_lookback_bars,
            volume_avg_period=self.config.exh_volume_avg_period,
            body_max_ratio=self.config.exh_body_max_ratio,
            volume_max_ratio=self.config.exh_volume_max_ratio,
        )
        self._atr = ATR(self.config.atr_period)
        self._levels: DayLevels | None = None

        bad = [c for c in self.config.level_classes_enabled if c not in _CLASS_LEVELS]
        if bad:
            raise ValueError(
                f"unknown level_classes_enabled entries: {bad} "
                f"(valid: {sorted(_CLASS_LEVELS)})"
            )
        bad_blk = [c for c in self.config.level_class_hour_blacklist_ct
                   if c not in _CLASS_LEVELS]
        if bad_blk:
            raise ValueError(
                f"unknown level_class_hour_blacklist_ct entries: {bad_blk} "
                f"(valid: {sorted(_CLASS_LEVELS)})"
            )
        self._high_by_class: dict[str, str] = {
            c: _CLASS_LEVELS[c][0] for c in self.config.level_classes_enabled
        }
        self._low_by_class: dict[str, str] = {
            c: _CLASS_LEVELS[c][1] for c in self.config.level_classes_enabled
        }

    def required_history_bars(self) -> int:
        return max(
            self.config.exh_lookback_bars,
            self.config.exh_volume_avg_period,
            self.config.atr_period,
        ) + 2

    def set_levels(self, levels: DayLevels) -> None:
        """Caller updates day-levels (typically once per trade date)."""
        self._levels = levels

    # ───────────────────────── harness / conductor hooks ─────────

    def wants_force_flat(self, bar: Bar) -> bool:
        """Fleet coordination — True iff `bar` falls in a 2-min window
        that contains one of the configured coordination close times.

        The conductor / backtest harness consults this each bar; on True
        any open BOUNDARY position is force-flattened so higher-priority
        strategies (GAP_FILL at 08:30, OVERNIGHT_DRIFT at 17:00) get a
        clean slate on the next bar.

        Returns False when `fleet_coordination_close_times_ct` is empty,
        so callers that haven't opted in see no behaviour change.
        """
        close_times = self.config.fleet_coordination_close_times_ct
        if not close_times:
            return False
        from acme.levels import CT
        ct = bar.t.astimezone(CT).time()
        bar_min = ct.hour * 60 + ct.minute
        # 2-min bars: trigger on the bar whose start <= close_time < start + 2 min.
        # In practice this means we fire on the bar that *contains* the close
        # time. e.g. close_time=16:59 → fires on the bar starting at 16:58
        # (which runs 16:58–17:00 CT). After force-flat, OVERNIGHT_DRIFT's
        # on_bar sees bar at 17:00 CT with the strategy flat.
        timeframe = self.timeframe_minutes
        for close_t in close_times:
            close_min = close_t.hour * 60 + close_t.minute
            if bar_min <= close_min < bar_min + timeframe:
                return True
        return False

    # ───────────────────────── helpers ─────────────────────────

    def _classes_allowed_at(self, entry_hour: int) -> set[str]:
        blk = self.config.level_class_hour_blacklist_ct
        return {c for c in self.config.level_classes_enabled
                if entry_hour not in blk.get(c, ())}

    def _nearest_high_side(self, price: float, entry_hour: int) -> tuple[str | None, float | None]:
        allowed = self._classes_allowed_at(entry_hour)
        names = tuple(self._high_by_class[c] for c in allowed)
        return self._nearest_among(price, names, above_only=True)

    def _nearest_low_side(self, price: float, entry_hour: int) -> tuple[str | None, float | None]:
        allowed = self._classes_allowed_at(entry_hour)
        names = tuple(self._low_by_class[c] for c in allowed)
        return self._nearest_among(price, names, above_only=False)

    def _nearest_among(
        self, price: float, names: tuple[str, ...], *, above_only: bool,
    ) -> tuple[str | None, float | None]:
        if self._levels is None:
            return None, None
        best_name = None
        best_val = None
        for nm in names:
            lvl = getattr(self._levels, nm, None)
            if lvl is None:
                continue
            if above_only and lvl < price:
                continue        # high-side: skip levels below price
            if (not above_only) and lvl > price:
                continue        # low-side: skip levels above price
            if best_val is None or abs(lvl - price) < abs(best_val - price):
                best_name, best_val = nm, lvl
        return best_name, best_val

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
        exh = self._exh.update(bar)
        self._atr.update(bar)

        # Stay quiet while in position. BOUNDARY exits are handled by the
        # bracket (stop = level + buffer; target = level ± distance).
        if current_position != 0:
            return None

        if exh is None or self._levels is None:
            return None

        from acme.levels import CT
        entry_hour = bar.t.astimezone(CT).hour

        # Entry-hour blacklist (CT). Skip RTH-volume hours where levels
        # get broken and exhaustion patterns false-fire. Backtest finding
        # 2026-05-12: 09-13 CT is -$712 over 2 years; 03-08 + 17-23 CT
        # carries the edge.
        if entry_hour in self.config.entry_hour_blacklist_ct:
            return None

        buffer_pts = self.config.level_buffer_ticks * self.contract.tick_size

        if exh.direction == "top" and self.config.allow_shorts:
            name, lvl = self._nearest_high_side(bar.h, entry_hour)
            if name is None or lvl is None:
                return None
            if abs(lvl - bar.h) > buffer_pts:
                return None
            stop_distance = (lvl + self.config.stop_buffer_ticks * self.contract.tick_size) - bar.c
            target_distance = self.config.target_distance_ticks * self.contract.tick_size
            return self._build_signal(
                side="sell", state=state, profile=profile,
                current_position=current_position,
                current_balance_unrealized=current_balance_unrealized,
                stop_distance=stop_distance, target_distance=target_distance,
                reason=f"boundary_fade_{name}",
            )

        if exh.direction == "bottom" and self.config.allow_longs:
            name, lvl = self._nearest_low_side(bar.l, entry_hour)
            if name is None or lvl is None:
                return None
            if abs(lvl - bar.l) > buffer_pts:
                return None
            stop_distance = bar.c - (lvl - self.config.stop_buffer_ticks * self.contract.tick_size)
            target_distance = self.config.target_distance_ticks * self.contract.tick_size
            return self._build_signal(
                side="buy", state=state, profile=profile,
                current_position=current_position,
                current_balance_unrealized=current_balance_unrealized,
                stop_distance=stop_distance, target_distance=target_distance,
                reason=f"boundary_fade_{name}",
            )

        return None

    # ───────────────────────── signal construction ──────────────

    def _build_signal(
        self, *, side: str, state: DailyState, profile: EvalProfile,
        current_position: int, current_balance_unrealized: float,
        stop_distance: float, target_distance: float, reason: str,
    ) -> Signal | None:
        if stop_distance <= 0 or target_distance <= 0:
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
            side=side,  # type: ignore[arg-type]
            size=size,
            bracket=BracketSpec(
                stop_loss_offset_ticks=stop_ticks,
                take_profit_offset_ticks=target_ticks,
            ),
            reason=reason,
        )
