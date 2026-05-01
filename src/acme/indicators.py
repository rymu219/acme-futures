"""Pure-Python streaming indicators used by the seed-fleet strategies.

All indicators are stateful classes with an `update(...)` method that takes
the new bar (or just close, for some) and returns the current value (or None
during warmup). They're designed to be called once per bar in `Strategy.on_bar`.

No numpy / pandas dependency — keeps the runtime footprint small and avoids
import-time overhead in the per-bar hot path. For B4's backtest harness we
might switch to vectorized variants, but live trading is one-bar-at-a-time.

Each indicator's `is_warm` returns True once it has enough samples to produce
a valid output. Strategies should check this before acting.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from acme.broker.base import Bar

# ---------- moving averages ----------

class EMA:
    """Exponential moving average. update() takes the new value, returns the EMA
    or None until at least `period` values have been seen.
    """

    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self._k = 2.0 / (period + 1.0)
        self._value: float | None = None
        self._n = 0

    def update(self, x: float) -> float | None:
        self._n += 1
        if self._value is None:
            self._value = x
        else:
            self._value = x * self._k + self._value * (1.0 - self._k)
        return self.value

    @property
    def value(self) -> float | None:
        return self._value if self.is_warm else None

    @property
    def is_warm(self) -> bool:
        return self._n >= self.period


class SMA:
    """Simple moving average over the last `period` values."""

    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self._buf: deque[float] = deque(maxlen=period)
        self._sum = 0.0

    def update(self, x: float) -> float | None:
        if len(self._buf) == self.period:
            self._sum -= self._buf[0]
        self._buf.append(x)
        self._sum += x
        return self.value

    @property
    def value(self) -> float | None:
        return self._sum / self.period if self.is_warm else None

    @property
    def is_warm(self) -> bool:
        return len(self._buf) == self.period


# ---------- volatility ----------

class ATR:
    """Average True Range over `period` bars. Uses Wilder's smoothing
    (equivalent to an EMA with alpha = 1/period).
    """

    def __init__(self, period: int = 14) -> None:
        self.period = period
        self._prev_close: float | None = None
        self._value: float | None = None
        self._n = 0

    def update(self, bar: Bar) -> float | None:
        if self._prev_close is None:
            tr = bar.h - bar.l
        else:
            tr = max(
                bar.h - bar.l,
                abs(bar.h - self._prev_close),
                abs(bar.l - self._prev_close),
            )
        self._prev_close = bar.c
        self._n += 1
        if self._value is None:
            self._value = tr
        else:
            self._value = (self._value * (self.period - 1) + tr) / self.period
        return self.value

    @property
    def value(self) -> float | None:
        return self._value if self.is_warm else None

    @property
    def is_warm(self) -> bool:
        return self._n >= self.period


# ---------- momentum ----------

class RSI:
    """Relative Strength Index over `period` closes. Uses Wilder's smoothing."""

    def __init__(self, period: int = 14) -> None:
        if period < 2:
            raise ValueError("period must be >= 2")
        self.period = period
        self._prev_close: float | None = None
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None
        self._n = 0

    def update(self, close: float) -> float | None:
        if self._prev_close is None:
            self._prev_close = close
            return None
        change = close - self._prev_close
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        self._n += 1
        if self._avg_gain is None:
            self._avg_gain = gain
            self._avg_loss = loss
        else:
            self._avg_gain = (self._avg_gain * (self.period - 1) + gain) / self.period
            self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period
        self._prev_close = close
        return self.value

    @property
    def value(self) -> float | None:
        if not self.is_warm or self._avg_gain is None or self._avg_loss is None:
            return None
        if self._avg_loss == 0:
            return 100.0
        rs = self._avg_gain / self._avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    @property
    def is_warm(self) -> bool:
        return self._n >= self.period


@dataclass
class StochasticOutput:
    k: float           # %K (raw)
    d: float           # %D (smoothed %K)


