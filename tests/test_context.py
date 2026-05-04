"""Tests for the universal market-context feature module."""

from datetime import UTC, datetime, timedelta

from acme.broker.base import Bar
from acme.context import MarketContext


def _bar(t, o, h, low, c, v=100):
    return Bar(t=t, o=o, h=h, l=low, c=c, v=v)


def _bars(closes, volumes=None, tf_min=5):
    t0 = datetime(2026, 4, 30, 14, 0, tzinfo=UTC)
    volumes = volumes or [100] * len(closes)
    bars = []
    prev = closes[0]
    for i, (c, v) in enumerate(zip(closes, volumes, strict=False)):
        h = max(prev, c) + 0.25
        lo = min(prev, c) - 0.25
        bars.append(_bar(t0 + timedelta(minutes=i * tf_min), prev, h, lo, c, v))
        prev = c
    return bars


def test_warmup_features_are_none():
    ctx = MarketContext()
    for b in _bars([100.0] * 5):
        ctx.update(b)
    f = ctx.features
    # ATR(14) and EMA(50) need ≥14, ≥50 bars respectively
    assert f["close_vs_ema50"] is None
    assert f["range_vs_atr"] is None


def test_close_position_in_bar_at_high_is_one():
    ctx = MarketContext()
    t = datetime(2026, 4, 30, 14, 0, tzinfo=UTC)
    # Single bar; manual: close = high
    ctx.update(_bar(t, 100, 102, 99, 102, 100))
    assert ctx.features["close_position_in_bar"] == 1.0


def test_close_position_in_bar_at_low_is_zero():
    ctx = MarketContext()
    t = datetime(2026, 4, 30, 14, 0, tzinfo=UTC)
    ctx.update(_bar(t, 100, 102, 99, 99, 100))
    assert ctx.features["close_position_in_bar"] == 0.0


def test_volume_ratio_warm_after_20_bars():
    ctx = MarketContext()
    for b in _bars([100.0] * 25, volumes=[100] * 25):
        ctx.update(b)
    # Average is 100, current bar volume is 100 → ratio = 1.0
    assert abs(ctx.features["volume_ratio_20"] - 1.0) < 1e-6


def test_volume_ratio_high_volume_bar():
    ctx = MarketContext()
    closes = [100.0] * 25
    volumes = [100] * 24 + [500]   # last bar 5x prior avg
    for b in _bars(closes, volumes=volumes):
        ctx.update(b)
    # SMA(20) over the last 20 bars: 19×100 + 1×500 = 2400, avg = 120 → ratio = 500/120 ≈ 4.17
    assert ctx.features["volume_ratio_20"] > 4.0


def test_momentum_5_positive_uptrend():
    ctx = MarketContext()
    # Need ATR warm (14) + 6 closes for momentum_5
    closes = [100 + i * 0.5 for i in range(25)]
    for b in _bars(closes):
        ctx.update(b)
    # Strong uptrend → momentum_5 should be positive and meaningful
    assert ctx.features["momentum_5"] is not None
    assert ctx.features["momentum_5"] > 0


def test_momentum_5_negative_downtrend():
    ctx = MarketContext()
    closes = [100 - i * 0.5 for i in range(25)]
    for b in _bars(closes):
        ctx.update(b)
    assert ctx.features["momentum_5"] is not None
    assert ctx.features["momentum_5"] < 0


def test_idempotent_on_same_bar():
    ctx = MarketContext()
    bars = _bars([100.0] * 25)
    for b in bars:
        ctx.update(b)
    snapshot = dict(ctx.features)
    # Update with the same final bar again — features must not change
    ctx.update(bars[-1])
    assert ctx.features == snapshot
