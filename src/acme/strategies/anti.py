"""Linda Raschke's "The Anti" — trend-pullback entry on a stochastic hook.

Spec (from Street Smarts, Raschke + Connors 1996, adapted to mechanical form):

  1. Establish trend with EMA(20) slope.
       - Slope > 0 (last close > EMA-3-bars-ago) → uptrend, look for longs only.
       - Slope < 0 → downtrend, look for shorts only.

  2. Wait for a small counter-trend pullback signaled by stochastic rolling over:
       - Slow stoch (k_period=14, d=3) %K crosses BELOW 75 in an uptrend (or ABOVE
         25 in a downtrend) — i.e. fast pullback against the trend.

  3. Entry trigger when the fast stoch (k_period=5, d=3, smoothing=3) %K hooks back
     up across its %D in the trend direction, confirming pullback exhaustion.

  4. Stop: prior swing low (last 5-bar low) for longs / high for shorts.
     Target: 1.5R from entry.

5-minute bars by design — Raschke designed Anti for 5-15 min charts. Less
frequent than the EMA crossover but typically higher hit rate when trends
are well-defined.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Literal

from acme.broker.base import Bar, BracketSpec
from acme.contracts import MES, FuturesContract
from acme.indicators import EMA, Stochastic
from acme.risk import DailyState, EvalProfile, can_open_new_position, dollars_to_contracts
from acme.strategies.base import Signal, StrategyMetadata
from acme.strategies.params import ParameterSpec

TrendDirection = Literal["up", "down", "none"]


@dataclass
class AntiConfig:
    trend_ema_period: int = 20
    trend_lookback_bars: int = 3
    fast_k_period: int = 5
    fast_k_smoothing: int = 3
    fast_d_period: int = 3
    slow_k_period: int = 14
    slow_d_period: int = 3
    stoch_overbought: float = 75.0
    stoch_oversold: float = 25.0
    swing_lookback: int = 5
    target_r_multiple: float = 1.5
    risk_dollars_per_trade: float = 25.0


class AntiStrategy:
    name = "anti"
    version = "1"
    metadata = StrategyMetadata(
        tier=1,
        regime_fit={"trending": 1.0, "ranging": 0.2, "volatile": 0.5, "quiet": 0.6},
        time_buckets=["09:00-14:00"],
        default_lifecycle="SHADOW",
        timeframe_minutes=5,
    )

    @classmethod
    def tunable_params(cls) -> list[ParameterSpec]:
        return [
            ParameterSpec("trend_ema_period", int, 20, 5, 50, 1, "Trend EMA period"),
            ParameterSpec("trend_lookback_bars", int, 3, 2, 10, 1, "Bars to confirm trend slope"),
            ParameterSpec("fast_k_period", int, 5, 3, 15, 1, "Fast stochastic %K period"),
            ParameterSpec("slow_k_period", int, 14, 8, 30, 1, "Slow stochastic %K period"),
            ParameterSpec("stoch_overbought", float, 75.0, 60.0, 90.0, 1.0, "Slow stoch overbought level"),
            ParameterSpec("stoch_oversold", float, 25.0, 10.0, 40.0, 1.0, "Slow stoch oversold level"),
            ParameterSpec("swing_lookback", int, 5, 3, 15, 1, "Bars for swing-based stop"),
            ParameterSpec("target_r_multiple", float, 1.5, 1.0, 3.0, 0.1, "Target as R multiple"),
            ParameterSpec("risk_dollars_per_trade", float, 25.0, 10.0, 200.0, 5.0, "Risk per trade ($)"),
        ]

    def __init__(self, config: AntiConfig | None = None, contract: FuturesContract = MES) -> None:
        self.config = config or AntiConfig()
        self.contract = contract
        self.timeframe_minutes = self.metadata.timeframe_minutes
        self._trend_ema = EMA(self.config.trend_ema_period)
        self._fast_stoch = Stochastic(
            k_period=self.config.fast_k_period,
            k_smoothing=self.config.fast_k_smoothing,
            d_period=self.config.fast_d_period,
        )
        self._slow_stoch = Stochastic(
            k_period=self.config.slow_k_period,
            k_smoothing=1,
            d_period=self.config.slow_d_period,
        )
        # State for slow-stoch pullback detection
        self._slow_k_crossed_overbought_recently = False
        self._slow_k_crossed_oversold_recently = False
        self._prev_slow_k: float | None = None
        self._prev_fast_k: float | None = None
        self._prev_fast_d: float | None = None
        # Closing-price ring for trend-slope check
        self._closes: deque[float] = deque(maxlen=self.config.trend_lookback_bars + 1)
        # Bar ring for swing-low/high stop placement
        self._bars: deque[Bar] = deque(maxlen=self.config.swing_lookback + 1)

    def required_history_bars(self) -> int:
        # slow stoch needs 14 + d-smoothing 3 = 17; trend ema needs ~20; safety margin
        return max(self.config.slow_k_period + self.config.slow_d_period,
                   self.config.trend_ema_period) * 2

    def _trend_direction(self, current_close: float) -> TrendDirection:
        if not self._trend_ema.is_warm or len(self._closes) <= self.config.trend_lookback_bars:
            return "none"
        prior = self._closes[0]   # oldest in the ring
        if current_close > prior and self._trend_ema.value is not None and current_close > self._trend_ema.value:
            return "up"
        if current_close < prior and self._trend_ema.value is not None and current_close < self._trend_ema.value:
            return "down"
        return "none"

    def on_bar(
        self,
        bar: Bar,
        *,
        state: DailyState,
        profile: EvalProfile,
        current_position: int,
        current_balance_unrealized: float,
    ) -> Signal | None:
        # Update indicators
        self._trend_ema.update(bar.c)
        slow_out = self._slow_stoch.update(bar)
        fast_out = self._fast_stoch.update(bar)
        self._closes.append(bar.c)
        self._bars.append(bar)

        if slow_out is None or fast_out is None:
            return None

        # Detect slow-stoch crossing into pullback territory
        if self._prev_slow_k is not None:
            if (self._prev_slow_k >= self.config.stoch_overbought
                    and slow_out.k < self.config.stoch_overbought):
                self._slow_k_crossed_overbought_recently = True
            if (self._prev_slow_k <= self.config.stoch_oversold
                    and slow_out.k > self.config.stoch_oversold):
                self._slow_k_crossed_oversold_recently = True

        trend = self._trend_direction(bar.c)

        # Detect fast-stoch hook (entry trigger)
        long_hook = (
            self._prev_fast_k is not None and self._prev_fast_d is not None
            and self._prev_fast_k < self._prev_fast_d
            and fast_out.k > fast_out.d
        )
        short_hook = (
            self._prev_fast_k is not None and self._prev_fast_d is not None
            and self._prev_fast_k > self._prev_fast_d
            and fast_out.k < fast_out.d
        )

        # Persist for next bar
        self._prev_slow_k = slow_out.k
        self._prev_fast_k = fast_out.k
        self._prev_fast_d = fast_out.d

        if current_position != 0:
            return None

        signal_side: Literal["buy", "sell"] | None = None
        if (trend == "up"
                and self._slow_k_crossed_overbought_recently
                and long_hook):
            signal_side = "buy"
            self._slow_k_crossed_overbought_recently = False
        elif (trend == "down"
                and self._slow_k_crossed_oversold_recently
                and short_hook):
            signal_side = "sell"
            self._slow_k_crossed_oversold_recently = False

        if signal_side is None:
            return None

        # Stop placement: prior swing low/high over the lookback
        prior_bars = list(self._bars)[:-1]   # exclude current bar
        if not prior_bars:
            return None
        if signal_side == "buy":
            stop_price = min(b.l for b in prior_bars)
            stop_distance_points = bar.c - stop_price
        else:
            stop_price = max(b.h for b in prior_bars)
            stop_distance_points = stop_price - bar.c

        if stop_distance_points <= 0:
            return None

        target_distance_points = stop_distance_points * self.config.target_r_multiple
        # Convert to ticks for the bracket spec (standard for our broker adapter)
        stop_ticks = max(1, int(round(stop_distance_points / self.contract.tick_size)))
        target_ticks = max(1, int(round(target_distance_points / self.contract.tick_size)))

        round_turn_fee = profile.round_turn_fees.get(self.contract.symbol, 0.0)
        size = dollars_to_contracts(
            self.config.risk_dollars_per_trade,
            stop_distance_points,
            self.contract.point_value,
            round_turn_fee=round_turn_fee,
        )
        if size <= 0:
            return None

        allowed, reason = can_open_new_position(
            profile, state, current_balance_unrealized,
            self.contract.symbol, size, current_position,
        )
        if not allowed:
            return Signal(side=signal_side, size=0, reason=f"blocked: {reason}")

        return Signal(
            side=signal_side,
            size=size,
            bracket=BracketSpec(
                stop_loss_offset_ticks=stop_ticks,
                take_profit_offset_ticks=target_ticks,
            ),
            reason=f"anti_{trend}_pullback",
        )
