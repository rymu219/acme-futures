"""Tests for the GO/NO-GO Box feature engine.

Validates each of the 4 gates independently plus the composite signal.
Time-window gate is not in this module; it lives in the strategy layer.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from acme.broker.base import Bar
from acme.strategies.go_no_go import GoNoGoConfig, GoNoGoEngine


def _bars(opens_highs_lows_closes_vols, t0=None):
    t0 = t0 or datetime(2026, 4, 30, 14, 0, tzinfo=UTC)
    return [
        Bar(t=t0 + timedelta(minutes=2 * i), o=o, h=h, l=lo, c=c, v=v)
        for i, (o, h, lo, c, v) in enumerate(opens_highs_lows_closes_vols)
    ]


def _flat(n: int, price: float = 100.0, vol: int = 100) -> list[Bar]:
    return _bars([(price, price + 0.05, price - 0.05, price, vol) for _ in range(n)])


# ---------- config validation ----------


def test_config_rejects_ema_fast_geq_slow():
    with pytest.raises(ValueError, match="ema_fast must be < ema_slow"):
        GoNoGoConfig(ema_fast=14, ema_slow=14)


def test_config_rejects_zero_slope_lookback():
    with pytest.raises(ValueError, match="slope_lookback"):
        GoNoGoConfig(slope_lookback=0)


def test_config_rejects_negative_sep_thr():
    with pytest.raises(ValueError, match="sep_thr"):
        GoNoGoConfig(sep_thr=-0.01)


# ---------- warm-up ----------


def test_engine_returns_none_before_warm():
    eng = GoNoGoEngine()
    for bar in _flat(15):  # slowest indicator (vr_len=20) needs 20
        assert eng.update(bar) is None
    assert not eng.is_warm


def test_engine_warm_after_required_history():
    eng = GoNoGoEngine()
    need = eng.required_history_bars()
    for bar in _flat(need + 1):
        eng.update(bar)
    assert eng.is_warm


# ---------- gate 1: separation ----------


def test_separation_gate_open_in_flat_market():
    eng = GoNoGoEngine()
    last = None
    for bar in _flat(40):
        last = eng.update(bar)
    # Flat → EMAs converge → abs_sep tiny → sep_ok False
    assert last is not None
    assert last.abs_sep < 0.35
    assert last.sep_ok is False
    assert last.signal == 0


def test_separation_gate_passes_on_trend():
    """Strong up-trend opens the gap between EMA9 and EMA14."""
    eng = GoNoGoEngine()
    # Warm flat
    bars = _flat(30)
    # Then a strong ramp: each bar +0.5 close
    t0 = bars[-1].t + timedelta(minutes=2)
    for i in range(50):
        c = 100.0 + i * 0.5
        bars.append(Bar(t=t0 + timedelta(minutes=2 * i),
                        o=c, h=c + 0.1, l=c - 0.1, c=c, v=200))
    last = None
    for bar in bars:
        last = eng.update(bar)
    assert last is not None
    # EMA9 leads EMA14 in an up-trend → separation grows
    assert last.abs_sep > 0.35
    assert last.sep_ok is True


# ---------- gate 2: volume ratio ----------


def test_volume_gate_fails_on_normal_volume():
    eng = GoNoGoEngine()
    last = None
    for bar in _flat(40, vol=100):
        last = eng.update(bar)
    assert last is not None
    # vr ≈ 1.0 here (volume = volume MA). Default vr_thr = 0.85 — passes.
    # But require_vr_rising defaults to True, and on flat vol the VR
    # stays flat → not rising → gate fails.
    assert last.vr_ok is False


def test_volume_gate_passes_on_rising_volume():
    """A volume spike on the final bar produces VR > 1 and VR > VR_prev."""
    eng = GoNoGoEngine(GoNoGoConfig(require_vr_rising=True, vr_thr=0.85))
    bars = []
    t0 = datetime(2026, 4, 30, 14, 0, tzinfo=UTC)
    # 30 warm-up bars with vol=100 — VR = 1.0 every bar (flat, not rising)
    for i in range(30):
        bars.append(Bar(t=t0 + timedelta(minutes=2 * i),
                        o=100, h=100.1, l=99.9, c=100, v=100))
    # Bar 31: small dip in volume, so VR_prev < 1
    bars.append(Bar(t=t0 + timedelta(minutes=2 * 30),
                    o=100, h=100.1, l=99.9, c=100, v=80))
    # Bar 32: spike — VR jumps above prev (which was ~0.8)
    bars.append(Bar(t=t0 + timedelta(minutes=2 * 31),
                    o=100, h=100.1, l=99.9, c=100, v=200))
    last = None
    for bar in bars:
        last = eng.update(bar)
    assert last is not None
    assert last.vr > 0.85
    assert last.vr_rising is True
    assert last.vr_ok is True


def test_volume_gate_passes_when_rising_check_disabled():
    eng = GoNoGoEngine(GoNoGoConfig(require_vr_rising=False, vr_thr=0.85))
    last = None
    for bar in _flat(40, vol=100):
        last = eng.update(bar)
    assert last is not None
    # Constant volume = VR ≈ 1.0 ≥ 0.85, and we don't need it to rise
    assert last.vr_ok is True


# ---------- gate 3: slope alignment ----------


def test_slopes_align_up_in_uptrend():
    eng = GoNoGoEngine()
    bars = _flat(30, vol=100)
    t0 = bars[-1].t + timedelta(minutes=2)
    for i in range(20):
        c = 100.0 + i * 0.5
        bars.append(Bar(t=t0 + timedelta(minutes=2 * i),
                        o=c, h=c + 0.1, l=c - 0.1, c=c, v=100 + i * 10))
    last = None
    for bar in bars:
        last = eng.update(bar)
    assert last is not None
    assert last.slope_fast > 0
    assert last.slope_slow > 0
    assert last.up_aligned is True
    assert last.down_aligned is False


def test_slopes_align_down_in_downtrend():
    eng = GoNoGoEngine()
    bars = _flat(30, price=200.0, vol=100)
    t0 = bars[-1].t + timedelta(minutes=2)
    for i in range(20):
        c = 200.0 - i * 0.5
        bars.append(Bar(t=t0 + timedelta(minutes=2 * i),
                        o=c, h=c + 0.1, l=c - 0.1, c=c, v=100 + i * 10))
    last = None
    for bar in bars:
        last = eng.update(bar)
    assert last is not None
    assert last.slope_fast < 0
    assert last.slope_slow < 0
    assert last.down_aligned is True
    assert last.up_aligned is False


# ---------- composite ----------


def test_long_signal_when_all_gates_pass():
    eng = GoNoGoEngine()
    bars = _flat(30, vol=100)
    t0 = bars[-1].t + timedelta(minutes=2)
    # Up-trend that establishes separation and slope alignment
    for i in range(18):
        c = 100.0 + i * 0.5
        bars.append(Bar(t=t0 + timedelta(minutes=2 * i),
                        o=c, h=c + 0.1, l=c - 0.1, c=c, v=100))
    # Final two bars: dip then spike volume so vr_rising is true
    c = 100.0 + 18 * 0.5
    bars.append(Bar(t=t0 + timedelta(minutes=2 * 18),
                    o=c, h=c + 0.1, l=c - 0.1, c=c, v=70))
    c = 100.0 + 19 * 0.5
    bars.append(Bar(t=t0 + timedelta(minutes=2 * 19),
                    o=c, h=c + 0.1, l=c - 0.1, c=c, v=300))
    last = None
    for bar in bars:
        last = eng.update(bar)
    assert last is not None
    assert last.sep_ok is True
    assert last.vr_ok is True
    assert last.up_aligned is True
    assert last.signal == 1


def test_wait_signal_in_chop():
    eng = GoNoGoEngine()
    last = None
    for bar in _flat(50, vol=100):
        last = eng.update(bar)
    assert last is not None
    assert last.signal == 0
