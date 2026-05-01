"""Tests for the B3 composite-score arbitrator."""

from datetime import datetime

import pytest

from acme.broker.base import BracketSpec
from acme.conductor.arbitrator import (
    ArbitrationContext,
    _Candidate,
    _composite_score,
    arbitrate,
)
from acme.strategies.base import Signal, StrategyMetadata


def _meta(tier=2, regime_fit=None, time_buckets=None):
    return StrategyMetadata(
        tier=tier,
        regime_fit=regime_fit or {"trending": 1.0, "ranging": 0.3, "volatile": 0.5, "quiet": 0.4},
        time_buckets=time_buckets or ["08:30-14:45"],
        default_lifecycle="SHADOW",
        timeframe_minutes=5,
    )


def _sig(side="buy", size=1, reason="test"):
    return Signal(
        side=side, size=size,
        bracket=BracketSpec(stop_loss_offset_ticks=8, take_profit_offset_ticks=16),
        reason=reason,
    )


def _cand(name="alpha", tier=2, regime_fit=None, time_buckets=None):
    return _Candidate(
        name=name, signal=_sig(),
        metadata=_meta(tier=tier, regime_fit=regime_fit, time_buckets=time_buckets),
        tier=tier,
    )


def _ct_at(hour=10, minute=0):
    """Construct a CT-aware datetime by building UTC and converting via tz."""
    from zoneinfo import ZoneInfo
    return datetime(2026, 4, 30, hour, minute, tzinfo=ZoneInfo("America/Chicago"))


# ---------- composite score components ----------

def test_tier_1_outscores_tier_3_at_equal_everything():
    ctx = ArbitrationContext(
        regime="trending", now_ct=_ct_at(10, 0),
        confidences={"a": 0.5, "b": 0.5},
    )
    s_a, _ = _composite_score(_cand("a", tier=1), ctx)
    s_b, _ = _composite_score(_cand("b", tier=3), ctx)
    assert s_a > s_b


def test_regime_fit_changes_winner():
    ctx_trend = ArbitrationContext(
        regime="trending", now_ct=_ct_at(10, 0), confidences={"a": 0.5, "b": 0.5},
    )
    ctx_range = ArbitrationContext(
        regime="ranging", now_ct=_ct_at(10, 0), confidences={"a": 0.5, "b": 0.5},
    )
    trend_strat = _cand("a", tier=2, regime_fit={"trending": 1.0, "ranging": 0.0})
    range_strat = _cand("b", tier=2, regime_fit={"trending": 0.0, "ranging": 1.0})
    s_trend_in_trend, _ = _composite_score(trend_strat, ctx_trend)
    s_range_in_trend, _ = _composite_score(range_strat, ctx_trend)
    assert s_trend_in_trend > s_range_in_trend
    s_trend_in_range, _ = _composite_score(trend_strat, ctx_range)
    s_range_in_range, _ = _composite_score(range_strat, ctx_range)
    assert s_range_in_range > s_trend_in_range


def test_confidence_breaks_a_tie():
    ctx = ArbitrationContext(
        regime="trending", now_ct=_ct_at(10, 0),
        confidences={"a": 0.9, "b": 0.1},
    )
    s_a, _ = _composite_score(_cand("a"), ctx)
    s_b, _ = _composite_score(_cand("b"), ctx)
    # Tier, regime, time bucket all equal — confidence makes the difference
    assert s_a > s_b


def test_time_bucket_fit_outside_window_lowers_score():
    in_window = _cand("a", time_buckets=["08:30-14:45"])
    out_window = _cand("b", time_buckets=["02:00-04:00"])  # current time 10:00 won't match
    ctx = ArbitrationContext(
        regime="trending", now_ct=_ct_at(10, 0),
        confidences={"a": 0.5, "b": 0.5},
    )
    s_in, _ = _composite_score(in_window, ctx)
    s_out, _ = _composite_score(out_window, ctx)
    assert s_in > s_out


def test_unknown_regime_defaults_to_quiet():
    ctx_none = ArbitrationContext(regime=None, now_ct=_ct_at(10, 0), confidences={})
    ctx_quiet = ArbitrationContext(regime="quiet", now_ct=_ct_at(10, 0), confidences={})
    cand = _cand("a")
    s_none, _ = _composite_score(cand, ctx_none)
    s_quiet, _ = _composite_score(cand, ctx_quiet)
    assert s_none == s_quiet


# ---------- arbitrate() winner selection ----------

def test_no_candidates_returns_no_winner():
    ctx = ArbitrationContext(regime="trending", now_ct=_ct_at(10, 0), confidences={})
    r = arbitrate([], ctx)
    assert r.winner is None
    assert "no_signals" in r.notes


def test_single_candidate_wins():
    ctx = ArbitrationContext(regime="trending", now_ct=_ct_at(10, 0),
                             confidences={"alpha": 0.5})
    r = arbitrate([_cand("alpha")], ctx)
    assert r.has_winner
    assert r.winner.strategy == "alpha"
    assert r.suppressed == []


def test_higher_score_wins_logs_others_as_suppressed():
    ctx = ArbitrationContext(
        regime="trending", now_ct=_ct_at(10, 0),
        confidences={"a": 0.9, "b": 0.5, "c": 0.1},
    )
    r = arbitrate([_cand("a", tier=1), _cand("b", tier=2), _cand("c", tier=3)], ctx)
    assert r.winner.strategy == "a"
    suppressed_names = {s.strategy for s in r.suppressed}
    assert suppressed_names == {"b", "c"}
    # Each suppressed signal carries its score breakdown
    for s in r.suppressed:
        assert "tier_weight" in s.score_breakdown
        assert "confidence" in s.score_breakdown


def test_tie_broken_by_lexicographic_name():
    """Two identical strategies → lower name wins deterministically."""
    ctx = ArbitrationContext(
        regime="trending", now_ct=_ct_at(10, 0),
        confidences={"alpha": 0.5, "beta": 0.5},
    )
    r = arbitrate([_cand("beta"), _cand("alpha")], ctx)
    assert r.winner.strategy == "alpha"
    assert r.suppressed[0].strategy == "beta"


def test_winner_score_is_sum_of_components():
    """Sanity: composite = 3*tier + 2*regime + 1.5*tb + 1*conf."""
    ctx = ArbitrationContext(
        regime="trending", now_ct=_ct_at(10, 0),
        confidences={"a": 0.5},
    )
    cand = _cand("a", tier=2,
                 regime_fit={"trending": 1.0},
                 time_buckets=["08:30-14:45"])
    r = arbitrate([cand], ctx)
    # tier_weight(2)=0.66, regime=1, tb=1, conf=0.5
    # composite = 3*0.66 + 2*1 + 1.5*1 + 1*0.5 = 1.98 + 2 + 1.5 + 0.5 = 5.98
    assert r.winner.composite_score == pytest.approx(5.98, abs=0.01)
