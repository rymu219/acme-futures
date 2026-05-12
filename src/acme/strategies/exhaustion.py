"""Exhaustion-bar detector for the BOUNDARY strategy.

A bar is "exhausted" (likely reversal candidate) when ALL four of:

  1. **Local extreme** — the bar's high is the N-bar highest high (for
     reversal-down candidates) or the bar's low is the N-bar lowest low
     (for reversal-up candidates).
  2. **Doji body** — |close - open| / (high - low) < body_max_ratio.
     A small body relative to the range indicates indecision.
  3. **Below-average volume** — bar.v < volume_avg * volume_max_ratio.
     Reversal exhaustion typically comes with declining participation,
     not a fresh impulse.
  4. **Confirmed close** — the close is in the opposite half of the
     bar from the extreme (i.e. for a top, close is in the lower half
     of the range; for a bottom, close is in the upper half).

Caller drives the buffer via `update(bar)` and gets back an
`ExhaustionBar | None` indicating whether the *just-completed* bar
qualifies.

Defaults match the typical retail/Wyckoff exhaustion-bar definition.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Literal

from acme.broker.base import Bar
from acme.indicators import SMA

ExhaustionDirection = Literal["top", "bottom"]


@dataclass(frozen=True)
class ExhaustionBar:
    """The just-evaluated bar, with the qualifying direction."""
    bar: Bar
    direction: ExhaustionDirection
    body_ratio: float
    range_size: float
    volume_ratio: float


class ExhaustionDetector:
    """Stateful exhaustion-bar detector.

    Usage:
        det = ExhaustionDetector()
        for bar in bars:
            out = det.update(bar)
            if out is not None:
                ...  # exhausted top/bottom

    Returns None until the lookback window has filled.
    """

    def __init__(
        self, *,
        lookback_bars: int = 10,
        volume_avg_period: int = 20,
        body_max_ratio: float = 0.30,
        volume_max_ratio: float = 1.00,
    ) -> None:
        if lookback_bars < 2:
            raise ValueError("lookback_bars must be >= 2")
        if not 0 < body_max_ratio <= 1:
            raise ValueError("body_max_ratio must be in (0, 1]")
        if volume_max_ratio <= 0:
            raise ValueError("volume_max_ratio must be > 0")
        self._lookback = lookback_bars
        self._body_max = body_max_ratio
        self._vol_max = volume_max_ratio
        self._highs: deque[float] = deque(maxlen=lookback_bars)
        self._lows: deque[float] = deque(maxlen=lookback_bars)
        self._vol_sma = SMA(volume_avg_period)

    @property
    def is_warm(self) -> bool:
        return len(self._highs) == self._lookback and self._vol_sma.is_warm

    def update(self, bar: Bar) -> ExhaustionBar | None:
        self._highs.append(bar.h)
        self._lows.append(bar.l)
        vma = self._vol_sma.update(float(bar.v))
        if not self.is_warm or vma is None or vma <= 0:
            return None

        range_size = bar.h - bar.l
        if range_size <= 0:
            return None
        body = abs(bar.c - bar.o)
        body_ratio = body / range_size
        if body_ratio >= self._body_max:
            return None  # body too large — not a doji

        vol_ratio = float(bar.v) / vma
        if vol_ratio >= self._vol_max:
            return None  # not below average

        is_local_top = bar.h == max(self._highs)
        is_local_bottom = bar.l == min(self._lows)
        if not (is_local_top or is_local_bottom):
            return None

        midpoint = (bar.h + bar.l) / 2
        # Top exhaustion: close in lower half AND we hit a new local high.
        # Bottom exhaustion: close in upper half AND we hit a new local low.
        if is_local_top and bar.c < midpoint:
            direction: ExhaustionDirection = "top"
        elif is_local_bottom and bar.c > midpoint:
            direction = "bottom"
        else:
            return None

        return ExhaustionBar(
            bar=bar, direction=direction,
            body_ratio=body_ratio, range_size=range_size,
            volume_ratio=vol_ratio,
        )
