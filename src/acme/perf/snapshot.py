"""Periodic flush of per-strategy PerfMetrics to Supabase.

Owns the cadence (every N seconds during the live loop, plus on every closed
trade). The conductor wires up the flusher and gives it access to the perf
registry + db.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from acme.db import Db
from acme.perf.scoring import compute_confidence
from acme.perf.tracker import PerfRegistry
from acme.registry import StrategyRegistry

log = structlog.get_logger(__name__)

# Default cadence: a snapshot every 5 minutes during RTH. The conductor also
# fires a snapshot on every dry_run_close, so this is a "no movement" pulse.
DEFAULT_SNAPSHOT_INTERVAL_SEC = 300


def write_snapshot(
    db: Db | None,
    perf_registry: PerfRegistry,
    strat_registry: StrategyRegistry,
) -> int:
    """Write one snapshot row per strategy to Supabase. Returns number written.
    Also updates each strategy's `score` column on the registry table.
    """
    if db is None:
        return 0
    written = 0
    for metrics in perf_registry.all_metrics():
        rec = strat_registry.get(metrics.strategy) if metrics.strategy in strat_registry else None
        lifecycle = rec.state if rec else "SHADOW"
        confidence = compute_confidence(metrics, lifecycle)

        row: dict[str, Any] = {
            "strategy": metrics.strategy,
            "window_label": metrics.window_label,
            "sharpe": metrics.sharpe,
            "max_drawdown": metrics.max_drawdown,
            "profit_factor": metrics.profit_factor,
            "win_rate": metrics.win_rate,
            "n_trades": metrics.n_trades,
            "net_pnl": metrics.net_pnl,
            "raw": {
                **metrics.as_dict(),
                "confidence": round(confidence, 4),
                "lifecycle": lifecycle,
            },
        }
        try:
            db.client.table("strategy_perf_snapshot").insert(row).execute()
            # Update the strategy registry's live confidence score
            strat_registry.set_score(metrics.strategy, confidence)
            written += 1
        except Exception as e:
            log.error("perf_snapshot_failed", strategy=metrics.strategy, error=str(e))
    return written


async def snapshot_loop(
    db: Db | None,
    perf_registry: PerfRegistry,
    strat_registry: StrategyRegistry,
    *,
    interval_sec: int = DEFAULT_SNAPSHOT_INTERVAL_SEC,
) -> None:
    """Background task — write a perf snapshot every `interval_sec` seconds."""
    while True:
        try:
            n = write_snapshot(db, perf_registry, strat_registry)
            if n > 0:
                log.info("perf_snapshot_flushed", n_strategies=n)
        except Exception as e:
            log.error("perf_snapshot_loop_failed", error=str(e))
        await asyncio.sleep(interval_sec)
