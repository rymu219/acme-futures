"""One-shot backfill: replay today's 2m MES bars through RyanSpecV3Engine.

Use case: the live v3_runtime hit a broker error (or wasn't running) and you
want to fill the paper-week gap with engine-faithful synthetic trades. Run
this in place of the live runtime — it shares the same auth session, so
"only one ProjectX instance" is preserved.

Differences vs the live runtime:
  - Pulls completed 2m bars from /api/History/retrieveBars (no streaming).
  - Reconstructs cum_delta from bar OHLCV via the SIZE-weighted bar-tick
    rule: sign(close - prior_close) * volume. This matches OOS-v3
    methodology exactly (PF 2.30 over 78 days), so the engine is invoked
    with FILTER_THRESH_SIZE_WEIGHTED = -2000 regardless of ACME_DELTA_SOURCE.
  - Writes rows with mode='backfill' so they're distinguishable from real
    paper rows in the dashboard / promotion gate.

Run:
  uv run python -m acme.ryan_spec.v3_backfill
  uv run python -m acme.ryan_spec.v3_backfill --date 2026-05-04 --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Any

import structlog
from dotenv import load_dotenv

from acme.broker.base import Bar
from acme.contracts import MES
from acme.db import Db
from acme.ryan_spec.v3_engine import (
    FILTER_THRESH_SIZE_WEIGHTED,
    RyanSpecV3Engine,
)

log = structlog.get_logger(__name__)

CT = timezone(timedelta(hours=-6))   # America/Chicago, ignoring DST
SESSION_OPEN_CT = time(8, 30)
SESSION_END_CT = time(14, 50)
COMMISSION_ROUND_TURN = 0.70


def session_window_utc(d: date) -> tuple[datetime, datetime]:
    """Return (session_open_utc, session_end_utc) for a given CT trading date."""
    open_ct = datetime.combine(d, SESSION_OPEN_CT, tzinfo=CT)
    end_ct = datetime.combine(d, SESSION_END_CT, tzinfo=CT)
    return open_ct.astimezone(UTC), end_ct.astimezone(UTC)


def reconstruct_bar_delta(prior_close: float | None, bar: Bar) -> int:
    """Bar-level size-weighted tick rule. Matches OOS-v3 cum_delta reconstruction."""
    if prior_close is None or bar.c == prior_close:
        return 0
    return int(bar.v) if bar.c > prior_close else -int(bar.v)


def replay(
    bars: list[Bar],
    *,
    contract_point_value: float = MES.point_value,
    commission_round_turn: float = COMMISSION_ROUND_TURN,
) -> list[dict[str, Any]]:
    """Run the engine over `bars` (chronological) and emit completed-trade rows.

    Returns rows ready for `db.insert_ryan_spec_v3_trade` (mode not yet set).
    Open positions at the end of the input are dropped with a warning.
    """
    engine = RyanSpecV3Engine(filter_thresh=FILTER_THRESH_SIZE_WEIGHTED)
    cum_delta = 0
    prior_close: float | None = None
    rows: list[dict[str, Any]] = []
    open_meta: dict[str, Any] | None = None
    current_session_anchor: date | None = None

    for bar in bars:
        # Reset cum_delta at session open (08:30 CT). We mirror the live
        # builder: anchor switches once we cross 08:30 CT.
        bar_ct = bar.t.astimezone(CT)
        anchor = bar_ct.date() if bar_ct.timetz() >= SESSION_OPEN_CT.replace(
            tzinfo=CT
        ) else (bar_ct - timedelta(days=1)).date()
        if anchor != current_session_anchor:
            current_session_anchor = anchor
            cum_delta = 0
            prior_close = None  # don't bleed delta across sessions

        bar_delta = reconstruct_bar_delta(prior_close, bar)
        cum_delta += bar_delta
        prior_close = bar.c

        decision = engine.on_bar(
            bar, bar_delta=bar_delta, cum_delta_session=cum_delta
        )

        if decision.action == "enter" and open_meta is None:
            assert decision.direction is not None
            assert decision.entry_price is not None
            assert decision.stop_price is not None
            assert decision.atr_at_entry is not None
            engine.open_position(
                direction=decision.direction,
                entry_ts=bar.t,
                entry_fill_price=float(decision.entry_price),
                atr_at_entry=float(decision.atr_at_entry),
                cum_delta_at_entry=int(decision.cum_delta_at_entry or 0),
            )
            open_meta = {
                "bar_ts": bar.t,
                "direction": decision.direction,
                "entry_ts": bar.t,
                "entry_price": float(decision.entry_price),
                "stop_price": float(decision.stop_price),
                "cum_delta_at_entry": int(decision.cum_delta_at_entry or 0),
                "atr_at_entry": float(decision.atr_at_entry),
                "entry_idx": len(rows),
                "sign": 1 if decision.direction == "long" else -1,
            }

        elif decision.action == "exit" and open_meta is not None:
            # The engine reports stop hits as bar.l/h crossing pos.stop_price.
            # For backfill we use stop_price as the fill on stop, bar close
            # otherwise. Same convention the OOS simulator used.
            exit_price = (
                open_meta["stop_price"] if decision.reason == "stop"
                else float(bar.c)
            )
            sign = open_meta["sign"]
            pnl_points = (exit_price - open_meta["entry_price"]) * sign
            pnl_dollars = pnl_points * contract_point_value - commission_round_turn
            rows.append({
                "bar_ts": open_meta["bar_ts"].astimezone(UTC).isoformat(),
                "direction": open_meta["direction"],
                "entry_ts": open_meta["entry_ts"].astimezone(UTC).isoformat(),
                "entry_price": open_meta["entry_price"],
                "stop_price": open_meta["stop_price"],
                "cum_delta_at_entry": open_meta["cum_delta_at_entry"],
                "atr_at_entry": open_meta["atr_at_entry"],
                "exit_ts": bar.t.astimezone(UTC).isoformat(),
                "exit_price": float(exit_price),
                "exit_reason": decision.reason,
                "pnl_dollars": float(pnl_dollars),
                "bars_held": engine.position.bars_held if engine.position else None,
            })
            engine.close_position()
            open_meta = None

    if open_meta is not None:
        log.warning("v3_backfill_position_still_open_at_end_of_bars",
                    bar_ts=open_meta["bar_ts"].isoformat(),
                    direction=open_meta["direction"],
                    note="not written; rerun after session_end (14:50 CT) to capture")

    return rows


async def _amain(target_date: date, dry_run: bool, mode: str) -> None:
    from acme.broker.projectx import ProjectXAdapter

    broker = ProjectXAdapter()
    db = Db()
    try:
        await broker.authenticate()
        await broker.get_account()
        contract_id = await broker.resolve_contract("MES")
        log.info("v3_backfill_resolved", contract_id=contract_id, date=str(target_date))

        start_utc, end_utc = session_window_utc(target_date)
        # If the target session is in progress, cap end at now.
        now_utc = datetime.now(UTC)
        end_fetch = min(end_utc + timedelta(minutes=2), now_utc)
        bars = await broker.get_bars(
            contract_id,
            unit=2, unit_number=2,            # 2 = minutes, count = 2
            start=start_utc, end=end_fetch,
        )
        bars.sort(key=lambda b: b.t)
        log.info("v3_backfill_fetched_bars", n=len(bars),
                 first=bars[0].t.isoformat() if bars else None,
                 last=bars[-1].t.isoformat() if bars else None)

        if not bars:
            log.warning("v3_backfill_no_bars_returned")
            return

        rows = replay(bars)
        log.info("v3_backfill_replay_complete", n_trades=len(rows))

        if dry_run:
            for r in rows:
                log.info("v3_backfill_dry_run_row", **r)
            return

        for r in rows:
            r["mode"] = mode
            db.insert_ryan_spec_v3_trade(r)
        log.info("v3_backfill_inserted", n=len(rows), mode=mode)
    finally:
        await broker.aclose()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ryan-Spec v3 same-day backfill")
    p.add_argument(
        "--date",
        help="CT trading date YYYY-MM-DD (default: today in CT)",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Replay and log trades without writing to Supabase",
    )
    p.add_argument(
        "--mode", default="backfill",
        help="Value to write into ryan_spec_v3_trades.mode (default 'backfill')",
    )
    return p.parse_args()


def main() -> None:
    load_dotenv()
    structlog.configure(processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ])
    args = _parse_args()
    target = (
        date.fromisoformat(args.date) if args.date
        else datetime.now(CT).date()
    )
    log.info("v3_backfill_starting", date=str(target),
             dry_run=args.dry_run, mode=args.mode)
    asyncio.run(_amain(target, args.dry_run, args.mode))


if __name__ == "__main__":
    main()
