"""Tests for the PULSE core feature engine.

Validates the porting of the Pine math: 4-bar decayed slope, RVOL,
logistic probability, edge, projected move. No HTF / structure / level
gates here — those are Phase 2b and live in separate modules.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from acme.broker.base import Bar
from acme.strategies.pulse_features import (
    PulseFeatureConfig,
    PulseFeatureEngine,
)


def _bars(opens_highs_lows_closes_vols, t0=None):
    """Build a list of Bars from a list of (o, h, l, c, v) tuples."""
    t0 = t0 or datetime(2026, 4, 30, 14, 0, tzinfo=UTC)
    return [
        Bar(t=t0 + timedelta(minutes=2 * i), o=o, h=h, l=lo, c=c, v=v)
        for i, (o, h, lo, c, v) in enumerate(opens_highs_lows_closes_vols)
    ]


def _flat_bars(n: int, price: float = 100.0, volume: int = 100) -> list[Bar]:
    """N bars at constant price + volume. Useful for warm-up."""
    return _bars([(price, price + 0.1, price - 0.1, price, volume) for _ in range(n)])


# ---------- config validation ----------


def test_config_rejects_ema_fast_geq_slow():
    with pytest.raises(ValueError, match="ema_fast must be < ema_slow"):
        PulseFeatureConfig(ema_fast=14, ema_slow=14)


def test_config_rejects_invalid_decay():
    with pytest.raises(ValueError, match="decay"):
        PulseFeatureConfig(decay=1.5)
    with pytest.raises(ValueError, match="decay"):
        PulseFeatureConfig(decay=0.0)


def test_config_rejects_nonpositive_score_clip():
    with pytest.raises(ValueError, match="score_clip"):
        PulseFeatureConfig(score_clip=0.0)


# ---------- warm-up ----------


def test_engine_returns_none_before_warm():
    eng = PulseFeatureEngine()  # defaults: vol_ma=20 is the slowest
    # 20 flat bars warms the SMA but slope history is still building
    for bar in _flat_bars(20):
        assert eng.update(bar) is None
    assert not eng.is_warm

    # 4 more bars to fill slope history
    out = None
    for bar in _flat_bars(4, price=100.0):
        out = eng.update(bar)
    assert eng.is_warm
    assert out is not None


def test_required_history_bars_covers_warm_up():
    eng = PulseFeatureEngine()
    need = eng.required_history_bars()
    bars = _flat_bars(need)
    # Sufficient warm-up
    out = None
    for bar in bars:
        out = eng.update(bar)
    assert eng.is_warm
    assert out is not None


# ---------- math: flat market ----------


def test_flat_market_produces_neutral_edge():
    """A perfectly flat market: zero slope → mom_w = 0, mag_w = 0 →
    score = 0 → p_long = 0.5 → edge = 0."""
    eng = PulseFeatureEngine()
    out = None
    for bar in _flat_bars(60):
        out = eng.update(bar)
    assert out is not None
    # Flat → no slope, no edge. RVOL = 1 (volume is constant).
    assert out.slope == pytest.approx(0.0, abs=1e-9)
    assert out.mom_w == pytest.approx(0.0, abs=1e-9)
    assert out.mag_w == pytest.approx(0.0, abs=1e-9)
    assert out.tanh_mag == pytest.approx(0.0, abs=1e-9)
    assert out.rvol == pytest.approx(1.0, abs=1e-9)
    # With slope=0 and rvol=1, raw_score = 0 → p_long = 0.5 → edge = 0
    assert out.raw_score == pytest.approx(0.0, abs=1e-9)
    assert out.p_long == pytest.approx(0.5, abs=1e-9)
    assert out.p_short == pytest.approx(0.5, abs=1e-9)
    assert out.edge == pytest.approx(0.0, abs=1e-9)


def test_probabilities_sum_to_one():
    eng = PulseFeatureEngine()
    out = None
    for bar in _flat_bars(60):
        out = eng.update(bar)
    assert out is not None
    assert out.p_long + out.p_short == pytest.approx(1.0, abs=1e-12)


# ---------- math: ramp up ----------


def _build_ramp_up_series(n_warmup: int = 60, n_signal: int = 8, step: float = 0.50):
    """Flat warm-up bars, then a steady up-ramp."""
    warm = _flat_bars(n_warmup, price=100.0)
    last_t = warm[-1].t + timedelta(minutes=2)
    signal = [
        Bar(t=last_t + timedelta(minutes=2 * i),
            o=100.0 + i * step,
            h=100.0 + i * step + 0.25,
            l=100.0 + i * step - 0.1,
            c=100.0 + (i + 1) * step,
            v=100)
        for i in range(n_signal)
    ]
    return warm + signal


def test_ramp_up_produces_long_edge():
    """A persistent up-move should produce p_long > p_short and score > 0."""
    eng = PulseFeatureEngine()
    last = None
    for bar in _build_ramp_up_series():
        last = eng.update(bar)
    assert last is not None
    assert last.slope > 0
    assert last.mom_w > 0          # all recent slopes positive
    assert last.score > 0
    assert last.p_long > 0.5
    assert last.p_short < 0.5
    assert last.edge > 0


def test_ramp_down_produces_short_edge():
    """Symmetric: a persistent down-move flips probability."""
    eng = PulseFeatureEngine()
    series = _build_ramp_up_series()
    # Flip the signal portion downward
    flipped = []
    for i, bar in enumerate(series):
        if i < 60:
            flipped.append(bar)
        else:
            j = i - 60
            base = 100.0 - j * 0.5
            flipped.append(Bar(
                t=bar.t,
                o=base, h=base + 0.1, l=base - 0.25, c=base - 0.5,
                v=100,
            ))
    last = None
    for bar in flipped:
        last = eng.update(bar)
    assert last is not None
    assert last.slope < 0
    assert last.mom_w < 0
    assert last.score < 0
    assert last.p_long < 0.5
    assert last.p_short > 0.5


# ---------- math: clipping ----------


def test_score_is_clipped_at_score_clip():
    """An absurd vol spike should not push score outside ±score_clip."""
    cfg = PulseFeatureConfig(score_clip=1.0)
    eng = PulseFeatureEngine(cfg)
    # Warm flat
    for bar in _flat_bars(40):
        eng.update(bar)
    # Then a massive vol spike with up-move
    t = _flat_bars(40)[-1].t + timedelta(minutes=2)
    spike = Bar(t=t, o=100, h=200, l=99.9, c=200, v=100_000_000)
    out = eng.update(spike)
    assert out is not None
    assert -cfg.score_clip <= out.score <= cfg.score_clip


def test_extreme_rvol_caps_probability_at_logistic_of_score_clip():
    """With score_clip=3, max |score|=3 → max p_long = 1/(1+e^-6) ≈ 0.9975."""
    cfg = PulseFeatureConfig(score_clip=3.0)
    eng = PulseFeatureEngine(cfg)
    for bar in _flat_bars(40):
        eng.update(bar)
    t = _flat_bars(40)[-1].t + timedelta(minutes=2)
    spike = Bar(t=t, o=100, h=200, l=99.9, c=200, v=100_000_000)
    out = eng.update(spike)
    assert out is not None
    max_p = 1.0 / (1.0 + math.exp(-2.0 * cfg.score_clip))
    assert out.p_long <= max_p + 1e-9


# ---------- projected move ----------


def test_projected_move_respects_min_floor():
    """Projected pts is at least cfg.min_range_pts."""
    cfg = PulseFeatureConfig(min_range_pts=2.0)
    eng = PulseFeatureEngine(cfg)
    for bar in _flat_bars(40, price=100.0, volume=1):
        eng.update(bar)
    t = _flat_bars(40)[-1].t + timedelta(minutes=2)
    # Tiny ATR (the bars have ~0.2 range) AND tiny RVOL would otherwise
    # produce a sub-2-point projection. The floor kicks in.
    out = eng.update(Bar(t=t, o=100, h=100.05, l=99.95, c=100, v=1))
    assert out is not None
    assert out.proj_pts >= cfg.min_range_pts


def test_projected_move_scales_with_rvol():
    """Higher RVOL → larger projected move (all else equal)."""
    eng_low = PulseFeatureEngine()
    eng_high = PulseFeatureEngine()
    for bar in _flat_bars(40, price=100.0, volume=100):
        eng_low.update(bar)
        eng_high.update(bar)
    # Final bar: same OHLC, different volume
    t = _flat_bars(40)[-1].t + timedelta(minutes=2)
    common = dict(o=100.5, h=100.6, l=100.4, c=100.55)
    low = eng_low.update(Bar(t=t, v=100, **common))
    high = eng_high.update(Bar(t=t, v=10_000, **common))
    assert low is not None and high is not None
    assert high.proj_pts > low.proj_pts


# ---------- decay weighting ----------


def test_decay_smooths_single_bar_shock():
    """A single up-bar after flat history should NOT push mom_w to 1.0
    — the 3 prior zero-sign slopes weight it down."""
    eng = PulseFeatureEngine()
    for bar in _flat_bars(40):
        eng.update(bar)
    t = _flat_bars(40)[-1].t + timedelta(minutes=2)
    out = eng.update(Bar(t=t, o=100, h=101.5, l=99.9, c=101, v=100))
    assert out is not None
    # The newest slope is positive (1), the other 3 are 0. mom_w should
    # be in (0, 1).
    assert 0.0 < out.mom_w < 1.0


def test_full_uniform_positive_slopes_make_mom_w_one():
    """If every one of the 4 recent slopes is positive, mom_w = 1."""
    eng = PulseFeatureEngine()
    # 40 flat warm-up
    for bar in _flat_bars(40):
        eng.update(bar)
    # 5 strictly-increasing bars (need 5 because the first one produces
    # slope = first non-zero diff, but we want 4 consecutive positive
    # slopes in the deque).
    t = _flat_bars(40)[-1].t + timedelta(minutes=2)
    out = None
    for i in range(5):
        out = eng.update(Bar(
            t=t + timedelta(minutes=2 * i),
            o=100 + i * 0.5,
            h=100 + i * 0.5 + 0.1,
            l=100 + i * 0.5 - 0.1,
            c=100 + (i + 1) * 0.5,
            v=100,
        ))
    assert out is not None
    assert out.mom_w == pytest.approx(1.0, abs=1e-9)
