"""Tests for PerfTracker math: Sharpe, profit factor, drawdown, win rate,
windowing semantics.
"""

from datetime import UTC, datetime, timedelta

from acme.perf.tracker import PerfRegistry, PerfTracker, _max_drawdown


def _t(secs: int = 0) -> datetime:
    return datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=secs)


# ---------- _max_drawdown ----------

def test_max_drawdown_empty():
    assert _max_drawdown([]) == 0.0


def test_max_drawdown_all_winners():
    assert _max_drawdown([10, 20, 30]) == 0.0


def test_max_drawdown_one_loser():
    # cum: 10, 30, 20 → peak=30, trough=20 → dd=10
    assert _max_drawdown([10, 20, -10]) == 10.0


def test_max_drawdown_recovers_then_drops_again():
    # cum: 10, -5, 5, -10, 0, -20
    pnls = [10, -15, 10, -15, 10, -20]
    # peaks: 10, 10, 10, 10, 10, 10
    # cum:   10,  -5,  5, -10,  0, -20
    # dd:     0,  15,  5,  20, 10,  30  → max=30
    assert _max_drawdown(pnls) == 30.0


# ---------- PerfTracker basic metrics ----------

def test_empty_tracker_has_zero_metrics():
    pt = PerfTracker(strategy="x")
    m = pt.metrics()
    assert m.n_trades == 0
    assert m.net_pnl == 0.0
    assert m.win_rate == 0.0
    assert m.sharpe == 0.0
    assert m.profit_factor is None
    assert m.max_drawdown == 0.0


def test_single_winning_trade():
    pt = PerfTracker(strategy="x")
    pt.record_close(net_pnl=20.0, side="buy", outcome="target",
                    entry_price=5000, exit_price=5004, closed_at=_t(0))
    m = pt.metrics()
    assert m.n_trades == 1
    assert m.net_pnl == 20.0
    assert m.win_rate == 1.0
    assert m.profit_factor is None     # no losing trades to divide by
    assert m.avg_win == 20.0
    assert m.avg_loss == 0.0
    assert m.best == 20.0
    assert m.worst == 20.0


def test_mixed_winners_and_losers():
    pt = PerfTracker(strategy="x")
    for i, p in enumerate([20, -10, 30, -5, 15]):
        pt.record_close(net_pnl=p, side="buy", outcome="target",
                        entry_price=5000, exit_price=5004, closed_at=_t(i))
    m = pt.metrics()
    assert m.n_trades == 5
    assert m.net_pnl == 50.0
    assert m.win_rate == 0.6   # 3 of 5
    # gross_wins = 65, gross_losses = 15 → PF = 65/15 ≈ 4.33
    assert abs(m.profit_factor - (65.0 / 15.0)) < 0.001
    assert m.avg_win == (20 + 30 + 15) / 3
    assert m.avg_loss == (-10 + -5) / 2
    assert m.best == 30
    assert m.worst == -10


def test_sharpe_is_zero_when_all_trades_identical():
    pt = PerfTracker(strategy="x")
    for i in range(5):
        pt.record_close(net_pnl=10.0, side="buy", outcome="target",
                        entry_price=5000, exit_price=5004, closed_at=_t(i))
    m = pt.metrics()
    # zero variance → sharpe defined as 0 in our impl
    assert m.sharpe == 0.0


def test_sharpe_positive_for_consistent_winners():
    pt = PerfTracker(strategy="x")
    # mostly winners with some variation
    for i, p in enumerate([10, 12, 8, 11, 9, 13]):
        pt.record_close(net_pnl=p, side="buy", outcome="target",
                        entry_price=5000, exit_price=5004, closed_at=_t(i))
    m = pt.metrics()
    assert m.sharpe > 0


def test_sharpe_negative_for_consistent_losers():
    pt = PerfTracker(strategy="x")
    for i, p in enumerate([-10, -12, -8, -11, -9, -13]):
        pt.record_close(net_pnl=p, side="buy", outcome="stop",
                        entry_price=5000, exit_price=5004, closed_at=_t(i))
    m = pt.metrics()
    assert m.sharpe < 0


def test_max_drawdown_in_metrics():
    pt = PerfTracker(strategy="x")
    for i, p in enumerate([10, 20, -50]):
        pt.record_close(net_pnl=p, side="buy", outcome="target",
                        entry_price=5000, exit_price=5004, closed_at=_t(i))
    m = pt.metrics()
    # cum: 10, 30, -20 → peak 30, trough -20 → dd 50
    assert m.max_drawdown == 50.0


# ---------- Window semantics ----------

def test_window_uses_n_trades_when_more_recent():
    """If we have 100 trades all within the last hour, the n-trades window
    (default 30) should bound the metric, not the days window.
    """
    pt = PerfTracker(strategy="x", window_n_trades=30, window_n_days=5)
    for i in range(50):
        pt.record_close(net_pnl=10.0, side="buy", outcome="target",
                        entry_price=5000, exit_price=5004, closed_at=_t(i * 60))
    m = pt.metrics(now=_t(50 * 60 + 1))
    # All 50 trades are within 5 days, so days-window includes all 50;
    # n-window only includes last 30. We pick whichever is larger.
    assert m.n_trades == 50


