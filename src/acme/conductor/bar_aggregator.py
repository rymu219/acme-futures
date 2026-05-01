"""Tick-to-bar aggregator. Extracted from runner.py (Phase A).

Phase A had a 1-minute aggregator. Phase B keeps the 1-min aggregator and adds
a multi-timeframe wrapper that fans completed 1-min bars into 5-min (and other)
buckets for strategies declaring `timeframe_minutes != 1`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from acme.broker.base import Bar


class BarAggregator:
    """Aggregate ticks into N-minute bars (UTC minute boundaries)."""

    def __init__(self, timeframe_minutes: int = 1) -> None:
        if timeframe_minutes < 1:
            raise ValueError("timeframe_minutes must be >= 1")
        self._tf_min = timeframe_minutes
        self._bucket_start: datetime | None = None
        self._o: float | None = None
        self._h: float | None = None
        self._l: float | None = None
        self._c: float | None = None
        self._v = 0

    def add_tick(self, t: datetime, price: float) -> Bar | None:
        bucket = _bucket_floor(t, self._tf_min)
        if self._bucket_start is None:
            self._reset(bucket, price)
            return None
        if bucket == self._bucket_start:
            self._h = max(self._h or price, price)
            self._l = min(self._l or price, price)
            self._c = price
            self._v += 1
            return None
        bar = Bar(
            t=self._bucket_start,
            o=self._o or price,
            h=self._h or price,
            l=self._l or price,
            c=self._c or price,
            v=self._v,
        )
        self._reset(bucket, price)
        return bar

    def _reset(self, bucket: datetime, price: float) -> None:
        self._bucket_start = bucket
        self._o = self._h = self._l = self._c = price
        self._v = 1


class MultiTimeframeAggregator:
    """Wraps a 1-min tick stream and fans completed 1-min bars into one
    sub-aggregator per requested timeframe (5, 15, etc.).

    Used by the conductor when strategies declare `timeframe_minutes` != 1:
    we keep a single tick stream from the broker, build 1-min bars, and roll
    them up into longer bars for the strategies that want them.
    """

    def __init__(self, timeframes: list[int]) -> None:
        if 1 not in timeframes:
            timeframes = [1, *timeframes]
        self._timeframes = sorted(set(timeframes))
        # Per-timeframe aggregator that consumes 1-min bars and emits longer ones.
        # The 1-min entry is its own pass-through; for tf>1, we aggregate 1-min bars.
        self._tick_agg = BarAggregator(timeframe_minutes=1)
        self._bar_aggs: dict[int, BarAggregator] = {
            tf: BarAggregator(timeframe_minutes=tf) for tf in self._timeframes if tf > 1
        }

    def add_tick(self, t: datetime, price: float) -> dict[int, Bar]:
        """Returns a dict {timeframe_minutes: Bar} of any bars that closed on this tick.
        At most one bar per timeframe per tick.
        """
        out: dict[int, Bar] = {}
        one_min = self._tick_agg.add_tick(t, price)
        if one_min is None:
            return out
        out[1] = one_min
        # Roll the completed 1-min bar into each longer aggregator using its close
        # price as a synthetic tick at the bar's start time.
        for tf, agg in self._bar_aggs.items():
            roll_t = one_min.t + timedelta(minutes=1) - timedelta(microseconds=1)
            longer = agg.add_tick(roll_t, one_min.c)
            if longer is not None:
                out[tf] = longer
        return out


def _bucket_floor(t: datetime, tf_min: int) -> datetime:
    minute = (t.minute // tf_min) * tf_min
    return t.replace(minute=minute, second=0, microsecond=0)
