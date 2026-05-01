"""Tests for streaming indicators against known values."""

from datetime import UTC, datetime, timedelta

import pytest

from acme.broker.base import Bar
from acme.indicators import ADX, ATR, EMA, RSI, SMA, Bollinger, Stochastic


def _bars_from(ohlc_tuples):
    """Build bars from list of (o, h, l, c) tuples."""
    t0 = datetime(2026, 4, 30, 14, 0, tzinfo=UTC)
    return [
        Bar(t=t0 + timedelta(minutes=i), o=o, h=h, l=lo, c=c, v=10)
        for i, (o, h, lo, c) in enumerate(ohlc_tuples)
    ]


# ---------- EMA ----------

def test_ema_warmup():
    ema = EMA(period=3)
    assert ema.update(10.0) is None         # not warm yet
    assert ema.update(10.0) is None
    assert ema.update(10.0) is not None     # 3rd sample, warm
    assert ema.value == pytest.approx(10.0)


def test_ema_responds_to_change():
    ema = EMA(period=3)
    for x in [10.0, 10.0, 10.0]:
        ema.update(x)
    # k = 2/(3+1) = 0.5; new value = 20*0.5 + 10*0.5 = 15
    ema.update(20.0)
    assert ema.value == pytest.approx(15.0)


def test_ema_invalid_period():
    with pytest.raises(ValueError):
        EMA(0)


# ---------- SMA ----------

def test_sma_basic():
    sma = SMA(period=3)
    sma.update(1)
    sma.update(2)
    assert sma.value is None    # not warm
    sma.update(3)
    assert sma.value == pytest.approx(2.0)
    sma.update(4)
    assert sma.value == pytest.approx(3.0)   # rolling: (2+3+4)/3


# ---------- ATR ----------

def test_atr_warm_after_period():
    atr = ATR(period=3)
    bars = _bars_from([(100, 102, 99, 101), (101, 103, 100, 102), (102, 104, 101, 103)])
    for b in bars:
        atr.update(b)
    assert atr.is_warm
    assert atr.value > 0


def test_atr_with_gaps_uses_max_tr():
    """True range should consider gap from prior close."""
    atr = ATR(period=2)
    atr.update(_bars_from([(100, 101, 99, 100)])[0])
    # Next bar gaps up — true range should be high - prev close = 110 - 100 = 10
    b = Bar(t=datetime(2026, 4, 30, 14, 1, tzinfo=UTC),
            o=110, h=111, l=109, c=110, v=10)
    atr.update(b)
    assert atr.value == pytest.approx((2.0 + 11.0) / 2.0, abs=0.01)


# ---------- RSI ----------

def test_rsi_all_gains_is_100():
    rsi = RSI(period=3)
    for x in [10, 11, 12, 13, 14]:
        rsi.update(x)
    assert rsi.value == pytest.approx(100.0)


def test_rsi_all_losses_is_zero():
    rsi = RSI(period=3)
    for x in [10, 9, 8, 7, 6]:
        rsi.update(x)
    assert rsi.value == pytest.approx(0.0)


def test_rsi_neutral_around_50():
    rsi = RSI(period=4)
    # Alternating up/down by 1
    for x in [10, 11, 10, 11, 10, 11, 10]:
        rsi.update(x)
    # Average gain ≈ average loss → RSI ≈ 50
    assert 35 < rsi.value < 65


# ---------- Stochastic ----------

def test_stochastic_warmup():
    s = Stochastic(k_period=3, k_smoothing=1, d_period=2)
    bars = _bars_from([(100, 105, 95, 102), (101, 106, 96, 103), (102, 107, 97, 100)])
    for b in bars:
        result = s.update(b)
    # After 3 bars, raw %K is computable; %D needs one more bar to smooth
    assert result is None
    s.update(Bar(t=datetime(2026, 4, 30, 14, 3, tzinfo=UTC),
                 o=100, h=108, l=98, c=104, v=10))
    assert s.value is not None
    assert 0 <= s.value.k <= 100
    assert 0 <= s.value.d <= 100


def test_stochastic_at_high():
    """Close at top of range → %K near 100."""
    s = Stochastic(k_period=3, k_smoothing=1, d_period=2)
    # All bars: high=110, low=100, close=110 (at top)
    bars = _bars_from([(105, 110, 100, 110)] * 5)
    for b in bars:
        s.update(b)
    assert s.value.k == pytest.approx(100.0)


# ---------- Bollinger ----------

def test_bollinger_basic():
    bb = Bollinger(period=4, num_std=2.0)
    for x in [10, 12, 14, 16]:
        bb.update(x)
    out = bb.update(18)   # 5th value, but the period-4 window is now [12,14,16,18]
    assert out is not None
    # mean = (12+14+16+18)/4 = 15, var = ((9+1+1+9)/4)=5, sd=2.236
    assert out.middle == pytest.approx(15.0)
    assert out.upper > out.lower
    assert out.width > 0


def test_bollinger_warmup_returns_none():
    bb = Bollinger(period=3, num_std=2.0)
    assert bb.update(10) is None
    assert bb.update(11) is None
    assert bb.update(12) is not None


# ---------- ADX ----------

def test_adx_ranging_market_low_value():
    """An oscillating market produces low ADX (< 20-25)."""
    adx = ADX(period=5)
    # Build bars that oscillate within a tight range
    bars = []
    t0 = datetime(2026, 4, 30, 14, 0, tzinfo=UTC)
    for i in range(40):
        # Sawtooth: up 1, down 1
        delta = 1.0 if i % 2 == 0 else -1.0
        bars.append(Bar(t=t0 + timedelta(minutes=i),
                        o=100, h=100.5 + delta, l=99.5 + delta, c=100 + delta, v=10))
    for b in bars:
        adx.update(b)
    assert adx.is_warm
    # Ranging market → ADX should be relatively low. Generous threshold.
    assert adx.value < 50


def test_adx_strong_trend_high_value():
    """A strong unidirectional trend produces high ADX (> 25)."""
    adx = ADX(period=5)
    bars = []
    t0 = datetime(2026, 4, 30, 14, 0, tzinfo=UTC)
    for i in range(40):
        # Steady uptrend, each bar makes new high
        base = 100 + i * 1.0
        bars.append(Bar(t=t0 + timedelta(minutes=i),
                        o=base, h=base + 0.5, l=base - 0.2, c=base + 0.4, v=10))
    for b in bars:
        adx.update(b)
    assert adx.is_warm
    assert adx.value > 25
