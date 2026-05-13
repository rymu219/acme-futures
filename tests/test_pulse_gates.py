"""Tests for the PULSE gate stack."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from acme.broker.base import Bar
from acme.strategies.pulse_gates import (
    ExhaustionTracker,
    HTFAlignmentEngine,
    LockoutManager,
    PullbackRisk,
    VolRegimeClassifier,
    ZoneClassifier,
)


def _bars_constant(n: int, *, c: float = 100.0, h: float | None = None,
                   l: float | None = None, v: int = 100) -> list[Bar]:  # noqa: E741 — matches Bar.l field
    h = h if h is not None else c + 0.1
    l = l if l is not None else c - 0.1  # noqa: E741 — matches Bar.l field
    t0 = datetime(2026, 4, 30, 14, 0, tzinfo=UTC)
    return [
        Bar(t=t0 + timedelta(minutes=2 * i), o=c, h=h, l=l, c=c, v=v)
        for i in range(n)
    ]


# ════════════ VolRegimeClassifier ════════════════════════════════════


def test_vol_regime_warm_up():
    cls = VolRegimeClassifier()
    for bar in _bars_constant(10):
        assert cls.update(bar) is None
    assert not cls.is_warm
    # 30 total bars (atr_avg_period=20 + atr_period=4 with overlap)
    for bar in _bars_constant(20)[:20]:
        cls.update(bar)
    assert cls.is_warm


def test_vol_regime_normal_in_flat():
    cls = VolRegimeClassifier()
    last = None
    for bar in _bars_constant(50):
        last = cls.update(bar)
    assert last is not None
    # ATR is constant on flat data → ratio = 1.0 → normal
    assert last.label == "normal"
    assert last.ratio == pytest.approx(1.0, abs=0.01)
    assert last.proj_move_multiplier == 1.0
    assert last.prob_gate_adjustment == 0.0


def test_vol_regime_high_on_expansion():
    cls = VolRegimeClassifier()
    bars = _bars_constant(40)  # warm with tight ranges
    t0 = bars[-1].t + timedelta(minutes=2)
    # Then 8 large-range bars (3x normal range)
    for i in range(8):
        bars.append(Bar(t=t0 + timedelta(minutes=2 * i),
                        o=100, h=100.5, l=99.5, c=100, v=100))
    last = None
    for bar in bars:
        last = cls.update(bar)
    assert last is not None
    assert last.ratio > 1.5
    assert last.label == "high"
    assert last.proj_move_multiplier == 1.25


# ════════════ ZoneClassifier ══════════════════════════════════════════


def test_zone_warm_up():
    cls = ZoneClassifier(window_bars=10)
    for bar in _bars_constant(9):
        assert cls.update(bar) is None
    assert cls.update(_bars_constant(1)[0]) is not None


def test_zone_mid_when_centered():
    cls = ZoneClassifier(window_bars=10, upper_band=0.2)
    # All bars at close=100, range 99.9–100.1 → range_pos ~0.5
    last = None
    for bar in _bars_constant(10):
        last = cls.update(bar)
    assert last is not None
    assert last.label == "mid"
    assert last.zone_ok is True


def test_zone_upper_when_close_at_top():
    cls = ZoneClassifier(window_bars=10, upper_band=0.2)
    # 9 bars at 100, then 1 bar where close pushes to the top of range
    bars = _bars_constant(9)
    t = bars[-1].t + timedelta(minutes=2)
    bars.append(Bar(t=t, o=100, h=101, l=99, c=100.95, v=100))
    last = None
    for bar in bars:
        last = cls.update(bar)
    assert last is not None
    assert last.label == "upper"
    assert last.zone_ok is False


def test_zone_invalid_upper_band():
    with pytest.raises(ValueError):
        ZoneClassifier(upper_band=0.6)


# ════════════ PullbackRisk ════════════════════════════════════════════


def test_pullback_flagged_on_flat_slope():
    pr = PullbackRisk.evaluate(slope=0.01, rvol=1.5, slope_band=0.02)
    assert pr.slope_too_flat is True
    assert pr.flagged is True


def test_pullback_flagged_on_low_rvol():
    pr = PullbackRisk.evaluate(slope=0.10, rvol=0.5, rvol_band=0.9)
    assert pr.rvol_too_low is True
    assert pr.flagged is True


def test_pullback_clean_on_healthy_signal():
    pr = PullbackRisk.evaluate(slope=0.10, rvol=1.5)
    assert pr.flagged is False


# ════════════ ExhaustionTracker ══════════════════════════════════════


def test_exhaustion_up_on_big_move():
    et = ExhaustionTracker(lookback_bars=4, atr_mult=1.25)
    for close in [100, 100, 100, 100]:
        assert et.update(close, atr=1.0) is None
    # 5th update: lookback close was 100, current 105, move 5 > 1.25
    out = et.update(105.0, atr=1.0)
    assert out is not None
    assert out.exhausted_up is True
    assert out.exhausted_down is False


def test_exhaustion_down_on_big_drop():
    et = ExhaustionTracker(lookback_bars=4, atr_mult=1.25)
    for close in [100, 100, 100, 100]:
        et.update(close, atr=1.0)
    out = et.update(95.0, atr=1.0)
    assert out is not None
    assert out.exhausted_down is True
    assert out.exhausted_up is False


def test_exhaustion_clean_on_small_move():
    et = ExhaustionTracker(lookback_bars=4, atr_mult=1.25)
    for close in [100, 100, 100, 100]:
        et.update(close, atr=2.0)
    out = et.update(101.0, atr=2.0)
    assert out is not None
    assert out.exhausted_up is False
    assert out.exhausted_down is False


# ════════════ LockoutManager ═════════════════════════════════════════


def test_lockout_triggers_after_n_losses():
    lm = LockoutManager(max_consec_losses=2, lockout_minutes=30)
    t = datetime(2026, 4, 30, 14, 0, tzinfo=UTC)
    lm.record_loss(t)
    assert not lm.is_locked(t)
    lm.record_loss(t)
    assert lm.is_locked(t)


def test_lockout_clears_after_window():
    lm = LockoutManager(max_consec_losses=2, lockout_minutes=30)
    t = datetime(2026, 4, 30, 14, 0, tzinfo=UTC)
    lm.record_loss(t)
    lm.record_loss(t)
    assert lm.is_locked(t)
    # 31 min later
    t2 = t + timedelta(minutes=31)
    assert not lm.is_locked(t2)


def test_lockout_resets_on_win():
    lm = LockoutManager(max_consec_losses=3, lockout_minutes=30)
    t = datetime(2026, 4, 30, 14, 0, tzinfo=UTC)
    lm.record_loss(t)
    lm.record_loss(t)
    assert lm.state.consec_losses == 2
    lm.record_win(t)
    assert lm.state.consec_losses == 0
    assert not lm.is_locked(t)


def test_lockout_disabled():
    lm = LockoutManager(max_consec_losses=2, lockout_minutes=30, enabled=False)
    t = datetime(2026, 4, 30, 14, 0, tzinfo=UTC)
    lm.record_loss(t)
    lm.record_loss(t)
    assert not lm.is_locked(t)


def test_lockout_invalid_config():
    with pytest.raises(ValueError):
        LockoutManager(max_consec_losses=0)


# ════════════ HTFAlignmentEngine ═════════════════════════════════════


def test_htf_permissive_before_warm():
    eng = HTFAlignmentEngine()
    # No HTF bars fed yet → aligned() returns True (permissive)
    assert eng.aligned(p_long=0.6, p_short=0.4) is True
    assert not eng.is_warm


def test_htf_aligns_on_bullish_regime():
    eng = HTFAlignmentEngine(ema_fast=2, ema_slow=4, vol_ma=3, rvol_floor=0.8)
    # Feed HTF bars climbing with rising volume
    t0 = datetime(2026, 4, 30, 14, 0, tzinfo=UTC)
    closes = [100, 100, 100.5, 101.0, 101.5, 102.0, 102.5]
    vols = [100, 100, 100, 110, 120, 130, 140]
    for i, (c, v) in enumerate(zip(closes, vols, strict=True)):
        eng.update_htf(Bar(t=t0 + timedelta(minutes=5 * i),
                           o=c, h=c + 0.1, l=c - 0.1, c=c, v=v))
    assert eng.is_warm
    assert eng.latest is not None
    assert eng.latest.htf_bullish is True
    # LTF prefers long → aligned
    assert eng.aligned(p_long=0.6, p_short=0.4) is True
    # LTF prefers short → misaligned (HTF says bullish)
    assert eng.aligned(p_long=0.4, p_short=0.6) is False


def test_htf_aligns_on_bearish_regime():
    eng = HTFAlignmentEngine(ema_fast=2, ema_slow=4, vol_ma=3, rvol_floor=0.8)
    t0 = datetime(2026, 4, 30, 14, 0, tzinfo=UTC)
    closes = [102, 102, 101.5, 101.0, 100.5, 100.0, 99.5]
    vols = [100, 100, 100, 110, 120, 130, 140]
    for i, (c, v) in enumerate(zip(closes, vols, strict=True)):
        eng.update_htf(Bar(t=t0 + timedelta(minutes=5 * i),
                           o=c, h=c + 0.1, l=c - 0.1, c=c, v=v))
    assert eng.latest is not None
    assert eng.latest.htf_bearish is True
    assert eng.aligned(p_long=0.4, p_short=0.6) is True
    assert eng.aligned(p_long=0.6, p_short=0.4) is False
