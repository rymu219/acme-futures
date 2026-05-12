"""New-fleet runner — wires the 4 Part-2 strategies through the classic
conductor, against one ProjectX broker session.

Replaces `acme.runner` (the 16-variant v3 multi-runtime) with the new
fleet:
  - IGNITION  (GO/NO-GO entry + min-2-bar hold + ATR stop)
  - SESSION   (audit-driven time windows + overnight bias)
  - REGIME    (vol-expansion + trend deadband)
  - BOUNDARY  (level-rejection fade)

All four start in SHADOW; the conductor logs phantom dry-run trades.
Promotion runs through PerfTracker as usual.

Single ProjectX SignalR mux (the conductor owns one BrokerAdapter
instance — no parallel SignalR connections, which TopstepX rejects).

Single position state — the conductor's FlatFirstFSM arbitrates across
the 4 strategies, which is the architectural answer to v3's cluster-
correlation problem (audit §6).

Usage:
    uv run python -m acme.fleet_runner --dry-run     # default
    uv run python -m acme.fleet_runner               # live (NO)
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from pathlib import Path

import structlog

# When invoked as `python -m acme.fleet_runner`, the package is already on
# sys.path. This guard keeps `python scripts/.../fleet_runner.py` direct
# invocation working too.
if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from acme.config import load_config
from acme.conductor.conductor import Conductor
from acme.db import Db
from acme.registry import StrategyRegistry
from acme.strategies.boundary import BoundaryStrategy
from acme.strategies.ignition import IgnitionStrategy
from acme.strategies.regime import RegimeStrategy
from acme.strategies.session import SessionStrategy

log = structlog.get_logger(__name__)


def _build_registry(db: Db) -> StrategyRegistry:
    """Pull the strategies-table rows, then attach Python instances.

    Strategies must have been pre-registered via
    `scripts/register_new_fleet.py --execute`. If a row is missing for
    one of the four, we upsert it on the fly at SHADOW state to be
    fail-safe.
    """
    reg = StrategyRegistry(db=db)
    reg.load_from_db()

    pairs = [
        ("ignition", IgnitionStrategy),
        ("session", SessionStrategy),
        ("regime", RegimeStrategy),
        ("boundary", BoundaryStrategy),
    ]

    for name, cls in pairs:
        if name not in {s.name for s in reg.list_all()}:
            log.warning("fleet_runner_missing_registry_row_upserting", name=name)
            inst = cls()
            reg.upsert(
                name=inst.name, version=inst.version,
                state=inst.metadata.default_lifecycle,
                tier=inst.metadata.tier, params={},
                notes="auto-upserted by fleet_runner on startup",
            )
        reg.attach_instance(name, cls())

    return reg


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Acme Futures — new-fleet runner")
    p.add_argument("--dry-run", action="store_true",
                   help="Phantom-position simulation; no broker orders.")
    return p.parse_args()


async def _amain(dry_run: bool) -> None:
    """Outer retry loop. SignalR connections can be closed by the server
    (observed 2026-05-12: 47 silent runner exits in 12 hours). Rather
    than let the python process exit, catch + log + retry the whole
    conductor.run_forever() call. Backoff is bounded to avoid hammering
    the broker; the watchdog supervises on top of this for catastrophic
    failures."""
    from acme.broker.projectx import ProjectXAdapter
    from acme.telemetry import BarEventLogger

    config = load_config()
    db = Db()
    registry_db = db   # capture once for clarity

    backoff_s = 5
    max_backoff_s = 60
    attempt = 0
    while True:
        attempt += 1
        broker = ProjectXAdapter()
        registry = _build_registry(registry_db)
        active = [s.name for s in registry.list_active()]
        log.info("fleet_runner_starting",
                 attempt=attempt, dry_run=dry_run, live=config.live,
                 strategies=active)
        if not dry_run and config.live:
            log.warning("fleet_runner_live_mode_active")

        conductor = Conductor(
            broker=broker, db=db, config=config, registry=registry,
            dry_run=dry_run,
            telemetry=BarEventLogger(source="fleet_runner_live",
                                      mode="full" if dry_run else "off"),
        )

        try:
            await conductor.run_forever()
            log.info("fleet_runner_run_forever_returned_normally",
                     attempt=attempt)
        except asyncio.CancelledError:
            log.info("fleet_runner_cancelled", attempt=attempt)
            await broker.aclose()
            raise
        except Exception as e:
            log.error("fleet_runner_run_forever_raised",
                      attempt=attempt, error=str(e), exc_info=True)
        finally:
            with contextlib.suppress(Exception):
                await broker.aclose()

        # Don't return — retry with exponential backoff. The watchdog
        # is still supervising; if we keep crashing it'll kill the
        # whole process eventually.
        log.warning("fleet_runner_retry_scheduled",
                    attempt=attempt, backoff_s=backoff_s)
        await asyncio.sleep(backoff_s)
        backoff_s = min(backoff_s * 2, max_backoff_s)


def main() -> None:
    args = _parse_args()
    try:
        asyncio.run(_amain(dry_run=args.dry_run))
    except KeyboardInterrupt:
        log.info("fleet_runner_interrupted")


if __name__ == "__main__":
    main()
