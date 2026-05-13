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

from dataclasses import dataclass

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


# Level names by "side" relative to current price.
# A high-side level (PDH/ONH/ORH) is a resistance candidate — fade short.
# A low-side level (PDL/ONL/ORL) is a support candidate — fade long.
_HIGH_SIDE_LEVELS = ("pdh", "onh", "orh")
_LOW_SIDE_LEVELS = ("pdl", "onl", "orl")


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

    def required_history_bars(self) -> int:
        return max(
            self.config.exh_lookback_bars,
            self.config.exh_volume_avg_period,
            self.config.atr_period,
        ) + 2

    def set_levels(self, levels: DayLevels) -> None:
        """Caller updates day-levels (typically once per trade date)."""
        self._levels = levels

    # ───────────────────────── helpers ─────────────────────────

    def _nearest_high_side(self, price: float) -> tuple[str | None, float | None]:
        return self._nearest_among(price, _HIGH_SIDE_LEVELS, above_only=True)

    def _nearest_low_side(self, price: float) -> tuple[str | None, float | None]:
        return self._nearest_among(price, _LOW_SIDE_LEVELS, above_only=False)

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

        # Entry-hour blacklist (CT). Skip RTH-volume hours where levels
        # get broken and exhaustion patterns false-fire. Backtest finding
        # 2026-05-12: 09-13 CT is -$712 over 2 years; 03-08 + 17-23 CT
        # carries the edge.
        if self.config.entry_hour_blacklist_ct:
            from acme.levels import CT
            entry_hour = bar.t.astimezone(CT).hour
            if entry_hour in self.config.entry_hour_blacklist_ct:
                return None

        buffer_pts = self.config.level_buffer_ticks * self.contract.tick_size

        if exh.direction == "top" and self.config.allow_shorts:
            name, lvl = self._nearest_high_side(bar.h)
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
            name, lvl = self._nearest_low_side(bar.l)
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
