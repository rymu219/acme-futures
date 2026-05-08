"""v4 regime classifiers.

The v3 strategy is a contrarian mean-reversion thesis: when cum_delta is
extreme, bet on a bounce. On chop days that works (OOS PF 2.30). On strong-
trend days it gets run over (2026-05-07: -$1,825 fleet, post-mortem in
docs/2026-05-07-trading-day-analysis.md).

This module provides pure-function classifiers that map a bar history to a
regime label. The v4 wrapper engines (`v4_engine.py`) consume this label and
either *gate* (skip counter-trend signals) or *flip* (invert direction) v3
entries.

Each classifier returns one of:
    "trend_up"   — recent bars trending higher, momentum favors longs
    "trend_down" — recent bars trending lower,  momentum favors shorts
    "chop"       — no clear direction; v3 mean-reversion thesis applies

`chop` is the default for "we don't know" — insufficient history, flat slope,
ambiguous data. The caller can treat it the same as today (take v3 signals
unfiltered).

Adding a new classifier: write `classify_<name>(bars, ...) -> Regime`,
keep it pure (no I/O, no time.now()), and add a unit test alongside.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from acme.broker.base import Bar

Regime = Literal["trend_up", "trend_down", "chop"]


# Tuned for 2-minute bars (the v3 runtime's bar size). EMA(20) ≈ 40-min
# smoothing; 10-bar lookback ≈ 20-min slope. slope_atr_threshold=0.5 means
# the recent EMA move must exceed half a typical bar's range to count as
# trend — empirically separates "drifting flat" from "moving directionally."
DEFAULT_EMA_PERIOD = 20
DEFAULT_LOOKBACK_BARS = 10
DEFAULT_SLOPE_ATR_THRESHOLD = 0.5


def _ema(values: Sequence[float], period: int) -> list[float]:
    """Exponential moving average. Seeds with SMA over the first `period`
    samples, then standard EMA recurrence. Returns a list the same length
    as `values`; entries before the seed point repeat the seed value (so
    callers can index by the same offset)."""
    if not values:
        return []
    n = len(values)
    if n < period:
        # Not enough data for even one full SMA seed — return the running
        # mean as a degenerate EMA. Callers checking len(bars) >= period
        # before calling won't hit this.
        running = 0.0
        out: list[float] = []
        for i, v in enumerate(values):
            running = (running * i + v) / (i + 1)
            out.append(running)
        return out
    seed = sum(values[:period]) / period
    out = [seed] * period
    alpha = 2.0 / (period + 1)
    prev = seed
    for v in values[period:]:
        cur = alpha * v + (1 - alpha) * prev
        out.append(cur)
        prev = cur
    return out


def _avg_range(bars: Sequence[Bar]) -> float:
    """Average bar (high-low) over the supplied window. Used as a volatility
    normalizer so the slope threshold transports across price levels."""
    if not bars:
        return 0.0
    total = sum(b.h - b.l for b in bars)
    return total / len(bars)


def classify_trend_ema(
    bars: Sequence[Bar],
    *,
    period: int = DEFAULT_EMA_PERIOD,
    lookback_bars: int = DEFAULT_LOOKBACK_BARS,
    slope_atr_threshold: float = DEFAULT_SLOPE_ATR_THRESHOLD,
) -> Regime:
    """Classify the current regime by the slope of an EMA over recent closes.

    Compute EMA(period) of bar closes. Take the slope between the most-recent
    EMA and the EMA `lookback_bars` ago. Normalize by recent average bar range
    (a volatility unit). The result is a unitless ratio; values above
    `+slope_atr_threshold` mean "trend up", below `-threshold` mean "trend
    down", anything between is "chop".

    Returns "chop" when there's not enough history to make a confident call —
    the `v4-trend-gate` variant treats chop as "no override," so this is the
    safe default during early-session bars or warmup.
    """
    needed = period + lookback_bars
    if len(bars) < needed:
        return "chop"
    closes = [b.c for b in bars]
    ema = _ema(closes, period)
    slope = ema[-1] - ema[-1 - lookback_bars]
    atr = _avg_range(bars[-lookback_bars:])
    if atr <= 0:
        return "chop"
    ratio = slope / atr
    if ratio > slope_atr_threshold:
        return "trend_up"
    if ratio < -slope_atr_threshold:
        return "trend_down"
    return "chop"


# Tuned for a deep buffer (~12 hours of 2-min bars = 360) so the classifier
# can see across the overnight Globex session into RTH. With a 60-bar buffer
# (default) it degrades to a "last 2 hours direction" signal — still useful,
# just not strictly overnight.
DEFAULT_OVERNIGHT_THRESHOLD_ATR = 1.0
DEFAULT_OVERNIGHT_MIN_BARS = 30


def classify_overnight_bias(
    bars: Sequence[Bar],
    *,
    threshold_atr: float = DEFAULT_OVERNIGHT_THRESHOLD_ATR,
    min_bars: int = DEFAULT_OVERNIGHT_MIN_BARS,
) -> Regime:
    """Classify the day's bias from the direction of the supplied bar window.

    Mechanizes the "I knew today was a short day" intuition by treating the
    move from buffer-start to buffer-end as a proxy for the day's directional
    pressure. With a 360-bar wrapper buffer (12 h), this spans the overnight
    Globex session up to the most recent bar.

    Compares (last close - first close) to recent average bar range. Above
    `+threshold_atr` → trend_up; below `-threshold_atr` → trend_down; else
    chop. Returns chop when fewer than `min_bars` are supplied (early
    session warmup).
    """
    if len(bars) < min_bars:
        return "chop"
    move = bars[-1].c - bars[0].c
    atr = _avg_range(bars[-DEFAULT_LOOKBACK_BARS:])
    if atr <= 0:
        return "chop"
    ratio = move / atr
    if ratio > threshold_atr:
        return "trend_up"
    if ratio < -threshold_atr:
        return "trend_down"
    return "chop"


# Vol-regime: high realized vol typically coincides with directional days
# (regardless of direction). This classifier requires BOTH a vol expansion
# AND a directional confirmation — so it never returns "trend_up" purely
# because vol is high and chop's range happened to drift up.
DEFAULT_VOL_FAST_BARS = 30
DEFAULT_VOL_BASELINE_BARS = 120
DEFAULT_VOL_RATIO_THRESHOLD = 1.3


def classify_vol_regime(
    bars: Sequence[Bar],
    *,
    fast_bars: int = DEFAULT_VOL_FAST_BARS,
    baseline_bars: int = DEFAULT_VOL_BASELINE_BARS,
    vol_ratio_threshold: float = DEFAULT_VOL_RATIO_THRESHOLD,
    slope_atr_threshold: float = DEFAULT_SLOPE_ATR_THRESHOLD,
) -> Regime:
    """High-vol-with-direction regime classifier.

    Compute fast and baseline average bar range over the most-recent
    `fast_bars` and `baseline_bars` respectively. If the ratio
    fast / baseline exceeds `vol_ratio_threshold`, the market is in a
    high-vol regime — typically directional. Then check the close-to-close
    move over the fast window: if directional (above slope_atr_threshold
    in some direction) return that direction, else fall through to chop.

    Returns "chop" when:
      - history < baseline_bars (warmup)
      - vol ratio is not elevated (mean-reversion thesis still applies)
      - vol is elevated but the move is sideways within the noise band
    """
    if len(bars) < baseline_bars:
        return "chop"
    fast_atr = _avg_range(bars[-fast_bars:])
    baseline_atr = _avg_range(bars[-baseline_bars:])
    if baseline_atr <= 0:
        return "chop"
    if fast_atr / baseline_atr < vol_ratio_threshold:
        return "chop"
    # Vol expanded — confirm with a directional move over the fast window.
    move = bars[-1].c - bars[-fast_bars].c
    if fast_atr <= 0:
        return "chop"
    ratio = move / fast_atr
    if ratio > slope_atr_threshold:
        return "trend_up"
    if ratio < -slope_atr_threshold:
        return "trend_down"
    return "chop"
