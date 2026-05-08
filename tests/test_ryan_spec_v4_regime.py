"""Tests for v4 regime classifiers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from acme.broker.base import Bar
from acme.ryan_spec.v4_regime import _ema, classify_trend_ema

CT = timezone(timedelta(hours=-6))


def _bar(t: datetime, *, c: float, high: float | None = None,
         low: float | None = None) -> Bar:
    """Build a Bar with reasonable defaults. high/low default to ±0.5 around close."""
    if high is None:
        high = c + 0.5
    if low is None:
        low = c - 0.5
    return Bar(t=t, o=c, h=high, l=low, c=c, v=100)


def _series(closes: list[float], *, atr: float = 1.0) -> list[Bar]:
    """Build a bar list with the given close prices and a constant bar range
    (h-l = atr). Times are 2-min spaced, anchored at a fixed CT date."""
    t0 = datetime(2026, 5, 7, 9, 0, tzinfo=CT)
    return [
        _bar(t0 + timedelta(minutes=2 * i), c=c,
             high=c + atr / 2, low=c - atr / 2)
        for i, c in enumerate(closes)
    ]


# --- _ema ----------------------------------------------------------------

def test_ema_constant_series_returns_constant():
    """An EMA over a constant series equals the constant after seed."""
    out = _ema([100.0] * 30, 10)
    assert out[-1] == 100.0
    assert out[10] == 100.0


def test_ema_responds_to_new_data():
    """EMA(period) of a step function moves toward the new level."""
    series = [100.0] * 20 + [110.0] * 20
    out = _ema(series, 10)
    # Seed at index 9 is mean of first 10 = 100.
    assert out[9] == 100.0
    # By the end, EMA has moved most of the way toward 110.
    assert 105.0 < out[-1] < 110.0


# --- classify_trend_ema --------------------------------------------------

def test_chop_when_insufficient_history():
    """Below period+lookback bars → 'chop' (safe default during warmup)."""
    bars = _series([100.0] * 10)
    assert classify_trend_ema(bars, period=20, lookback_bars=10) == "chop"


def test_trend_up_on_monotonically_rising_series():
    """A clean uptrend should classify as trend_up."""
    closes = [100.0 + i * 0.5 for i in range(40)]
    bars = _series(closes, atr=1.0)
    assert classify_trend_ema(bars, period=20, lookback_bars=10,
                              slope_atr_threshold=0.5) == "trend_up"


def test_trend_down_on_monotonically_falling_series():
    """A clean downtrend should classify as trend_down."""
    closes = [100.0 - i * 0.5 for i in range(40)]
    bars = _series(closes, atr=1.0)
    assert classify_trend_ema(bars, period=20, lookback_bars=10,
                              slope_atr_threshold=0.5) == "trend_down"


def test_chop_on_flat_series():
    """A flat market with noise within ATR should classify as chop."""
    closes = [100.0 + (0.1 if i % 2 else -0.1) for i in range(40)]
    bars = _series(closes, atr=1.0)
    assert classify_trend_ema(bars, period=20, lookback_bars=10,
                              slope_atr_threshold=0.5) == "chop"


def test_threshold_controls_sensitivity():
    """A gentle drift can be flagged as trend with a low threshold and chop
    with a high one — same data, different cutoff."""
    closes = [100.0 + i * 0.05 for i in range(40)]  # slow drift up
    bars = _series(closes, atr=1.0)
    assert classify_trend_ema(bars, slope_atr_threshold=0.1) == "trend_up"
    assert classify_trend_ema(bars, slope_atr_threshold=2.0) == "chop"


def test_chop_when_atr_zero():
    """Degenerate zero-range bars → return chop rather than divide-by-zero."""
    bars = _series([100.0] * 40, atr=0.0)
    assert classify_trend_ema(bars) == "chop"
