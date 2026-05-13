"""Tests for the exhaustion-bar detector."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from acme.broker.base import Bar
from acme.strategies.exhaustion import ExhaustionDetector


def _bar(t, *, o, h, l, c, v=100):  # noqa: E741 — matches Bar.l field
    return Bar(t=t, o=o, h=h, l=l, c=c, v=v)


def _filler(t, *, c=100.0, h_pad=0.1, l_pad=0.1, v=100):
    return _bar(t, o=c, h=c + h_pad, l=c - l_pad, c=c, v=v)


T0 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


# ════════════ config validation ═══════════════════════════════════════


def test_invalid_lookback():
    with pytest.raises(ValueError):
        ExhaustionDetector(lookback_bars=1)


def test_invalid_body_ratio():
    with pytest.raises(ValueError):
        ExhaustionDetector(body_max_ratio=1.5)


# ════════════ warm-up ═════════════════════════════════════════════════


def test_warmup_returns_none():
    det = ExhaustionDetector(lookback_bars=5, volume_avg_period=5)
    for i in range(4):
        assert det.update(_filler(T0 + timedelta(minutes=2 * i))) is None
    assert not det.is_warm


# ════════════ top exhaustion ══════════════════════════════════════════


def test_detects_top_exhaustion():
    """Build 10 quiet bars then one bar that hits a new local high, has a
    tiny body, low volume, and closes in the lower half of its range."""
    det = ExhaustionDetector(lookback_bars=10, volume_avg_period=10)
    # Warm with 10 quiet bars at price 100, volume 100
    bars = []
    for i in range(10):
        bars.append(_filler(T0 + timedelta(minutes=2 * i)))
    for b in bars:
        det.update(b)

    # Now the exhaustion bar: new local high at 102, doji body (open=close
    # nearly), close in lower half, volume below average.
    ex_bar = _bar(
        T0 + timedelta(minutes=2 * 10),
        o=101.5, h=102.0, l=101.0, c=101.1,  # body 0.4 / range 1.0 = 0.4 — too big
        v=50,
    )
    # First check that body_ratio rejection works
    assert det.update(ex_bar) is None

    # Try a true doji with close clearly in the lower half (midpoint=101.5)
    ex_bar = _bar(
        T0 + timedelta(minutes=2 * 11),
        o=101.35, h=102.0, l=101.0, c=101.25,  # body 0.1, close at 101.25 < 101.5
        v=50,
    )
    out = det.update(ex_bar)
    assert out is not None
    assert out.direction == "top"
    assert out.body_ratio < 0.3
    assert out.volume_ratio < 1.0


# ════════════ bottom exhaustion ═══════════════════════════════════════


def test_detects_bottom_exhaustion():
    det = ExhaustionDetector(lookback_bars=10, volume_avg_period=10)
    for i in range(10):
        det.update(_filler(T0 + timedelta(minutes=2 * i)))
    ex_bar = _bar(
        T0 + timedelta(minutes=2 * 10),
        o=98.65, h=99.0, l=98.0, c=98.75,  # body 0.1, close at 98.75 > midpoint 98.5
        v=50,
    )
    out = det.update(ex_bar)
    assert out is not None
    assert out.direction == "bottom"


# ════════════ rejections ══════════════════════════════════════════════


def test_rejects_when_volume_above_average():
    det = ExhaustionDetector(lookback_bars=10, volume_avg_period=10)
    for i in range(10):
        det.update(_filler(T0 + timedelta(minutes=2 * i)))
    # Doji bar at new high but with volume SPIKE
    ex_bar = _bar(
        T0 + timedelta(minutes=2 * 10),
        o=101.6, h=102.0, l=101.0, c=101.5, v=500,
    )
    assert det.update(ex_bar) is None


def test_rejects_when_not_local_extreme():
    det = ExhaustionDetector(lookback_bars=10, volume_avg_period=10)
    # First bar a much higher high
    det.update(_bar(T0, o=100, h=110, l=99.9, c=100, v=100))
    for i in range(1, 10):
        det.update(_filler(T0 + timedelta(minutes=2 * i)))
    # Doji bar at h=102 — not a new local high (110 is still the max)
    ex_bar = _bar(
        T0 + timedelta(minutes=2 * 10),
        o=101.6, h=102.0, l=101.0, c=101.5, v=50,
    )
    assert det.update(ex_bar) is None


def test_rejects_when_close_in_wrong_half():
    """Local top but close in upper half → not a top exhaustion."""
    det = ExhaustionDetector(lookback_bars=10, volume_avg_period=10)
    for i in range(10):
        det.update(_filler(T0 + timedelta(minutes=2 * i)))
    ex_bar = _bar(
        T0 + timedelta(minutes=2 * 10),
        o=101.5, h=102.0, l=101.0, c=101.9, v=50,  # close near high — not exhaustion
    )
    assert det.update(ex_bar) is None
