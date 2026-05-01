"""Thin entry-point that wires everything into a Conductor and runs forever.

All trading logic lives in `acme.conductor.Conductor`. This module's only job
is to construct the dependencies (broker, db, registry, config), register the
active strategies, and start the loop.

Modes:
  default   — submits real market orders against the configured broker
  --dry-run — phantom-position simulation; no orders submitted
"""

from __future__ import annotations

import argparse
import asyncio

import structlog

from acme.conductor.conductor import Conductor
from acme.config import Config, load_config
from acme.contracts import MES
from acme.db import Db
from acme.registry import StrategyRegistry
from acme.strategies.anti import AntiStrategy
from acme.strategies.bb_mr import BollingerMeanReversionStrategy
from acme.strategies.donchian import DonchianBreakoutStrategy
from acme.strategies.ema_cross import EmaCrossStrategy
from acme.strategies.orb import OpeningRangeBreakoutStrategy

log = structlog.get_logger(__name__)


def _build_registry(db: Db) -> StrategyRegistry:
    """Load strategies from Supabase and attach runtime instances.

    Seed fleet (B2):
      - ema_cross   (PILOT)  — Phase A bot, validated by Combine round-trip
      - anti        (SHADOW) — Raschke's stochastic pullback
      - orb         (SHADOW) — Opening Range Breakout
      - donchian    (SHADOW) — Turtles System 1 first-of-day
      - bb_mr       (SHADOW) — Bollinger mean-reversion (range regime contrarian)
    """
    registry = StrategyRegistry(db=db)
    try:
        registry.load_from_db()
    except Exception as e:
        log.warning("registry_load_failed", error=str(e),
                    note="proceeding with in-memory defaults only")

    seeds = [
        ("ema_cross", "PILOT", 2, {"fast": 9, "slow": 21, "stop_ticks": 8, "target_ticks": 16},
         "Phase A bot — auto-registered by runner",
         lambda: EmaCrossStrategy(contract=MES)),
        ("anti", "SHADOW", 1, {"trend_ema_period": 20, "fast_k_period": 5, "slow_k_period": 14},
         "Raschke's Anti — stochastic pullback in trend (5m)",
         lambda: AntiStrategy(contract=MES)),
        ("orb", "SHADOW", 2, {"or_minutes": 15, "volume_multiple": 1.2},
         "Opening Range Breakout, 15-min OR, volume-confirmed (5m)",
         lambda: OpeningRangeBreakoutStrategy(contract=MES)),
        ("donchian", "SHADOW", 2, {"lookback": 20, "atr_stop_multiple": 2.0},
         "Donchian System 1, first breakout per day (5m)",
         lambda: DonchianBreakoutStrategy(contract=MES)),
        ("bb_mr", "SHADOW", 3, {"bb_period": 20, "rsi_period": 2, "adx_max_for_range": 20.0},
         "Bollinger MR, range regime contrarian (5m)",
         lambda: BollingerMeanReversionStrategy(contract=MES)),
    ]
    for name, default_state, tier, params, notes, builder in seeds:
        if name not in registry:
            registry.upsert(name=name, version="1", state=default_state, tier=tier,
                            params=params, notes=notes)
        registry.attach_instance(name, builder())
    return registry


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Acme Futures live runner")
    p.add_argument("--dry-run", action="store_true",
                   help="Log intended orders to Supabase without submitting them")
    return p.parse_args()


async def _amain(dry_run: bool) -> None:
    from acme.broker.projectx import ProjectXAdapter

    config: Config = load_config()
    db = Db()
    registry = _build_registry(db)
    broker = ProjectXAdapter()
    conductor = Conductor(broker, db, config, registry, dry_run=dry_run)
    try:
        await conductor.run_forever()
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