def test_window_uses_days_when_n_outside_days():
    """If trades are spread over a week but only 30 happened in the last 5 days,
    we should pick the days-bound (more accurate to current performance).
    """
    pt = PerfTracker(strategy="x", window_n_trades=30, window_n_days=5)
    # Trades 0–9: 10 days ago
    for i in range(10):
        pt.record_close(net_pnl=100.0, side="buy", outcome="target",
                        entry_price=5000, exit_price=5004,
                        closed_at=_t(0) - timedelta(days=10) + timedelta(minutes=i))
    # Trades 10–14: today
    for i in range(5):
        pt.record_close(net_pnl=10.0, side="buy", outcome="target",
                        entry_price=5000, exit_price=5004, closed_at=_t(i))
    # Days window (last 5d): just the recent 5 trades
    # n window (last 30): all 15 trades
    m = pt.metrics(now=_t(60))
    assert m.n_trades == 15


# ---------- Registry ----------

def test_registry_per_strategy_isolation():
    reg = PerfRegistry()
    reg.record_close(strategy="alpha", net_pnl=10, side="buy", outcome="target",
                     entry_price=5000, exit_price=5004, closed_at=_t(0))
    reg.record_close(strategy="beta", net_pnl=-5, side="sell", outcome="stop",
                     entry_price=5000, exit_price=5004, closed_at=_t(0))
    assert reg.metrics_for("alpha").net_pnl == 10
    assert reg.metrics_for("beta").net_pnl == -5
    assert reg.metrics_for("ghost") is None


def test_registry_all_metrics():
    reg = PerfRegistry()
    reg.record_close(strategy="a", net_pnl=10, side="buy", outcome="target",
                     entry_price=5000, exit_price=5004, closed_at=_t(0))
    reg.record_close(strategy="b", net_pnl=20, side="buy", outcome="target",
                     entry_price=5000, exit_price=5004, closed_at=_t(0))
    metrics = reg.all_metrics()
    assert len(metrics) == 2
    names = {m.strategy for m in metrics}
    assert names == {"a", "b"}


# ---------- Backfill from db ----------

class _FakeDb:
    """Minimal fake replicating the supabase-py table().select().eq().order().execute() chain."""
    def __init__(self, rows):
        self.rows = rows
        self.client = self

    def table(self, _name):
        return self

    def select(self, _cols):
        return self

    def eq(self, _col, _val):
        return self

    def order(self, _col, **_kw):
        return self

    def execute(self):
        class _Res:
            pass
        r = _Res()
        r.data = self.rows
        return r


def test_backfill_replays_historical_closes():
    """Past dry_run_close events in Supabase should be replayed into PerfTracker."""
    rows = [
        {"strategy": "ema_cross", "occurred_at": "2026-04-29T22:30:00Z",
         "raw": {"net_pnl": 18.76, "entry_price": 7172.75, "exit_price": 7176.75,
                 "outcome": "target"}},
        {"strategy": "ema_cross", "occurred_at": "2026-04-29T23:15:00Z",
         "raw": {"net_pnl": -11.24, "entry_price": 7180.0, "exit_price": 7178.0,
                 "outcome": "stop"}},
        {"strategy": "anti", "occurred_at": "2026-04-30T01:00:00Z",
         "raw": {"net_pnl": 30.0, "entry_price": 7150.0, "exit_price": 7156.0,
                 "outcome": "target"}},
    ]
    reg = PerfRegistry()
    n = reg.backfill_from_db(_FakeDb(rows))
    assert n == 3
    assert reg.metrics_for("ema_cross").n_trades == 2
    assert reg.metrics_for("ema_cross").net_pnl == pytest.approx(7.52, abs=0.01)
    assert reg.metrics_for("anti").n_trades == 1
    assert reg.metrics_for("anti").net_pnl == 30.0


def test_backfill_handles_empty_db():
    reg = PerfRegistry()
    n = reg.backfill_from_db(_FakeDb([]))
    assert n == 0
    assert len(reg) == 0


def test_backfill_skips_malformed_rows():
    """Bad rows shouldn't crash the backfill; they're silently skipped."""
    rows = [
        {"strategy": "good", "occurred_at": "2026-04-30T01:00:00Z",
         "raw": {"net_pnl": 10.0, "entry_price": 100, "exit_price": 102, "outcome": "target"}},
        {"strategy": "bad_no_raw", "occurred_at": "2026-04-30T01:00:00Z"},
        {"strategy": "bad_no_ts",
         "raw": {"net_pnl": 5.0, "entry_price": 100, "exit_price": 101, "outcome": "target"}},
    ]
    reg = PerfRegistry()
    n = reg.backfill_from_db(_FakeDb(rows))
    # Both bad rows should fail gracefully; only "good" gets recorded
    assert n == 1
    assert reg.metrics_for("good").n_trades == 1


def test_backfill_returns_zero_with_none_db():
    reg = PerfRegistry()
    assert reg.backfill_from_db(None) == 0


# Need pytest import for one of the new tests
import pytest  # noqa: E402
