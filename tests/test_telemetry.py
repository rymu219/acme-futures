"""Tests for the local sqlite telemetry layer."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from acme.broker.base import Bar, BracketSpec
from acme.strategies.base import Signal
from acme.telemetry import BarEventLogger


def _bar():
    return Bar(t=datetime(2026, 4, 30, 14, 0, tzinfo=UTC),
               o=100.0, h=101.0, l=99.5, c=100.5, v=200)


def _ctx():
    return {
        "volume_ratio_20": 1.5,
        "momentum_5": 0.8,
        "range_vs_atr": 0.6,
        "close_position_in_bar": 0.7,
        "close_vs_ema9": 0.3,
        "close_vs_ema21": 0.5,
        "close_vs_ema50": 0.8,
    }


def test_log_full_mode_writes_every_bar(tmp_path: Path):
    db = tmp_path / "tel.sqlite"
    with BarEventLogger(db, source="backtest", mode="full") as t:
        # No-signal bar
        rid1 = t.log(bar=_bar(), timeframe=5, strategy="ema_cross",
                     signal=None, context=_ctx(), position=0, balance=50_000)
        # Fired bar
        sig = Signal(side="buy", size=2,
                     bracket=BracketSpec(stop_loss_offset_ticks=8, take_profit_offset_ticks=16),
                     reason="ema_cross_up")
        rid2 = t.log(bar=_bar(), timeframe=5, strategy="ema_cross",
                     signal=sig, context=_ctx(), position=0, balance=50_000)
    assert rid1 is not None and rid2 is not None
    assert rid2 == rid1 + 1

    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT id, sig_side, sig_size, fired FROM bar_events ORDER BY id"
    ).fetchall()
    conn.close()
    assert rows == [(rid1, None, None, 0), (rid2, "buy", 2, 1)]


def test_fires_only_mode_skips_non_fires(tmp_path: Path):
    db = tmp_path / "tel.sqlite"
    with BarEventLogger(db, source="backtest", mode="fires_only") as t:
        rid1 = t.log(bar=_bar(), timeframe=5, strategy="ema_cross",
                     signal=None, context=_ctx(), position=0, balance=50_000)
        sig = Signal(side="sell", size=1, reason="x")
        rid2 = t.log(bar=_bar(), timeframe=5, strategy="ema_cross",
                     signal=sig, context=_ctx(), position=0, balance=50_000)
    # First call returns None (skipped); second writes
    assert rid1 is None
    assert rid2 is not None
    conn = sqlite3.connect(str(db))
    n = conn.execute("SELECT count(*) FROM bar_events").fetchone()[0]
    conn.close()
    assert n == 1


def test_off_mode_does_not_create_db(tmp_path: Path):
    db = tmp_path / "tel.sqlite"
    with BarEventLogger(db, source="backtest", mode="off") as t:
        rid = t.log(bar=_bar(), timeframe=5, strategy="ema_cross",
                    signal=None, context=_ctx(), position=0, balance=50_000)
    assert rid is None
    assert not db.exists()


def test_log_outcome_joins_to_bar_event(tmp_path: Path):
    db = tmp_path / "tel.sqlite"
    with BarEventLogger(db, source="backtest", mode="full") as t:
        sig = Signal(side="buy", size=2, reason="x")
        rid = t.log(bar=_bar(), timeframe=5, strategy="ema_cross",
                    signal=sig, context=_ctx(), position=0, balance=50_000)
        t.log_outcome(rid, exit_t=datetime(2026, 4, 30, 14, 30, tzinfo=UTC),
                      exit_price=101.5, net_pnl=18.76, outcome="target")
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT outcome, net_pnl FROM trade_outcomes WHERE bar_event_id=?", (rid,),
    ).fetchone()
    conn.close()
    assert row == ("target", 18.76)


def test_run_id_groups_events(tmp_path: Path):
    db = tmp_path / "tel.sqlite"
    sig = Signal(side="buy", size=1, reason="x")
    with BarEventLogger(db, source="backtest", mode="full", run_id="run-A") as t:
        t.log(bar=_bar(), timeframe=5, strategy="ema_cross",
              signal=sig, context=_ctx(), position=0, balance=50_000)
    with BarEventLogger(db, source="backtest", mode="full", run_id="run-B") as t:
        t.log(bar=_bar(), timeframe=5, strategy="ema_cross",
              signal=sig, context=_ctx(), position=0, balance=50_000)
        t.log(bar=_bar(), timeframe=5, strategy="ema_cross",
              signal=sig, context=_ctx(), position=0, balance=50_000)
    conn = sqlite3.connect(str(db))
    counts = dict(conn.execute(
        "SELECT run_id, count(*) FROM bar_events GROUP BY run_id"
    ).fetchall())
    conn.close()
    assert counts == {"run-A": 1, "run-B": 2}
