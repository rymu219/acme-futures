"""Market-context features computed once per bar, shared across all strategies.

The features here are deliberately universal — every strategy benefits from
knowing whether volume agrees, whether momentum is aligned, where the close
sits inside the bar. None of the C-1 strategies USE these features (we're in
observation-only mode); they're computed and logged so the Inspector can
answer "what conditions produce winning fires vs losing fires?".

Features (all `float | None`, None during warmup):

  volume_ratio_20         bar.v / SMA(volume, 20)            volume agreement
  momentum_5              (bar.c - close_5_ago) / atr14      normalized momentum
  range_vs_atr            (bar.h - bar.l) / atr14            relative range
  close_position_in_bar   (bar.c - bar.l) / (bar.h - bar.l)  bullishness within bar
  close_vs_ema9           (bar.c - ema9) / atr14             distance from short trend
  close_vs_ema21          (bar.c - ema21) / atr14            distance from medium trend
  close_vs_ema50          (bar.c - ema50) / atr14            distance from long trend
"""

from __future__ import annotations

from collections import deque

from acme.broker.base import Bar
from acme.indicators import ATR, EMA, SMA


class MarketContext:
    """Per-(contract, timeframe) feature accumulator. Update once per bar."""

    def __init__(self, atr_period: int = 14) -> None:
        self._atr = ATR(atr_period)
        self._vol_sma = SMA(20)
        self._ema9 = EMA(9)
        self._ema21 = EMA(21)
        self._ema50 = EMA(50)
        self._closes: deque[float] = deque(maxlen=6)  # need close 5 bars ago
        self._last_bar_t = None
        self._features: dict[str, float | None] = {
            "volume_ratio_20": None,
            "momentum_5": None,
            "range_vs_atr": None,
            "close_position_in_bar": None,
            "close_vs_ema9": None,
            "close_vs_ema21": None,
            "close_vs_ema50": None,
        }

    def update(self, bar: Bar) -> None:
        # Idempotent on the same bar — protects against accidental double-update
        # when conductor fans the same bar to multiple strategies.
        if self._last_bar_t == bar.t:
            return
        self._last_bar_t = bar.t

        self._atr.update(bar)
        vol_avg = self._vol_sma.update(float(bar.v))
        e9 = self._ema9.update(bar.c)
        e21 = self._ema21.update(bar.c)
        e50 = self._ema50.update(bar.c)
        self._closes.append(bar.c)

        atr_val = self._atr.value
        bar_range = bar.h - bar.l

        self._features["volume_ratio_20"] = (
            bar.v / vol_avg if vol_avg and vol_avg > 0 else None
        )
        self._features["momentum_5"] = (
            (bar.c - self._closes[0]) / atr_val
            if len(self._closes) == 6 and atr_val and atr_val > 0
            else None
        )
        self._features["range_vs_atr"] = (
            bar_range / atr_val if atr_val and atr_val > 0 else None
        )
        self._features["close_position_in_bar"] = (
            (bar.c - bar.l) / bar_range if bar_range > 0 else 0.5
        )
        self._features["close_vs_ema9"] = (
            (bar.c - e9) / atr_val if e9 is not None and atr_val and atr_val > 0 else None
        )
        self._features["close_vs_ema21"] = (
            (bar.c - e21) / atr_val if e21 is not None and atr_val and atr_val > 0 else None
        )
        self._features["close_vs_ema50"] = (
            (bar.c - e50) / atr_val if e50 is not None and atr_val and atr_val > 0 else None
        )

    @property
    def features(self) -> dict[str, float | None]:
        return dict(self._features)

    @property
    def is_warm(self) -> bool:
        return all(v is not None for v in self._features.values())
