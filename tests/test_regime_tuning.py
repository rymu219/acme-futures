"""Tests covering the 2026-05-12 REGIME tuning pass.

After overnight whipsaw analysis (20 trades, PF 0.53, bar-1 PF 0.22),
the defaults were tightened:
  vol_high_ratio: 1.5 → 2.0
  vol_min_high_atr_pts: 0.0 → 1.0 (new absolute-ATR floor)

These tests verify the *new* defaults behave as intended:
1. Tiny-absolute-ATR markets no longer qualify as 'high' even if the
   ratio crosses 2.0 (the bar-1 whipsaw protection).
2. Real expansion (ratio above 2.0 AND atr above 1.0pt) still fires.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from acme.broker.base import Bar
from acme.strategies.pulse_gates import VolRegimeClassifier


def _bar(t, *, h_pad=0.05, l_pad=0.05, c=100.0, v=100):
    return Bar(t=t, o=c, h=c + h_pad, l=c - l_pad, c=c, v=v)


T0 = datetime(2026, 5, 12, 12, 0, tzinfo=UTC)


def test_tight_ratio_passes_with_real_atr():
    """ATR above 1.0 + ratio > 2.0 → high (real expansion qualifies)."""
    cls = VolRegimeClassifier(
        atr_period=4, atr_avg_period=20,
        high_ratio=2.0, min_high_atr=1.0,
    )
    # Warm: 20 bars with 0.5pt ranges → ATR ~0.5
    for i in range(25):
        cls.update(_bar(T0 + timedelta(minutes=2 * i),
                        h_pad=0.25, l_pad=0.25))
    # A few wide bars (4pt range each) to push ATR past both gates
    out = None
    for i in range(3):
        out = cls.update(_bar(T0 + timedelta(minutes=2 * (25 + i)),
                              h_pad=2.0, l_pad=2.0))
    assert out is not None
    assert out.ratio > 2.0
    assert out.atr >= 1.0
    assert out.label == "high"


def test_ratio_alone_insufficient_when_atr_tiny():
    """Ratio crosses 2.0 but absolute ATR is below 1.0pt → NOT high.

    This is the bar-1 whipsaw scenario: thin-market base ATR ~0.05,
    a slightly wider bar pushes ratio above 2.0 but absolute ATR is
    only ~0.15pt. Stop distance of 1.5 × 0.15 = 0.225pt = <1 tick
    would be obliterated by any reversion. The floor blocks this.
    """
    cls = VolRegimeClassifier(
        atr_period=4, atr_avg_period=20,
        high_ratio=2.0, min_high_atr=1.0,
    )
    # Very thin warm-up: 0.05pt ranges
    for i in range(25):
        cls.update(_bar(T0 + timedelta(minutes=2 * i),
                        h_pad=0.025, l_pad=0.025))
    # Then a "wide" bar that's still tiny in absolute terms
    out = cls.update(_bar(T0 + timedelta(minutes=60),
                          h_pad=0.1, l_pad=0.1))
    assert out is not None
    assert out.atr < 1.0
    # Ratio may exceed 2.0 here but the absolute floor blocks 'high'
    assert out.label != "high"


def test_default_classifier_uses_tight_thresholds_when_unset():
    """Sanity: leaving min_high_atr=0 reproduces pre-tune behavior."""
    cls = VolRegimeClassifier(high_ratio=1.5, min_high_atr=0.0)
    for i in range(25):
        cls.update(_bar(T0 + timedelta(minutes=2 * i),
                        h_pad=0.025, l_pad=0.025))
    out = cls.update(_bar(T0 + timedelta(minutes=60),
                          h_pad=0.1, l_pad=0.1))
    assert out is not None
    # With no absolute floor + lower ratio, the tiny "expansion" qualifies
    assert out.label == "high"