class Stochastic:
    """Stochastic oscillator (%K, %D).

    %K = 100 * (close - lowest_low_n) / (highest_high_n - lowest_low_n)
    %D = SMA(%K, smoothing)

    `k_period` is the lookback for highest/lowest.
    `k_smoothing` is an optional SMA on raw %K (1 = no smoothing).
    `d_period` is the SMA period for %D.
    """

    def __init__(self, k_period: int = 14, k_smoothing: int = 1, d_period: int = 3) -> None:
        self.k_period = k_period
        self._highs: deque[float] = deque(maxlen=k_period)
        self._lows: deque[float] = deque(maxlen=k_period)
        self._k_smoother = SMA(k_smoothing) if k_smoothing > 1 else None
        self._d_smoother = SMA(d_period)
        self._last: StochasticOutput | None = None

    def update(self, bar: Bar) -> StochasticOutput | None:
        self._highs.append(bar.h)
        self._lows.append(bar.l)
        if len(self._highs) < self.k_period:
            return None
        hi = max(self._highs)
        lo = min(self._lows)
        raw_k = 50.0 if hi == lo else 100.0 * (bar.c - lo) / (hi - lo)
        k = self._k_smoother.update(raw_k) if self._k_smoother else raw_k
        if k is None:
            return None
        d = self._d_smoother.update(k)
        if d is None:
            return None
        self._last = StochasticOutput(k=k, d=d)
        return self._last

    @property
    def value(self) -> StochasticOutput | None:
        return self._last

    @property
    def is_warm(self) -> bool:
        return self._last is not None


# ---------- bands ----------

@dataclass
class BollingerOutput:
    middle: float       # SMA
    upper: float
    lower: float

    @property
    def width(self) -> float:
        return self.upper - self.lower


class Bollinger:
    """Bollinger Bands: SMA(period) ± num_std * stddev(period)."""

    def __init__(self, period: int = 20, num_std: float = 2.0) -> None:
        self.period = period
        self.num_std = num_std
        self._buf: deque[float] = deque(maxlen=period)

    def update(self, close: float) -> BollingerOutput | None:
        self._buf.append(close)
        if len(self._buf) < self.period:
            return None
        m = sum(self._buf) / self.period
        var = sum((x - m) ** 2 for x in self._buf) / self.period
        sd = var ** 0.5
        return BollingerOutput(
            middle=m, upper=m + self.num_std * sd, lower=m - self.num_std * sd,
        )

    @property
    def is_warm(self) -> bool:
        return len(self._buf) == self.period


# ---------- trend strength ----------

@dataclass
class _ADXState:
    prev_high: float | None = None
    prev_low: float | None = None
    prev_close: float | None = None
    smoothed_tr: float | None = None
    smoothed_pdm: float | None = None
    smoothed_mdm: float | None = None
    di_history: list[float] = field(default_factory=list)
    adx: float | None = None


class ADX:
    """Average Directional Index over `period` bars. Wilder's smoothing.

    Indicates trend strength (not direction). Common thresholds:
      ADX < 20 → ranging / choppy
      ADX > 25 → trending
    """

    def __init__(self, period: int = 14) -> None:
        self.period = period
        self.s = _ADXState()
        self._n = 0

    def update(self, bar: Bar) -> float | None:
        self._n += 1
        s = self.s
        if s.prev_close is None:
            s.prev_high, s.prev_low, s.prev_close = bar.h, bar.l, bar.c
            return None

        tr = max(
            bar.h - bar.l,
            abs(bar.h - s.prev_close),
            abs(bar.l - s.prev_close),
        )
        up_move = bar.h - s.prev_high
        down_move = s.prev_low - bar.l
        pdm = up_move if (up_move > down_move and up_move > 0) else 0.0
        mdm = down_move if (down_move > up_move and down_move > 0) else 0.0

        if s.smoothed_tr is None:
            s.smoothed_tr = tr
            s.smoothed_pdm = pdm
            s.smoothed_mdm = mdm
        else:
            s.smoothed_tr = (s.smoothed_tr * (self.period - 1) + tr) / self.period
            s.smoothed_pdm = (s.smoothed_pdm * (self.period - 1) + pdm) / self.period
            s.smoothed_mdm = (s.smoothed_mdm * (self.period - 1) + mdm) / self.period

        s.prev_high, s.prev_low, s.prev_close = bar.h, bar.l, bar.c

        if s.smoothed_tr is None or s.smoothed_tr == 0:
            return None
        plus_di = 100.0 * (s.smoothed_pdm or 0) / s.smoothed_tr
        minus_di = 100.0 * (s.smoothed_mdm or 0) / s.smoothed_tr
        di_sum = plus_di + minus_di
        dx = 0.0 if di_sum == 0 else 100.0 * abs(plus_di - minus_di) / di_sum
        s.di_history.append(dx)
        if len(s.di_history) < self.period:
            return None
        if s.adx is None:
            s.adx = sum(s.di_history[-self.period:]) / self.period
        else:
            s.adx = (s.adx * (self.period - 1) + dx) / self.period
        return s.adx

    @property
    def value(self) -> float | None:
        return self.s.adx

    @property
    def is_warm(self) -> bool:
        return self.s.adx is not None


