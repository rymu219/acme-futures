"""Retroactive regime classification + trade tagging.

Phase 3 of C-2:
  1. Iterate historical 1-min bars (Databento cache via acme.backtest.data.iter_bars).
  2. Aggregate to 5-min bars.
  3. Run RegimeEngine.on_bar() on each 5m bar — classifies, returns RegimeSnapshot.
  4. Batch-insert snapshots into Supabase market_regimes.
  5. After regimes are persisted: tag every fire row in the local sqlite telemetry
     with the regime active at its bar_t (writes to Supabase trade_regime_tags).
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog

from acme.backtest.data import iter_bars
from acme.broker.base import Bar
from acme.calendar import CT, in_econ_blackout
from acme.conductor.bar_aggregator import BarAggregator
from acme.db import Db
from acme.regime.classifier import RegimeEngine
from acme.telemetry import DEFAULT_DB_PATH

log = structlog.get_logger(__name__)


def _is_news_blackout(t: datetime) -> bool:
    """Use the existing calendar to flag bars in NFP/FOMC blackout windows."""
    try:
        now_ct = t.astimezone(CT) if t.tzinfo else t
        blackout, _ = in_econ_blackout(now_ct)
        return blackout
    except Exception:
        return False


def backfill_regimes(
    *,
    start: datetime,
    end: datetime,
    db: Db,
    timeframe_minutes: int = 5,
    batch_size: int = 500,
) -> int:
    """Stream 1-min bars from start→end, aggregate to N-min, classify, batch-insert.

    Returns the total number of regime snapshots written.
    """
    engine = RegimeEngine(timeframe_minutes=timeframe_minutes)
    aggregator = BarAggregator(timeframe_minutes=timeframe_minutes)

    pending: list[dict] = []
    total = 0
    last_log = 0

    for one_min in iter_bars(start=start, end=end):
        # Aggregate the 1-min close into the longer timeframe via the same
        # tick-style API the live conductor uses (close-price as synthetic tick).
        roll_t = one_min.t + timedelta(minutes=1) - timedelta(microseconds=1)
        five_min: Bar | None = aggregator.add_tick(roll_t, one_min.c)
        if five_min is None:
            continue
        # The bar_aggregator builds OHLC from close ticks only — replace with the
        # actual 1-min OHLC accumulated over the window for realistic ATR/range.
        # Simplest: classify on the close-only synthetic bar; ATR uses h-l so we
        # patch a more realistic one by looking at the 1-min bar's range.
        snap = engine.on_bar(five_min, news_blackout=_is_news_blackout(five_min.t))
        pending.append(snap.to_db_row())
        total += 1

        if len(pending) >= batch_size:
            db.insert_regime_snapshots(pending)
            pending = []
        if total - last_log >= 5000:
            log.info("regime_backfill_progress", written=total, current_ts=str(snap.ts))
            last_log = total

    if pending:
        db.insert_regime_snapshots(pending)
    log.info("regime_backfill_done", total=total)
    return total


def backfill_regimes_from_1min(
    *,
    start: datetime,
    end: datetime,
    db: Db,
    timeframe_minutes: int = 5,
    batch_size: int = 500,
) -> int:
    """Higher-quality variant: build the 5-min OHLC from 1-min OHLC directly
    (preserving real high/low) instead of a tick-style aggregator. Use this for
    backfill so ATR-derived features reflect real bar ranges.
    """
    engine = RegimeEngine(timeframe_minutes=timeframe_minutes)
    pending: list[dict] = []
    total = 0
    last_log = 0

    bucket_start: datetime | None = None
    bucket_o = bucket_h = bucket_l = bucket_c = 0.0
    bucket_v = 0
    bucket_count = 0

    def _flush_bucket() -> None:
        nonlocal pending, total, last_log
        if bucket_start is None or bucket_count == 0:
            return
        bar = Bar(
            t=bucket_start, o=bucket_o, h=bucket_h, l=bucket_l, c=bucket_c, v=bucket_v
        )
        snap = engine.on_bar(bar, news_blackout=_is_news_blackout(bar.t))
        pending.append(snap.to_db_row())
        total += 1
        if len(pending) >= batch_size:
            db.insert_regime_snapshots(pending)
            pending = []
        if total - last_log >= 5000:
            log.info("regime_backfill_progress", written=total, current_ts=str(bar.t))
            last_log = total

    for one_min in iter_bars(start=start, end=end):
        bucket_floor = _floor_minute(one_min.t, timeframe_minutes)
        if bucket_start is None or bucket_floor != bucket_start:
            _flush_bucket()
            bucket_start = bucket_floor
            bucket_o, bucket_h, bucket_l, bucket_c = one_min.o, one_min.h, one_min.l, one_min.c
            bucket_v = one_min.v
            bucket_count = 1
        else:
            bucket_h = max(bucket_h, one_min.h)
            bucket_l = min(bucket_l, one_min.l)
            bucket_c = one_min.c
            bucket_v += one_min.v
            bucket_count += 1

    _flush_bucket()
    if pending:
        db.insert_regime_snapshots(pending)
    log.info("regime_backfill_done", total=total)
    return total


def _floor_minute(t: datetime, tf_min: int) -> datetime:
    minutes = (t.hour * 60 + t.minute) // tf_min * tf_min
    return t.replace(hour=minutes // 60, minute=minutes % 60, second=0, microsecond=0)


def tag_trades_from_telemetry(
    *,
    db: Db,
    sqlite_path: Path = DEFAULT_DB_PATH,
) -> int:
    """For every fire in ~/.acme/telemetry.sqlite (bar_events.fired=1), look up
    the most-recent regime in Supabase market_regimes and write a row to
    trade_regime_tags. Idempotent — uses upsert on trade_id.

    To avoid hammering Supabase with 1000+ point queries, pulls all market_regimes
    rows into memory once and joins in Python.
    """
    if not sqlite_path.exists():
        log.warning("telemetry_sqlite_missing", path=str(sqlite_path))
        return 0

    # Pull regime snapshots into memory, ordered by ts ascending.
    log.info("loading_market_regimes")
    regimes: list[dict] = []
    page_size = 1000
    offset = 0
    while True:
        try:
            res = (
                db.client.table("market_regimes")
                .select("ts, regime, regime_direction, adx, atr_ratio, hurst, confidence")
                .order("ts", desc=False)
                .range(offset, offset + page_size - 1)
                .execute()
            )
        except Exception as e:
            log.error("market_regimes_fetch_failed", error=str(e))
            return 0
        rows = res.data or []
        if not rows:
            break
        regimes.extend(rows)
        if len(rows) < page_size:
            break
        offset += page_size

    if not regimes:
        log.warning("no_market_regimes_found")
        return 0
    log.info("market_regimes_loaded", n=len(regimes))

    # Index by ts (string ISO) for fast bisect-style lookup.
    regime_ts: list[str] = [r["ts"] for r in regimes]
    import bisect

    def regime_at_or_before(ts_iso: str) -> dict | None:
        # bisect_right: rightmost insertion point. We want the largest ts <= query.
        idx = bisect.bisect_right(regime_ts, ts_iso) - 1
        if idx < 0:
            return None
        return regimes[idx]

    # Pull fires from telemetry sqlite.
    conn = sqlite3.connect(str(sqlite_path))
    try:
        cur = conn.execute(
            """
            SELECT e.id, e.bar_t, e.strategy, o.net_pnl, o.outcome
            FROM bar_events e
            LEFT JOIN trade_outcomes o ON o.bar_event_id = e.id
            WHERE e.fired = 1
            ORDER BY e.bar_t
            """
        )
        fires = cur.fetchall()
    finally:
        conn.close()
    log.info("fires_loaded", n=len(fires))

    written = 0
    skipped = 0
    pending: list[dict] = []
    for fire_id, bar_t, strategy, net_pnl, outcome in fires:
        # Normalize bar_t to ISO-compatible — sqlite stores Bar.t.isoformat()
        bar_t_iso = bar_t
        snap = regime_at_or_before(bar_t_iso)
        if snap is None:
            skipped += 1
            continue
        if outcome is None:
            outcome_label = None
        elif net_pnl is None:
            outcome_label = "scratch"
        else:
            outcome_label = "win" if net_pnl > 0 else ("loss" if net_pnl < 0 else "scratch")
        pending.append({
            "trade_id": str(fire_id),
            "strategy": strategy,
            "entry_ts": bar_t_iso,
            "regime_at_entry": snap["regime"],
            "regime_direction": snap.get("regime_direction"),
            "adx_at_entry": snap.get("adx"),
            "atr_ratio_at_entry": snap.get("atr_ratio"),
            "hurst_at_entry": snap.get("hurst"),
            "confidence_at_entry": snap.get("confidence"),
            "pnl": net_pnl,
            "outcome": outcome_label,
        })
        if len(pending) >= 500:
            for row in pending:
                db.upsert_trade_regime_tag(row)
            written += len(pending)
            pending = []

    for row in pending:
        db.upsert_trade_regime_tag(row)
    written += len(pending)
    log.info("trade_tagging_done", written=written, skipped=skipped)
    return written


def _parse_iso(s: str) -> datetime:
    if "T" in s:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    return datetime.fromisoformat(s + "T00:00:00+00:00").replace(tzinfo=UTC)


def main() -> None:
    p = argparse.ArgumentParser(description="Regime backfill + trade tagging")
    p.add_argument("--start", default="2024-04-01", help="Backfill start date (UTC)")
    p.add_argument("--end", default="2026-04-30", help="Backfill end date (UTC)")
    p.add_argument("--timeframe", type=int, default=5, help="Bar timeframe in minutes (default 5)")
    p.add_argument("--regimes-only", action="store_true",
                   help="Only regenerate market_regimes, skip trade tagging")
    p.add_argument("--tags-only", action="store_true",
                   help="Only re-tag trades; assume market_regimes is already populated")
    p.add_argument("--snapshot-aggregator", action="store_true",
                   help="Use the close-tick aggregator instead of OHLC bucket aggregator (faster, less precise)")
    args = p.parse_args()

    db = Db()

    if not args.tags_only:
        start = _parse_iso(args.start)
        end = _parse_iso(args.end)
        if args.snapshot_aggregator:
            n = backfill_regimes(start=start, end=end, db=db, timeframe_minutes=args.timeframe)
        else:
            n = backfill_regimes_from_1min(start=start, end=end, db=db, timeframe_minutes=args.timeframe)
        log.info("regime_backfill_complete", snapshots=n)

    if not args.regimes_only:
        n = tag_trades_from_telemetry(db=db)
        log.info("trade_tagging_complete", tagged=n)


if __name__ == "__main__":
    main()
