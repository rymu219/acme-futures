"""Tests for v4 regime classifiers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from acme.broker.base import Bar
from acme.ryan_spec.v4_regime import (
    _ema,
    _resample_bars,
    classify_higher_tf_alignment,
    classify_overnight_bias,
    classify_trend_ema,
    classify_vol_regime,
)

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


# --- classify_overnight_bias --------------------------------------------

def test_overnight_chop_when_insufficient_history():
    """Below min_bars → chop (warmup safety)."""
    bars = _series([100.0] * 10)
    assert classify_overnight_bias(bars) == "chop"


def test_overnight_trend_down_when_buffer_falls():
    """First-to-last drop bigger than threshold_atr × range → trend_down."""
    closes = [100.0 - i * 0.2 for i in range(40)]  # drift down
    bars = _series(closes, atr=1.0)
    assert classify_overnight_bias(bars, threshold_atr=1.0) == "trend_down"


def test_overnight_trend_up_when_buffer_rises():
    closes = [100.0 + i * 0.2 for i in range(40)]
    bars = _series(closes, atr=1.0)
    assert classify_overnight_bias(bars, threshold_atr=1.0) == "trend_up"


def test_overnight_chop_when_round_trip():
    """A round-trip (rise then fall back to start) should classify chop —
    the buffer's net direction is what we care about, not the path."""
    closes = [100.0 + 5.0 * (1 - abs(i - 20) / 20.0) for i in range(40)]
    bars = _series(closes, atr=1.0)
    # Net move from start to end is 0; even with high path volatility, chop.
    assert classify_overnight_bias(bars) == "chop"


# --- classify_vol_regime -------------------------------------------------

def test_vol_chop_when_insufficient_history():
    bars = _series([100.0] * 50, atr=1.0)
    assert classify_vol_regime(bars, fast_bars=10, baseline_bars=120) == "chop"


def test_vol_chop_when_vol_not_expanded():
    """Same ATR throughout = no vol expansion → chop regardless of price."""
    closes = [100.0 + i * 0.1 for i in range(150)]
    bars = _series(closes, atr=1.0)
    assert classify_vol_regime(bars, fast_bars=30, baseline_bars=120,
                               vol_ratio_threshold=1.3) == "chop"


def _series_with_vol_step(
    n_baseline: int = 120, n_fast: int = 30,
    baseline_atr: float = 1.0, fast_atr: float = 2.0,
    direction: int = 0,
) -> list[Bar]:
    """Build a bar series whose ATR steps up in the most-recent fast_bars."""
    bars: list[Bar] = []
    t0 = datetime(2026, 5, 7, 0, 0, tzinfo=CT)
    for i in range(n_baseline - n_fast):
        c = 100.0
        bars.append(_bar(t0 + timedelta(minutes=2 * i), c=c,
                         high=c + baseline_atr / 2, low=c - baseline_atr / 2))
    # Then n_fast bars with wider range and a drift in `direction`
    base = n_baseline - n_fast
    for j in range(n_fast):
        c = 100.0 + direction * j * 0.3
        bars.append(_bar(t0 + timedelta(minutes=2 * (base + j)), c=c,
                         high=c + fast_atr / 2, low=c - fast_atr / 2))
    return bars


def test_vol_trend_down_when_expanded_and_falling():
    bars = _series_with_vol_step(direction=-1, fast_atr=2.0)
    assert classify_vol_regime(bars, fast_bars=30, baseline_bars=120,
                               vol_ratio_threshold=1.3,
                               slope_atr_threshold=0.5) == "trend_down"


def test_vol_trend_up_when_expanded_and_rising():
    bars = _series_with_vol_step(direction=1, fast_atr=2.0)
    assert classify_vol_regime(bars, fast_bars=30, baseline_bars=120,
                               vol_ratio_threshold=1.3,
                               slope_atr_threshold=0.5) == "trend_up"


def test_vol_chop_when_expanded_but_directionless():
    """Vol expanded but no net move → still chop (don't bias on noise)."""
    bars = _series_with_vol_step(direction=0, fast_atr=2.0)
    assert classify_vol_regime(bars, fast_bars=30, baseline_bars=120,
                               vol_ratio_threshold=1.3) == "chop"


# --- _resample_bars + classify_higher_tf_alignment (PR-F) ----------------

def test_resample_bars_ohlcv_correctness():
    """Each HTF bar's OHLCV is computed from its raw-bar group correctly.
    open=first, high=max, low=min, close=last, volume=sum."""
    t0 = datetime(2026, 5, 7, 9, 0, tzinfo=CT)
    raw = []
    for i in range(10):
        # Vary OHLCV deterministically so we can assert exact resample math.
        raw.append(Bar(
            t=t0 + timedelta(minutes=2 * i),
            o=100.0 + i,
            h=105.0 + i,
            l=95.0 + i,
            c=101.0 + i,
            v=10 + i,
        ))
    htf = _resample_bars(raw, factor=5)
    assert len(htf) == 2
    # First HTF bar: bars 0-4
    first = htf[0]
    assert first.o == 100.0  # first.o
    assert first.h == 109.0  # max of [105..109]
    assert first.l == 95.0   # min of [95..99]
    assert first.c == 105.0  # last bar's close (101+4)
    assert first.v == sum(10 + i for i in range(5))
    # Second HTF bar: bars 5-9
    second = htf[1]
    assert second.o == 105.0
    assert second.h == 114.0
    assert second.l == 100.0
    assert second.c == 110.0
    assert second.v == sum(10 + i for i in range(5, 10))


def test_resample_drops_partial_trailing_group():
    """N raw bars at factor F → N // F HTF bars; trailing < F bars dropped."""
    raw = _series([100.0] * 32)
    htf = _resample_bars(raw, factor=10)
    assert len(htf) == 3  # 32 // 10 = 3, the trailing 2 bars are discarded


def test_resample_zero_factor_raises():
    """Sanity: factor must be positive."""
    import pytest
    with pytest.raises(ValueError):
        _resample_bars(_series([100.0] * 5), factor=0)


def test_higher_tf_alignment_chop_on_warmup():
    """Insufficient raw bars → chop (the inner classify_trend_ema's
    warmup branch fires after resampling)."""
    bars = _series([100.0] * 100)  # at factor=15 → 6 HTF bars, < 30 needed
    assert classify_higher_tf_alignment(bars) == "chop"


def test_higher_tf_alignment_trend_up_on_rising_htf():
    """450 raw bars resampled to 30 HTF bars rising linearly → trend_up."""
    closes = [100.0 + i * 0.05 for i in range(450)]
    bars = _series(closes, atr=1.0)
    assert classify_higher_tf_alignment(bars) == "trend_up"


def test_higher_tf_alignment_trend_down_on_falling_htf():
    closes = [100.0 - i * 0.05 for i in range(450)]
    bars = _series(closes, atr=1.0)
    assert classify_higher_tf_alignment(bars) == "trend_down"


def test_higher_tf_alignment_chop_on_lower_tf_noise_higher_tf_flat():
    """Killer-feature test: 2-min noise within ATR but no net 30-min drift
    → chop. This is the *whole point* of the variant — it sees through
    short-timeframe wiggle that v4-trend-gate might over-react to."""
    # Sawtooth pattern: alternates ±0.4 each bar so the 2-min EMA sees
    # significant local moves, but consecutive bars cancel out so the
    # 30-min resampled close is near-flat.
    closes = [100.0 + (0.4 if i % 2 else -0.4) for i in range(450)]
    bars = _series(closes, atr=1.0)
    # The 30-min EMA slope should sit inside the noise band → chop.
    assert classify_higher_tf_alignment(bars) == "chop"
