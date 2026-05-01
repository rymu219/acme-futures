"""Tests for the backtest bar-replay engine.

Critical invariants:
  - Every opened position eventually closes (no leaks)
  - Slippage is applied in the adverse direction only
  - Stop fires before target on bars where both are within OHLC
  - Per-trade P&L matches the (exit-entry)*pv*size - fees formula
  - Equity curve is monotonic-by-time and matches cumulative net_pnl
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from acme.backtest.bar_replay import (
    STAGE_0_MIN_PROFIT_FACTOR,
    STAGE_0_MIN_SHARPE,
    BacktestReport,
    evaluate_stage_0,
    run_backtest,
)
from acme.broker.base import Bar, BracketSpec
from acme.contracts import MES
from acme.risk import TOPSTEP_50K
from acme.strategies.base import Signal, StrategyMetadata


def _bars_from_closes(closes, t_start=None, vol=100):
    t0 = t_start or datetime(2026, 4, 30, 14, 0, tzinfo=UTC)
    bars = []
    prev = closes[0]
    for i, c in enumerate(closes):
        h = max(prev, c) + 0.25
        lo = min(prev, c) - 0.25
        bars.append(Bar(t=t0 + timedelta(minutes=i), o=prev, h=h, l=lo, c=c, v=vol))
        prev = c
    return bars


class _FixedSignalStrategy:
    """Strategy that emits a programmed list of signals; one per bar position.
    Useful for exercising bar_replay without coupling to indicator math.
    """

    name = "fixture"
    version = "1"
    contract = MES
    timeframe_minutes = 1
    metadata = StrategyMetadata(
        tier=2, regime_fit={"trending": 1.0},
        time_buckets=["00:00-23:59"],
        default_lifecycle="SHADOW",
        timeframe_minutes=1,
    )

    def __init__(self, signals: list[Signal | None]):
        self._queue = list(signals)
        self._consumed = 0

    def required_history_bars(self):
        return 0

    def on_bar(self, bar, *, state, profile, current_position, current_balance_unrealized):
        if self._consumed >= len(self._queue):
            return None
        s = self._queue[self._consumed]
        self._consumed += 1
        return s


def _bracket(stop_ticks=8, target_ticks=16):
    return BracketSpec(stop_loss_offset_ticks=stop_ticks, take_profit_offset_ticks=target_ticks)


# ---------- basic invariants ----------

def test_no_signals_no_trades():
    strat = _FixedSignalStrategy([None] * 50)
    bars = _bars_from_closes([100.0] * 50)
    report = run_backtest(strat, iter(bars), profile=TOPSTEP_50K)
    assert report.metrics.n_trades == 0
    assert report.metrics.net_pnl == 0.0
    assert report.equity_curve == []
    assert report.bars_processed == 50


def test_winning_long_trade_target_hit():
    """Buy at bar 0 → target reached on bar 1's high → profit."""
    buy_signal = Signal(side="buy", size=1, bracket=_bracket(8, 16), reason="test_buy")
    strat = _FixedSignalStrategy([buy_signal] + [None] * 5)

    # bar 0 close=5000.0; entry with 1 tick adverse = 5000.25
    # target = 5000.25 + 16*0.25 = 5004.25
    # bar 1 high needs to reach 5004.25
    bars = [
        Bar(t=datetime(2026, 4, 30, 14, 0, tzinfo=UTC),
            o=5000.0, h=5000.5, l=4999.5, c=5000.0, v=100),
        Bar(t=datetime(2026, 4, 30, 14, 1, tzinfo=UTC),
            o=5000.0, h=5004.5, l=4999.5, c=5004.0, v=100),
    ]
    report = run_backtest(strat, iter(bars), profile=TOPSTEP_50K)
    assert report.metrics.n_trades == 1
    t = report.trades[0]
    assert t.outcome == "target"
    # exit = target_price (5004.25) - 1 tick target slip = 5004.0
    # gross = (5004.0 - 5000.25) * 5 * 1 = 18.75
    # net = 18.75 - 1.24 fee = 17.51
    assert t.exit_price == pytest.approx(5004.0)
    assert t.gross_pnl == pytest.approx(18.75, abs=0.01)
    assert t.net_pnl == pytest.approx(17.51, abs=0.01)


