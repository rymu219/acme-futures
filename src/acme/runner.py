"""Thin entry-point that wires the Ryan-Spec v3 runtime into one process and
runs forever.

Why v3-only: ProjectX rejects a second concurrent SignalR market-hub
connection on the same JWT, so running the Conductor and v3 side-by-side
crashed the runner in a loop. Per the user's call, we ship v3 and park the
Conductor's 8 strategies until we add SignalR fan-out at the broker layer.

The Conductor's seed registry, regime engine, and strategy classes still
live in the repo — re-introduce them here once `ProjectXAdapter.stream_quotes`
multiplexes a single connection across multiple consumers.

Modes:
  default   — submits real market orders against the configured broker
  --dry-run — phantom-position simulation; no orders submitted
"""

from __future__ import annotations

import argparse
import asyncio
import os

import structlog

from acme.db import Db
from acme.ryan_spec.v3_runtime import (
    SESSION_END_CT,
    SESSION_OPEN_CT,
    V3Runtime,
    _parse_hhmm,
)

log = structlog.get_logger(__name__)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Acme Futures live runner (v3-only)")
    p.add_argument("--dry-run", action="store_true",
                   help="Log intended orders to Supabase without submitting them")
    return p.parse_args()


def _build_v3_runtime(broker, db: Db, *, dry_run: bool) -> V3Runtime:
    """Build the Ryan-Spec v3 runtime with the configured broker connection.

    Reads its own env vars (delta source, contract, risk size, session times)
    so the runtime stays configurable without touching this entry point.
    """
    contract = os.getenv("ACME_CONTRACT_SYMBOL", "MES")
    delta_source = os.getenv("ACME_DELTA_SOURCE", "quote")
    if delta_source not in ("quote", "trade"):
        raise SystemExit(f"Invalid ACME_DELTA_SOURCE={delta_source!r}")
    risk = int(os.getenv("ACME_RISK_CONTRACTS", "1") or "1")
    session_open = _parse_hhmm(
        os.getenv("ACME_SESSION_OPEN_CT") or SESSION_OPEN_CT.strftime("%H:%M"),
        field_name="ACME_SESSION_OPEN_CT",
    )
    session_end = _parse_hhmm(
        os.getenv("ACME_SESSION_END_CT") or SESSION_END_CT.strftime("%H:%M"),
        field_name="ACME_SESSION_END_CT",
    )
    return V3Runtime(
        broker=broker, db=db,
        contract_symbol=contract,
        mode="paper" if dry_run else "live",
        delta_source=delta_source,  # type: ignore[arg-type]
        risk_contracts=risk,
        session_open_ct=session_open,
        session_end_ct=session_end,
        dry_run=dry_run,
    )


async def _amain(dry_run: bool) -> None:
    from acme.broker.projectx import ProjectXAdapter

    db = Db()
    broker = ProjectXAdapter()
    v3 = _build_v3_runtime(broker, db, dry_run=dry_run)
    log.info("runner_starting_v3_only",
             dry_run=dry_run, contract=v3.contract_symbol,
             delta_source=v3.delta_source)
    try:
        await v3.run()
    finally:
        await broker.aclose()


def main() -> None:
    args = _parse_args()
    if args.dry_run:
        log.info("runner_dry_run_mode", note="orders will be LOGGED but NOT submitted")
    try:
        asyncio.run(_amain(dry_run=args.dry_run))
    except KeyboardInterrupt:
        log.info("runner_stopped_by_user")


if __name__ == "__main__":
    main()
