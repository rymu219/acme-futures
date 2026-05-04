"""Per-bar strategy event log → local sqlite at ~/.acme/telemetry.sqlite.

Each row captures what one strategy saw on one bar — bar OHLCV, the universal
market-context features computed by acme.context.MarketContext, the signal
decision (or absence thereof), and the strategy's phantom position state at
that moment. A separate trade_outcomes table joins fires to their eventual
exits so the Inspector can label each fire as win/loss without re-running.

This is intentionally local sqlite, not Supabase: per-bar volume would chew
through the free Supabase tier, and offline replay over a backtest doesn't
need network round-trips on the hot path.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from acme.broker.base import Bar
from acme.strategies.base import Signal

DEFAULT_DB_PATH = Path.home() / ".acme" / "telemetry.sqlite"

Source = Literal["live", "backtest"]
TelemetryMode = Literal["full", "fires_only", "off"]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS bar_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bar_t TEXT NOT NULL,
    timeframe INTEGER NOT NULL,
    strategy TEXT NOT NULL,
    source TEXT NOT NULL,
    run_id TEXT NOT NULL,

    bar_o REAL, bar_h REAL, bar_l REAL, bar_c REAL, bar_v INTEGER,

    sig_side TEXT,
    sig_size INTEGER,
    sig_reason TEXT,

    ctx_volume_ratio_20 REAL,
    ctx_momentum_5 REAL,
    ctx_range_vs_atr REAL,
    ctx_close_position_in_bar REAL,
    ctx_close_vs_ema9 REAL,
    ctx_close_vs_ema21 REAL,
    ctx_close_vs_ema50 REAL,

    position_size INTEGER,
    balance_unrealized REAL,

    fired INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_bar_events_strategy_time ON bar_events(strategy, bar_t);
CREATE INDEX IF NOT EXISTS idx_bar_events_run ON bar_events(run_id);
CREATE INDEX IF NOT EXISTS idx_bar_events_fired ON bar_events(fired) WHERE fired = 1;

CREATE TABLE IF NOT EXISTS trade_outcomes (
    bar_event_id INTEGER PRIMARY KEY REFERENCES bar_events(id),
    exit_t TEXT,
    exit_price REAL,
    net_pnl REAL,
    outcome TEXT
);
"""


def _new_run_id(source: Source) -> str:
    return f"{source}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"


class BarEventLogger:
    """Buffered sqlite writer for per-bar strategy decisions.

    Single writer per process. Use one instance per backtest run or runner
    session. Pass `source="live"` from the conductor, `source="backtest"`
    from bar_replay.
    """

    def __init__(
        self,
        db_path: Path = DEFAULT_DB_PATH,
        *,
        source: Source = "live",
        run_id: str | None = None,
        mode: TelemetryMode = "full",
    ) -> None:
        self.db_path = db_path
        self.source = source
        self.run_id = run_id or _new_run_id(source)
        self.mode = mode
        if mode != "off":
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(db_path))
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        else:
            self._conn = None

    def log(
        self,
        *,
        bar: Bar,
        timeframe: int,
        strategy: str,
        signal: Signal | None,
        context: dict[str, float | None],
        position: int,
        balance: float,
    ) -> int | None:
        """Insert one bar_events row. Returns the row id, or None if write skipped."""
        if self._conn is None:
            return None
        fired = 1 if (signal is not None and signal.size > 0) else 0
        if self.mode == "fires_only" and not fired:
            return None

        cur = self._conn.execute(
            """
            INSERT INTO bar_events (
                bar_t, timeframe, strategy, source, run_id,
                bar_o, bar_h, bar_l, bar_c, bar_v,
                sig_side, sig_size, sig_reason,
                ctx_volume_ratio_20, ctx_momentum_5, ctx_range_vs_atr,
                ctx_close_position_in_bar,
                ctx_close_vs_ema9, ctx_close_vs_ema21, ctx_close_vs_ema50,
                position_size, balance_unrealized, fired
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                bar.t.isoformat(), timeframe, strategy, self.source, self.run_id,
                bar.o, bar.h, bar.l, bar.c, bar.v,
                signal.side if signal else None,
                signal.size if signal else None,
                signal.reason if signal else None,
                context.get("volume_ratio_20"),
                context.get("momentum_5"),
                context.get("range_vs_atr"),
                context.get("close_position_in_bar"),
                context.get("close_vs_ema9"),
                context.get("close_vs_ema21"),
                context.get("close_vs_ema50"),
                position, balance, fired,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def log_outcome(
        self,
        bar_event_id: int,
        *,
        exit_t: datetime,
        exit_price: float,
        net_pnl: float,
        outcome: str,
    ) -> None:
        if self._conn is None:
            return
        self._conn.execute(
            """
            INSERT OR REPLACE INTO trade_outcomes (bar_event_id, exit_t, exit_price, net_pnl, outcome)
            VALUES (?, ?, ?, ?, ?)
            """,
            (bar_event_id, exit_t.isoformat(), exit_price, net_pnl, outcome),
        )
        self._conn.commit()

    def flush(self) -> None:
        if self._conn is not None:
            self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.commit()
            self._conn.close()
            self._conn = None

    def __enter__(self) -> BarEventLogger:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