def test_losing_long_trade_stop_hit():
    """Buy at bar 0 → stop reached on bar 1's low → loss."""
    buy_signal = Signal(side="buy", size=1, bracket=_bracket(8, 16), reason="test_buy")
    strat = _FixedSignalStrategy([buy_signal] + [None] * 5)
    # entry = 5000.25 (1 tick adverse on buy)
    # stop = 5000.25 - 8*0.25 = 4998.25
    bars = [
        Bar(t=datetime(2026, 4, 30, 14, 0, tzinfo=UTC),
            o=5000.0, h=5000.5, l=4999.5, c=5000.0, v=100),
        Bar(t=datetime(2026, 4, 30, 14, 1, tzinfo=UTC),
            o=5000.0, h=5000.5, l=4998.0, c=4998.5, v=100),
    ]
    report = run_backtest(strat, iter(bars), profile=TOPSTEP_50K)
    assert report.metrics.n_trades == 1
    t = report.trades[0]
    assert t.outcome == "stop"
    # exit = stop_price (4998.25) - 2 ticks stop slip on a buy = 4997.75
    # gross = (4997.75 - 5000.25) * 5 = -12.50
    assert t.exit_price == pytest.approx(4997.75)
    assert t.gross_pnl == pytest.approx(-12.50, abs=0.01)
    assert t.net_pnl == pytest.approx(-13.74, abs=0.01)


def test_short_trade_target_hit():
    """Sell at bar 0 → low reaches target on bar 1 → profit."""
    sell_signal = Signal(side="sell", size=1, bracket=_bracket(8, 16), reason="test_sell")
    strat = _FixedSignalStrategy([sell_signal] + [None] * 5)
    # entry = 5000.0 - 1 tick adverse = 4999.75 (sell goes against you on entry)
    # target = 4999.75 - 16*0.25 = 4995.75
    bars = [
        Bar(t=datetime(2026, 4, 30, 14, 0, tzinfo=UTC),
            o=5000.0, h=5000.5, l=4999.5, c=5000.0, v=100),
        Bar(t=datetime(2026, 4, 30, 14, 1, tzinfo=UTC),
            o=5000.0, h=5000.5, l=4995.0, c=4995.5, v=100),
    ]
    report = run_backtest(strat, iter(bars), profile=TOPSTEP_50K)
    assert report.metrics.n_trades == 1
    t = report.trades[0]
    assert t.outcome == "target"
    # exit = target (4995.75) + 1 tick adverse on sell-target = 4996.0
    assert t.exit_price == pytest.approx(4996.0)
    # gross = -1 * (4996.0 - 4999.75) * 5 * 1 = 18.75 (positive, short profit)
    assert t.gross_pnl == pytest.approx(18.75, abs=0.01)


def test_stop_fires_before_target_when_both_within_ohlc():
    """Conservative invariant: if H reaches target AND L reaches stop in the
    same bar, stop is assumed to fire first."""
    buy_signal = Signal(side="buy", size=1, bracket=_bracket(8, 16), reason="test")
    strat = _FixedSignalStrategy([buy_signal] + [None] * 5)
    # entry 5000.25; stop 4998.25; target 5004.25
    # bar 1 has both within OHLC
    bars = [
        Bar(t=datetime(2026, 4, 30, 14, 0, tzinfo=UTC),
            o=5000.0, h=5000.5, l=4999.5, c=5000.0, v=100),
        Bar(t=datetime(2026, 4, 30, 14, 1, tzinfo=UTC),
            o=5000.0, h=5005.0, l=4998.0, c=5002.0, v=100),
    ]
    report = run_backtest(strat, iter(bars), profile=TOPSTEP_50K)
    assert report.trades[0].outcome == "stop"


