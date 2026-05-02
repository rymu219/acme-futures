"""Regime-engine-specific indicators that aren't in acme.indicators.

Two only:
  - Hurst exponent (rescaled-range method, Mandelbrot & Wallis 1969)
  - BB-width percentile rank (current width's rank within a rolling lookback)

Everything else (ADX, ATR, EMA, SMA, Bollinger, etc.) reuses acme.indicators.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from acme.indicators import Bollinger

_HURST_LAGS = (2, 4, 8, 16, 32)


def _rs_at_lag(values: list[float], lag: int) -> float | None:
    """Average rescaled range over non-overlapping segments of length `lag`."""
    n = len(values)
    if n < lag * 2:
        return None
    n_segments = n // lag
    rs_values: list[float] = []
    for seg_i in range(n_segments):
        start = seg_i * lag
        seg = values[start:start + lag]
        mean = sum(seg) / lag
        deviations = [v - mean for v in seg]
        cum = []
        running = 0.0
        for d in deviations:
            running += d
            cum.append(running)
        rng = max(cum) - min(cum)
        var = sum((v - mean) ** 2 for v in seg) / lag
        std = math.sqrt(var)
        if std <= 0 or rng <= 0:
            continue
        rs_values.append(rng / std)
    if not rs_values:
        return None
    return sum(rs_values) / len(rs_values)


def _hurst_from_log_returns(log_returns: list[float]) -> float | None:
    """Log-log regression slope of mean(R/S) vs lag. Returns Hurst H in (0,1)."""
    points: list[tuple[float, float]] = []
    for lag in _HURST_LAGS:
        rs = _rs_at_lag(log_returns, lag)
        if rs is None or rs <= 0:
            continue
        points.append((math.log(lag), math.log(rs)))
    if len(points) < 3:
        return None
    n = len(points)
    sum_x = sum(p[0] for p in points)
    sum_y = sum(p[1] for p in points)
    sum_xx = sum(p[0] * p[0] for p in points)
    sum_xy = sum(p[0] * p[1] for p in points)
    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        return None
    return (n * sum_xy - sum_x * sum_y) / denom


class Hurst:
    """Rolling Hurst exponent over the last `window` close-to-close log returns.

    H > 0.5  →  trending (persistent)
    H < 0.5  →  mean-reverting (anti-persistent)
    H ≈ 0.5  →  random walk
    """

    def __init__(self, window: int = 100) -> None:
        if window < 64:
            raise ValueError("window must be >= 64 (need enough samples for max lag 32)")
        self.window = window
        self._closes: deque[float] = deque(maxlen=window + 1)
        self._value: float | None = None

    def update(self, close: float) -> float | None:
        self._closes.append(close)
        if len(self._closes) < self.window + 1:
            return None
        log_returns: list[float] = []
        prev = self._closes[0]
        for c in list(self._closes)[1:]:
            if prev <= 0 or c <= 0:
                prev = c
                continue
            log_returns.append(math.log(c / prev))
            prev = c
        self._value = _hurst_from_log_returns(log_returns)
        return self._value

    @property
    def value(self) -> float | None:
        return self._value

    @property
    def is_warm(self) -> bool:
        return self._value is not None


@dataclass
class BBWidthOutput:
    width: float                  # (upper - lower) / middle, normalized
    percentile: float             # 0..1, fraction of lookback bars where width was <= current


class BBWidthPercentile:
    """Tracks Bollinger-band width and its percentile rank within a rolling
    lookback window. Used to detect compression states (current width sits in
    the bottom 20th percentile of recent history → squeeze).
    """

    def __init__(self, period: int = 20, std_mult: float = 2.0, lookback: int = 50) -> None:
        if lookback < 10:
            raise ValueError("lookback must be >= 10")
        self.period = period
        self.lookback = lookback
        self._bb = Bollinger(period=period, num_std=std_mult)
        self._widths: deque[float] = deque(maxlen=lookback)
        self._last: BBWidthOutput | None = None

    def update(self, close: float) -> BBWidthOutput | None:
        bb_out = self._bb.update(close)
        if bb_out is None or bb_out.middle == 0:
            return None
        width = (bb_out.upper - bb_out.lower) / abs(bb_out.middle)
        self._widths.append(width)
        if len(self._widths) < self.lookback:
            return None
        # percentile rank — fraction of lookback widths <= current
        n_le = sum(1 for w in self._widths if w <= width)
        pct = n_le / len(self._widths)
        self._last = BBWidthOutput(width=width, percentile=pct)
        return self._last

    @property
    def value(self) -> BBWidthOutput | None:
        return self._last

    @property
    def is_warm(self) -> bool:
        return self._last is not None
