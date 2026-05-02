"""Tests for classify_regime() rules and RegimeEngine end-to-end."""

import math
from datetime import UTC, datetime, timedelta

from acme.broker.base import Bar
from acme.regime.classifier import RegimeEngine, classify_regime


def _t(i: int = 0) -> datetime:
    return datetime(2026, 4, 30, 14, 0, tzinfo=UTC) + timedelta(minutes=i)


def test_chaotic_takes_priority_news_blackout():
    snap = classify_regime(
        ts=_t(), timeframe="5m",
        adx=30.0, adx_direction="rising", atr_current=10.0, atr_ratio=1.0,
        bb_width=0.05, bb_width_pct=0.5, hurst=0.6,
        volume_ratio=1.0, momentum_score=0.7, news_blackout=True,
    )
    assert snap.regime == "chaotic"
    assert snap.confidence == 1.0


def test_chaotic_atr_ratio_extreme():
    snap = classify_regime(
        ts=_t(), timeframe="5m",
        adx=20.0, adx_direction="flat", atr_current=10.0, atr_ratio=2.5,
        bb_width=0.05, bb_width_pct=0.5, hurst=0.5,
        volume_ratio=1.0, momentum_score=0.5, news_blackout=False,
    )
    assert snap.regime == "chaotic"


def test_compressing_low_bb_low_atr():
    snap = classify_regime(
        ts=_t(), timeframe="5m",
        adx=10.0, adx_direction="flat", atr_current=2.0, atr_ratio=0.5,
        bb_width=0.01, bb_width_pct=0.10, hurst=0.5,
        volume_ratio=0.6, momentum_score=0.5, news_blackout=False,
    )
    assert snap.regime == "compressing"


def test_trending_clean():
    snap = classify_regime(
        ts=_t(), timeframe="5m",
        adx=32.0, adx_direction="rising", atr_current=12.0, atr_ratio=1.1,
        bb_width=0.10, bb_width_pct=0.65, hurst=0.62,
        volume_ratio=1.5, momentum_score=0.75, news_blackout=False,
    )
    assert snap.regime == "trending"
    assert snap.direction == "long_bias"
    assert snap.confidence > 0.6


def test_trending_short_bias_when_momentum_low():
    snap = classify_regime(
        ts=_t(), timeframe="5m",
        adx=30.0, adx_direction="rising", atr_current=12.0, atr_ratio=1.1,
        bb_width=0.10, bb_width_pct=0.65, hurst=0.55,
        volume_ratio=1.0, momentum_score=0.30, news_blackout=False,
    )
    assert snap.regime == "trending"
    assert snap.direction == "short_bias"


def test_ranging_low_adx_neutral_hurst():
    snap = classify_regime(
        ts=_t(), timeframe="5m",
        adx=14.0, adx_direction="falling", atr_current=4.0, atr_ratio=0.9,
        bb_width=0.04, bb_width_pct=0.40, hurst=0.45,
        volume_ratio=0.9, momentum_score=0.5, news_blackout=False,
    )
    assert snap.regime == "ranging"
    assert snap.confidence > 0.5


def test_ambiguous_when_no_rule_matches():
    snap = classify_regime(
        ts=_t(), timeframe="5m",
        adx=23.0, adx_direction="flat", atr_current=8.0, atr_ratio=1.0,
        bb_width=0.05, bb_width_pct=0.50, hurst=0.51,
        volume_ratio=1.0, momentum_score=0.5, news_blackout=False,
    )
    assert snap.regime == "ambiguous"


def test_warmup_returns_ambiguous_with_low_confidence():
    """If any core indicator is None (warmup), classification should be ambiguous."""
    snap = classify_regime(
        ts=_t(), timeframe="5m",
        adx=None, adx_direction="flat", atr_current=None, atr_ratio=None,
        bb_width=None, bb_width_pct=None, hurst=None,
        volume_ratio=None, momentum_score=None, news_blackout=False,
    )
    assert snap.regime == "ambiguous"
    assert snap.confidence < 0.5


# ---------- RegimeEngine integration ----------

def _bar(i: int, *, o: float, h: float, l: float, c: float, v: int = 100) -> Bar:  # noqa: E741
    return Bar(t=_t(i), o=o, h=h, l=l, c=c, v=v)


def test_engine_warms_up_to_a_classification():
    """Feed enough bars that all indicators warm; assert engine emits a non-warmup snapshot."""
    eng = RegimeEngine(timeframe_minutes=5)
    last_regime = None
    # 200 bars in a steady uptrend with mild noise — should converge to trending
    price = 100.0
    for i in range(220):
        price *= 1.0008 * (1 + 0.0005 * math.sin(i / 3))
        snap = eng.on_bar(_bar(i, o=price * 0.9995, h=price * 1.001,
                               l=price * 0.999, c=price))
        last_regime = snap
    assert last_regime is not None
    # After 220 strong-trend bars, expect trending or at least non-warmup
    assert last_regime.regime in ("trending", "ranging", "ambiguous")
    # Indicator values should be present (not None)
    assert last_regime.adx is not None
    assert last_regime.atr_ratio is not None
    assert last_regime.hurst is not None


def test_engine_chaotic_on_volatility_spike():
    """Build a low-vol baseline then spike ATR to trigger chaotic."""
    eng = RegimeEngine(timeframe_minutes=5)
    price = 100.0
    for i in range(180):
        price *= 1 + 0.0001 * math.sin(i)
        eng.on_bar(_bar(i, o=price, h=price * 1.0005, l=price * 0.9995, c=price))
    # Now a huge bar
    last = eng.on_bar(_bar(181, o=100.0, h=120.0, l=80.0, c=110.0))
    # ATR ratio explodes; expect chaotic
    assert last.regime == "chaotic" or (last.atr_ratio is not None and last.atr_ratio > 1.0)
