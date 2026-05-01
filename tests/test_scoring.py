"""Tests for the confidence formula and promotion gates."""

from acme.perf.scoring import (
    AUTO_BENCH_CONFIDENCE,
    LIFECYCLE_FLOOR,
    compute_confidence,
    is_eligible_for_promotion,
    should_auto_bench,
)
from acme.perf.tracker import PerfMetrics


def _metrics(
    *,
    n_trades=20, net_pnl=100, win_rate=0.55, profit_factor=1.5,
    sharpe=1.2, max_drawdown=50.0, avg_win=20.0, avg_loss=-10.0,
    best=30.0, worst=-15.0,
):
    return PerfMetrics(
        strategy="x", window_label="test",
        n_trades=n_trades, net_pnl=net_pnl, win_rate=win_rate,
        profit_factor=profit_factor, sharpe=sharpe,
        max_drawdown=max_drawdown, avg_win=avg_win, avg_loss=avg_loss,
        best=best, worst=worst,
    )


# ---------- compute_confidence ----------

def test_confidence_in_range():
    """Confidence is always in [0, 1] regardless of inputs."""
    extremes = [
        _metrics(sharpe=10.0, profit_factor=10.0, n_trades=1000, max_drawdown=0.0),
        _metrics(sharpe=-5.0, profit_factor=0.1, n_trades=0, max_drawdown=10000.0),
    ]
    for m in extremes:
        for state in ("SHADOW", "PILOT", "LIVE"):
            c = compute_confidence(m, state)
            assert 0.0 <= c <= 1.0


def test_confidence_zero_metrics_floors_to_lifecycle_only():
    """An empty-metric strategy gets only the lifecycle term + the
    profit_factor=None neutral term.
    """
    m = _metrics(n_trades=0, net_pnl=0, win_rate=0,
                 profit_factor=None, sharpe=0, max_drawdown=0,
                 avg_win=0, avg_loss=0, best=0, worst=0)
    c = compute_confidence(m, "SHADOW")
    # Expected: 0.40*0 + 0.25*0.5(neutral PF) + 0.15*0 + 0.10*1 (no DD = full health)
    #           + 0.10*0.3 (SHADOW floor) = 0.125 + 0.10 + 0.03 = 0.255
    assert abs(c - 0.255) < 0.01


def test_confidence_lifecycle_floor_increases_score():
    m = _metrics()
    c_shadow = compute_confidence(m, "SHADOW")
    c_paper = compute_confidence(m, "PILOT")
    c_live = compute_confidence(m, "LIVE")
    assert c_shadow < c_paper < c_live


def test_confidence_high_sharpe_dominates():
    """A strategy with sharpe >= 2 maxes the sharpe term (40% of total)."""
    high_sharpe = _metrics(sharpe=2.5, profit_factor=2.0, n_trades=100,
                           max_drawdown=0)
    c = compute_confidence(high_sharpe, "PILOT")
    # 0.40*1.0 + 0.25*1.0 + 0.15*1.0 + 0.10*1.0 + 0.10*0.5 = 0.95
    assert c > 0.85


def test_confidence_breaks_on_unprofitable():
    """A profit factor < 1 (lose more than win) drives PF term to 0."""
    losing = _metrics(profit_factor=0.5, sharpe=-0.5, n_trades=50,
                      max_drawdown=200)
    c = compute_confidence(losing, "PILOT")
    assert c < 0.30


# ---------- promotion eligibility ----------

def test_promotion_shadow_to_paper_threshold():
    assert is_eligible_for_promotion(0.55, "SHADOW", "PILOT")
    assert is_eligible_for_promotion(0.99, "SHADOW", "PILOT")
    assert not is_eligible_for_promotion(0.54, "SHADOW", "PILOT")


def test_promotion_paper_to_live_threshold():
    assert is_eligible_for_promotion(0.65, "PILOT", "LIVE")
    assert not is_eligible_for_promotion(0.64, "PILOT", "LIVE")


def test_promotion_invalid_path_returns_false():
    """Confidence cannot promote across illegal transitions."""
    assert not is_eligible_for_promotion(0.99, "BENCH", "LIVE")
    assert not is_eligible_for_promotion(0.99, "SHADOW", "LIVE")  # must go via PILOT


# ---------- auto-bench ----------

def test_auto_bench_only_for_paper_or_live():
    low = _metrics(n_trades=20, sharpe=-2.0, profit_factor=0.3, max_drawdown=500)
    # SHADOW strategies don't get auto-benched (they're already "on the bench")
    should, _ = should_auto_bench(low, 0.10, "SHADOW")
    assert not should
    # PILOT strategy with same metrics gets benched
    should, reason = should_auto_bench(low, 0.10, "PILOT")
    assert should
    assert "below floor" in reason


def test_auto_bench_requires_min_sample():
    """A LIVE strategy with n=5 trades doesn't get benched even with low confidence."""
    low = _metrics(n_trades=5)
    should, _ = should_auto_bench(low, 0.05, "LIVE")
    assert not should


def test_auto_bench_does_not_fire_when_above_floor():
    high = _metrics(n_trades=50)
    should, _ = should_auto_bench(high, 0.50, "LIVE")
    assert not should


# ---------- LIFECYCLE_FLOOR table sanity ----------

def test_lifecycle_floor_monotonic():
    """Higher lifecycle states should have higher floor values."""
    assert LIFECYCLE_FLOOR["SHADOW"] < LIFECYCLE_FLOOR["PILOT"] < LIFECYCLE_FLOOR["LIVE"]
    assert LIFECYCLE_FLOOR["BENCH"] == 0.0
    assert LIFECYCLE_FLOOR["RETIRED"] == 0.0


# ---------- AUTO_BENCH_CONFIDENCE constant sanity ----------

def test_auto_bench_floor_is_low():
    """The auto-bench floor should be well below all promotion thresholds."""
    assert AUTO_BENCH_CONFIDENCE < 0.55
    assert AUTO_BENCH_CONFIDENCE < 0.65
