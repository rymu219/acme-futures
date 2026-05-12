"""REGIME — vol-state + trend-aligned strategy.

Composition:
  - **Vol classifier** (compression / normal / expansion) via
    [`pulse_gates.VolRegimeClassifier`](pulse_gates.py).
  - **Trend classifier** (trend_up / trend_down / chop) via
    [`acme.ryan_spec.v4_regime.classify_trend_ema`](../ryan_spec/v4_regime.py).
  - **Entry**: only fires when vol is in EXPANSION (high) AND trend
    is clear (not chop). Direction follows the trend classifier.
  - **Exit**: when vol regime exits expansion OR when min-2-bar hold
    has elapsed and trend flips, OR ATR-stop hit.

Audit-driven design choice (§7): 11 of 16 v3 variants take their
max-DD in pure chop. Compression-fade ("buy the low-vol breakout
fade") is NOT justified by the data — the original plan's framing
gets replaced with a strict deadband (compression → skip).

Lifecycle: SHADOW. Long-only by default (audit §4); shorts allowed via config.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from acme.broker.base import Bar, BracketSpec
from acme.contracts import MES, FuturesContract
from acme.indicators import ATR
from acme.risk import DailyState, EvalProfile, can_open_new_position, dollars_to_contracts
from acme.ryan_spec.v4_regime import DEFAULT_LOOKBACK_BARS, classify_trend_ema
from acme.strategies.base import Signal, StrategyMetadata
from acme.strategies.params import ParameterSpec
from acme.strategies.pulse_gates import VolRegimeClassifier


# 60 bars = 120 min of 2-min bars. Enough for the trend_ema classifier
# (EMA(20) on a 60-bar window).
DEFAULT_TREND_BUFFER_BARS = 60


@dataclass
class RegimeConfig:
    # Vol regime thresholds (from PULSE Pine defaults). high → expansion;
    # low → compression; in-between → normal (also skipped).
    atr_period: int = 4
    atr_avg_period: int = 20
    vol_high_ratio: float = 1.5     # vol/avg > this → expansion
    vol_low_ratio: float = 0.8      # vol/avg < this → compression
    # Trend classifier buffer
    trend_buffer_bars: int = DEFAULT_TREND_BUFFER_BARS
    trend_lookback_bars: int = DEFAULT_LOOKBACK_BARS
    # Exit policy
    min_bars_before_opposite_exit: int = 2
    atr_stop_multiple: float = 1.5
    atr_target_multiple: float = 2.5
    # Sizing & direction
    risk_dollars_per_trade: float = 25.0
    allow_longs: bool = True
    allow_shorts: bool = False
    # Exit when vol regime drops out of expansion (the entry thesis is
    # gone). Setting this to False relies only on the bracket stop.
    exit_on_vol_normalization: bool = True


class RegimeStrategy:
    name = "regime"
    version = "1"
    metadata = StrategyMetadata(
        tier=2,
        regime_fit={"trending": 1.0, "volatile": 1.0, "ranging": 0.0, "quiet": 0.0},
        time_buckets=[],
        default_lifecycle="SHADOW",
        timeframe_minutes=2,
    )

    @classmethod
    def tunable_params(cls) -> list[ParameterSpec]:
        return [
            ParameterSpec("vol_high_ratio", float, 1.5, 1.1, 3.0, 0.05,
                          "ATR / SMA(ATR) ratio that defines expansion"),
            ParameterSpec("min_bars_before_opposite_exit", int, 2, 1, 5, 1,
                          "Min bars held before opposite-trend exit"),
            ParameterSpec("atr_stop_multiple", float, 1.5, 0.5, 4.0, 0.1,
                          "ATR multiple for stop loss"),
            ParameterSpec("atr_target_multiple", float, 2.5, 1.0, 6.0, 0.1,
                          "ATR multiple for take profit"),
            ParameterSpec("risk_dollars_per_trade", float, 25.0, 10.0, 200.0, 5.0,
                          "Risk per trade ($)"),
        ]

    def __init__(
        self,
        config: RegimeConfig | None = None,
        contract: FuturesContract = MES,
    ) -> None:
        self.config = config or RegimeConfig()
        self.contract = contract
        self.timeframe_minutes = self.metadata.timeframe_minutes
        self._vol = VolRegimeClassifier(
            atr_period=self.config.atr_period,
            atr_avg_period=self.config.atr_avg_period,
            high_ratio=self.config.vol_high_ratio,
            low_ratio=self.config.vol_low_ratio,
        )
        self._atr = ATR(self.config.atr_period)
        self._trend_buffer: deque[Bar] = deque(maxlen=self.config.trend_buffer_bars)
        # bars_held tracking
        self._bars_held = 0
        self._prev_position = 0

    def required_history_bars(self) -> int:
        return max(
            self.config.atr_avg_period,
            self.config.trend_buffer_bars,
        ) + 5

    # ───────────────────────── helpers ─────────────────────────

    def _update_bars_held(self, current_position: int) -> None:
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
        vol = self._vol.update(bar)
        self._atr.update(bar)
        self._trend_buffer.append(bar)
        self._update_bars_held(current_position)

        if vol is None or not self._atr.is_warm:
            return None

        trend = classify_trend_ema(
            list(self._trend_buffer),
            lookback_bars=self.config.trend_lookback_bars,
        )

        # ────── Exit path ──────
        if current_position != 0:
            holding_long = current_position > 0

            # Exit 1: vol regime no longer expansion → entry thesis gone
            if self.config.exit_on_vol_normalization and vol.label != "high":
                if self._bars_held >= self.config.min_bars_before_opposite_exit:
                    side = "sell" if holding_long else "buy"
                    return self._build_signal(
                        bar=bar, side=side, state=state, profile=profile,
                        current_position=current_position,
                        current_balance_unrealized=current_balance_unrealized,
                        reason="regime_vol_normalized",
                    )

            # Exit 2: trend flipped — but only if min-2-bar elapsed
            if self._bars_held >= self.config.min_bars_before_opposite_exit:
                if holding_long and trend == "trend_down":
                    return self._build_signal(
                        bar=bar, side="sell", state=state, profile=profile,
                        current_position=current_position,
                        current_balance_unrealized=current_balance_unrealized,
                        reason="regime_trend_flip",
                    )
                if not holding_long and trend == "trend_up":
                    return self._build_signal(
                        bar=bar, side="buy", state=state, profile=profile,
                        current_position=current_position,
                        current_balance_unrealized=current_balance_unrealized,
                        reason="regime_trend_flip",
                    )
            return None

        # ────── Entry path ──────
        if vol.label != "high":          # compression / normal → SKIP (audit §7)
            return None
        if trend == "chop":
            return None
        if trend == "trend_up" and self.config.allow_longs:
            side = "buy"
        elif trend == "trend_down" and self.config.allow_shorts:
            side = "sell"
        else:
            return None

        return self._build_signal(
            bar=bar, side=side, state=state, profile=profile,
            current_position=current_position,
            current_balance_unrealized=current_balance_unrealized,
            reason=f"regime_entry_{trend}_in_expansion",
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
            side=side,  # type: ignore[arg-type]
            size=size,
            bracket=BracketSpec(
                stop_loss_offset_ticks=stop_ticks,
                take_profit_offset_ticks=target_ticks,
            ),
            reason=reason,
        )