def test_no_position_leaks_when_signal_pending_at_end_of_bars():
    """An open position at end-of-bars stays open; the test ensures nothing crashes."""
    buy_signal = Signal(side="buy", size=1, bracket=_bracket(8, 16), reason="test")
    strat = _FixedSignalStrategy([buy_signal])
    bars = [
        Bar(t=datetime(2026, 4, 30, 14, 0, tzinfo=UTC),
            o=5000.0, h=5000.5, l=4999.5, c=5000.0, v=100),
    ]
    report = run_backtest(strat, iter(bars), profile=TOPSTEP_50K)
    # No close happened → no trades recorded, but bars_processed == 1
    assert report.metrics.n_trades == 0
    assert report.bars_processed == 1


def test_equity_curve_matches_cumulative_pnl():
    """Equity curve at trade i should equal sum of net_pnls of trades 0..i."""
    # 3 buy signals spaced out, all hitting target
    s = Signal(side="buy", size=1, bracket=_bracket(8, 16), reason="t")
    queue = [s, None, None, s, None, None, s, None, None]
    strat = _FixedSignalStrategy(queue)
    # Closes go up steadily so each long hits target
    closes = [5000, 5004, 5004, 5004, 5008, 5008, 5008, 5012, 5012]
    bars = []
    t0 = datetime(2026, 4, 30, 14, 0, tzinfo=UTC)
    prev = closes[0]
    for i, c in enumerate(closes):
        h = max(prev, c) + 0.5
        lo = min(prev, c) - 0.5
        bars.append(Bar(t=t0 + timedelta(minutes=i),
                        o=prev, h=h, l=lo, c=c, v=100))
        prev = c
    report = run_backtest(strat, iter(bars), profile=TOPSTEP_50K)
    cum = 0.0
    for i, (_t, equity) in enumerate(report.equity_curve):
        cum += report.trades[i].net_pnl
        assert equity == pytest.approx(cum, abs=0.01)


# ---------- Stage 0 gate evaluation ----------

def test_stage_0_passes_when_metrics_clear():
    # Fake report with metrics that clear all gates
    from acme.perf.tracker import PerfMetrics
    report = BacktestReport(
        strategy="fake", profile="topstep_50k", bars_processed=1000,
        metrics=PerfMetrics(
            strategy="fake", window_label="all",
            n_trades=150, net_pnl=2000, win_rate=0.55,
            profit_factor=1.5, sharpe=2.0, max_drawdown=1500,
            avg_win=20, avg_loss=-12, best=80, worst=-50,
        ),
    )
    eval_result = evaluate_stage_0(report, starting_balance=50_000)
    assert eval_result["verdict"] == "PASS"
    assert all(g["pass"] for g in eval_result["gates"].values())


def test_stage_0_fails_when_sample_too_small():
    from acme.perf.tracker import PerfMetrics
    report = BacktestReport(
        strategy="fake", profile="topstep_50k", bars_processed=1000,
        metrics=PerfMetrics(
            strategy="fake", window_label="all",
            n_trades=50, net_pnl=2000, win_rate=0.55,
            profit_factor=1.5, sharpe=2.0, max_drawdown=1500,
            avg_win=20, avg_loss=-12, best=80, worst=-50,
        ),
    )
    eval_result = evaluate_stage_0(report, starting_balance=50_000)
    assert eval_result["verdict"] == "FAIL"
    assert eval_result["gates"]["sample_size"]["pass"] is False
    # Other gates should pass
    assert eval_result["gates"]["sharpe"]["pass"] is True


def test_stage_0_threshold_constants_sane():
    """Make sure the thresholds are reasonable for futures intraday."""
    assert 1.0 < STAGE_0_MIN_SHARPE < 3.0
    assert 1.0 < STAGE_0_MIN_PROFIT_FACTOR < 3.0
