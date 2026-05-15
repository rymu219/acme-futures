"""IGNITION wired to PulseFeatureEngine — backtest-only test variant.

This file exists solely to answer one binary question: does wiring
PULSE to IGNITION produce materially better results than GO/NO-GO
alone? It is NOT registered with the `strategies` table and NOT bound
by `fleet_runner.py`.

Construction:
  Take the IGNITION strategy AS-IS. Swap the entry filter:
    - was:  GoNoGoEngine.update(bar).signal     (∈ {+1, -1, 0})
    - now:  PulseFeatureEngine.update(bar) → entry rule on (edge, p_long, p_short)

Entry rule (per the Phase 3 spec):
    if pulse_feat.edge > eci_threshold (default 0.5):
        +1 if p_long > p_short else -1
    else:
        0

Everything else identical to IGNITION:
  - Long-only by default (`allow_shorts=False`) — matches `IgnitionConfig`
  - min-2-bar opposite-exit gate — matches IgnitionConfig.min_bars_before_opposite_exit=2
  - Stop = 1.5 × ATR, target = 2.5 × ATR
  - Risk = $25 per trade
  - Time windows empty by default; Phase 3 RUN B sets ((08, 12),) explicitly
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo

from acme.broker.base import Bar, BracketSpec
from acme.contracts import MES, FuturesContract
from acme.indicators import ATR
from acme.risk import DailyState, EvalProfile, can_open_new_position, dollars_to_contracts
from acme.strategies.base import Signal, StrategyMetadata
from acme.strategies.params import ParameterSpec
from acme.strategies.pulse_features import PulseFeatureConfig, PulseFeatureEngine, PulseFeatures

CT = ZoneInfo("America/Chicago")


@dataclass(frozen=True)
class IgnitionPulseTimeWindow:
    start: time
    end: time

    def contains(self, ts: datetime) -> bool:
        ct = ts.astimezone(CT).time()
        return self.start <= ct < self.end

    @classmethod
    def from_hours(cls, start_hour: int, end_hour: int) -> IgnitionPulseTimeWindow:
        return cls(time(start_hour, 0), time(end_hour, 0))


@dataclass
class IgnitionPulseConfig:
    """Defaults mirror `IgnitionConfig` exactly EXCEPT the entry filter
    (PULSE replaces GO/NO-GO) and the addition of `eci_threshold`."""
    pulse: PulseFeatureConfig = field(default_factory=PulseFeatureConfig)

    # ECI / edge threshold — per spec, default 0.5 ("expansion, entries active")
    eci_threshold: float = 0.5

    # Time gating — same shape as IgnitionConfig.time_windows
    time_windows: tuple[IgnitionPulseTimeWindow, ...] = ()

    # Exit policy — copied from IgnitionConfig
    min_bars_before_opposite_exit: int = 2

    # Bracket / sizing — copied from IgnitionConfig
    atr_period: int = 4
    atr_stop_multiple: float = 1.5
    atr_target_multiple: float = 2.5

    # Direction policy — copied from IgnitionConfig (audit §4: long-only default)
    allow_longs: bool = True
    allow_shorts: bool = False

    # Sizing — copied from IgnitionConfig
    risk_dollars_per_trade: float = 25.0


class IgnitionPulseTestStrategy:
    """IGNITION with PULSE as the entry filter. Backtest-only test variant."""

    name = "ignition_pulse"
    version = "1"
    metadata = StrategyMetadata(
        tier=2,
        regime_fit={"trending": 1.0, "volatile": 0.7, "ranging": 0.3, "quiet": 0.2},
        time_buckets=[],
        default_lifecycle="SHADOW",
        timeframe_minutes=2,
    )

    @classmethod
    def tunable_params(cls) -> list[ParameterSpec]:
        return [
            ParameterSpec("eci_threshold", float, 0.5, 0.0, 0.95, 0.05,
                          "PULSE edge threshold — entries require edge > this"),
            ParameterSpec("min_bars_before_opposite_exit", int, 2, 1, 5, 1,
                          "Minimum bars held before opposite-signal exit fires"),
            ParameterSpec("atr_stop_multiple", float, 1.5, 0.5, 4.0, 0.1,
                          "ATR multiple for stop loss"),
            ParameterSpec("atr_target_multiple", float, 2.5, 1.0, 6.0, 0.1,
                          "ATR multiple for take profit"),
            ParameterSpec("risk_dollars_per_trade", float, 25.0, 10.0, 200.0, 5.0,
                          "Risk per trade ($)"),
        ]

    def __init__(
        self,
        config: IgnitionPulseConfig | None = None,
        contract: FuturesContract = MES,
    ) -> None:
        self.config = config or IgnitionPulseConfig()
        self.contract = contract
        self.timeframe_minutes = self.metadata.timeframe_minutes
        self._pulse = PulseFeatureEngine(self.config.pulse)
        self._atr = ATR(self.config.atr_period)
        # Mirrors IgnitionStrategy._bars_held for the min-2-bar opposite-exit gate
        self._bars_held = 0
        self._prev_position = 0

    def required_history_bars(self) -> int:
        return max(
            self._pulse.required_history_bars(),
            self.config.atr_period,
        ) + 2

    # ───────────────────────── helpers ─────────────────────────

    def _in_window(self, ts: datetime) -> bool:
        if not self.config.time_windows:
            return True
        return any(w.contains(ts) for w in self.config.time_windows)

    def _pulse_to_signal(self, feat: PulseFeatures) -> int:
        """ECI / edge → directional signal. The spec for Phase 3:
            edge > eci_threshold and p_long  > p_short  → +1
            edge > eci_threshold and p_short > p_long   → -1
            otherwise                                   →  0
        """
        if feat.edge <= self.config.eci_threshold:
            return 0
        if feat.p_long > feat.p_short:
            return 1
        if feat.p_short > feat.p_long:
            return -1
        return 0

    def _update_bars_held(self, current_position: int) -> None:
        """Identical to IgnitionStrategy._update_bars_held."""
        if current_position == 0:
            self._bars_held = 0
        elif self._prev_position == 0 and current_position != 0:
            self._bars_held = 1
        else:
            self._bars_held += 1
        self._prev_position = current_position

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
        feat = self._pulse.update(bar)
        self._atr.update(bar)
        self._update_bars_held(current_position)

        if feat is None or not self._atr.is_warm:
            return None

        signal_raw = self._pulse_to_signal(feat)

        # ────── Exit / reversal path: we're already in a position ──────
        if current_position != 0:
            if signal_raw == 0:
                return None
            holding_long = current_position > 0
            new_long = signal_raw == 1
            if holding_long == new_long:
                return None
            # Opposite signal — apply IGNITION's min-bars gate.
            if self._bars_held < self.config.min_bars_before_opposite_exit:
                return None
            side = "buy" if new_long else "sell"
            return self._build_signal(
                bar=bar, side=side, state=state, profile=profile,
                current_position=current_position,
                current_balance_unrealized=current_balance_unrealized,
                reason="pulse_reverse_after_min_hold",
            )

        # ────── Entry path: we're flat ──────
        if signal_raw == 0:
            return None
        if not self._in_window(bar.t):
            return None
        if signal_raw == 1 and not self.config.allow_longs:
            return None
        if signal_raw == -1 and not self.config.allow_shorts:
            return None

        side = "buy" if signal_raw == 1 else "sell"
        return self._build_signal(
            bar=bar, side=side, state=state, profile=profile,
            current_position=current_position,
            current_balance_unrealized=current_balance_unrealized,
            reason="pulse_entry",
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