# ---------- supertrend ----------

@dataclass
class SupertrendOutput:
    line: float                # current Supertrend line (acts as trailing stop)
    trend: int                 # +1 (up) or -1 (down)
    flipped: bool              # True only on the bar where trend changed


class Supertrend:
    """Supertrend trend-flip indicator (HL2 ± multiplier × ATR with band-locking).

    Update sequence each bar:
      hl2          = (high + low) / 2
      basic_upper  = hl2 + multiplier * atr
      basic_lower  = hl2 - multiplier * atr
      final_upper  = basic_upper if (basic_upper < prev_final_upper or prev_close > prev_final_upper) else prev_final_upper
      final_lower  = basic_lower if (basic_lower > prev_final_lower or prev_close < prev_final_lower) else prev_final_lower
      trend        = +1 if close > prev_final_upper
                     -1 if close < prev_final_lower
                     else prev_trend (carry)

    Warmup: ATR must be warm AND we need one prior bar's final bands. Returns None
    until both conditions are met.
    """

    def __init__(self, period: int = 10, multiplier: float = 3.0) -> None:
        if period < 1:
            raise ValueError("period must be >= 1")
        if multiplier <= 0:
            raise ValueError("multiplier must be > 0")
        self.period = period
        self.multiplier = multiplier
        self._atr = ATR(period)
        self._prev_close: float | None = None
        self._prev_final_upper: float | None = None
        self._prev_final_lower: float | None = None
        self._trend: int | None = None
        self._last: SupertrendOutput | None = None

    def update(self, bar: Bar) -> SupertrendOutput | None:
        atr_val = self._atr.update(bar)
        if atr_val is None:
            self._prev_close = bar.c
            return None

        hl2 = (bar.h + bar.l) / 2.0
        basic_upper = hl2 + self.multiplier * atr_val
        basic_lower = hl2 - self.multiplier * atr_val

        if self._prev_final_upper is None or self._prev_final_lower is None:
            final_upper = basic_upper
            final_lower = basic_lower
            self._prev_final_upper = final_upper
            self._prev_final_lower = final_lower
            self._prev_close = bar.c
            return None

        prev_close = self._prev_close if self._prev_close is not None else bar.c

        if basic_upper < self._prev_final_upper or prev_close > self._prev_final_upper:
            final_upper = basic_upper
        else:
            final_upper = self._prev_final_upper

        if basic_lower > self._prev_final_lower or prev_close < self._prev_final_lower:
            final_lower = basic_lower
        else:
            final_lower = self._prev_final_lower

        prev_trend = self._trend
        if bar.c > self._prev_final_upper:
            new_trend: int | None = 1
        elif bar.c < self._prev_final_lower:
            new_trend = -1
        else:
            new_trend = prev_trend  # carry — may still be None during early bars

        self._prev_final_upper = final_upper
        self._prev_final_lower = final_lower
        self._prev_close = bar.c

        if new_trend is None:
            return None  # haven't crossed either band yet — trend undetermined

        flipped = prev_trend is not None and new_trend != prev_trend
        self._trend = new_trend
        line = final_lower if new_trend == 1 else final_upper

        self._last = SupertrendOutput(line=line, trend=new_trend, flipped=flipped)
        return self._last

    @property
    def value(self) -> SupertrendOutput | None:
        return self._last

    @property
    def is_warm(self) -> bool:
        return self._last is not None
