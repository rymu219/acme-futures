"""Thin entry-point that wires everything into a Conductor and runs forever.

All trading logic lives in `acme.conductor.Conductor` plus the standalone
`acme.ryan_spec.v3_runtime.V3Runtime`. This module's only job is to construct
the shared dependencies (one broker connection, one Db, one registry, one
config), register the active strategies, and start both loops in parallel.

Modes:
  default   — submits real market orders against the configured broker
  --dry-run — phantom-position simulation; no orders submitted (applies to
              both the Conductor's 8 strategies and Ryan-Spec v3)

Single ProjectX connection is shared across the Conductor and v3 so we don't
hit per-credential connection limits — each strategy keeps its own phantom
P&L bucket via Supabase rows tagged with its strategy name.
"""

from __future__ import annotations

import argparse
import asyncio
import os

import structlog

from acme.conductor.conductor import Conductor
from acme.config import Config, load_config
from acme.contracts import MES
from acme.db import Db
from acme.registry import StrategyRegistry
from acme.ryan_spec.v3_runtime import (
    SESSION_END_CT,
    SESSION_OPEN_CT,
    V3Runtime,
    _parse_hhmm,
)
from acme.strategies.anti import AntiStrategy
from acme.strategies.bb_mr import BollingerMeanReversionStrategy
from acme.strategies.donchian import DonchianBreakoutStrategy
from acme.strategies.ema_cross import EmaCrossStrategy
from acme.strategies.orb import OpeningRangeBreakoutStrategy
from acme.strategies.supertrend import SupertrendStrategy
from acme.strategies.turtle_soup import TurtleSoupStrategy
from acme.strategies.turtles_system2 import TurtlesSystem2Strategy

log = structlog.get_logger(__name__)


def _build_registry(db: Db) -> StrategyRegistry:
    """Load strategies from Supabase and attach runtime instances.

    Seed fleet (B2):
      - ema_cross   (PILOT)  — Phase A bot, validated by Combine round-trip
      - anti        (SHADOW) — Raschke's stochastic pullback
      - orb         (SHADOW) — Opening Range Breakout
      - donchian    (SHADOW) — Turtles System 1 first-of-day
      - bb_mr       (SHADOW) — Bollinger mean-reversion (range regime contrarian)
      - turtle_soup    (SHADOW) — Raschke fade of failed Donchian breakouts
      - supertrend     (SHADOW) — ATR-based trend follower
      - turtles_system2 (SHADOW) — slower 55-bar Donchian variant
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
        ("turtle_soup", "SHADOW", 2,
         {"lookback": 20, "atr_stop_multiple": 1.0, "atr_target_multiple": 1.5},
         "Raschke TurtleSoup — fade failed 20-bar Donchian breakouts (5m)",
         lambda: TurtleSoupStrategy(contract=MES)),
        ("supertrend", "SHADOW", 2,
         {"atr_period": 10, "multiplier": 3.0},
         "ATR-based Supertrend trend follower, ATR(10)x3.0 (5m)",
         lambda: SupertrendStrategy(contract=MES)),
        ("turtles_system2", "SHADOW", 2,
         {"lookback": 55, "atr_period": 20, "atr_stop_multiple": 2.5, "atr_target_multiple": 4.0},
         "Turtles System 2 — slower 55-bar breakout variant (5m)",
         lambda: TurtlesSystem2Strategy(contract=MES)),
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


def _build_v3_runtime(broker, db: Db, *, dry_run: bool) -> V3Runtime:
    """Build the Ryan-Spec v3 runtime with the shared broker connection.

    Reads its own env vars (delta source, contract, risk size, session times)
    so it stays configurable independently of the Conductor's parameters.
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
    from acme.regime.classifier import RegimeEngine

    config: Config = load_config()
    db = Db()
    registry = _build_registry(db)
    # ONE ProjectX connection shared by the Conductor and Ryan-Spec v3.
    # Both run as concurrent async tasks against the same broker; each keeps
    # its own phantom P&L bucket (Conductor → broker_events / perf tables;
    # v3 → ryan_spec_v3_trades).
    broker = ProjectXAdapter()
    regime_engine = RegimeEngine(timeframe_minutes=5)
    conductor = Conductor(
        broker, db, config, registry,
        dry_run=dry_run,
        regime_engine=regime_engine,
    )
    v3 = _build_v3_runtime(broker, db, dry_run=dry_run)
    log.info("runner_starting_v3_in_parallel",
             dry_run=dry_run, contract=v3.contract_symbol,
             delta_source=v3.delta_source)
    try:
        # TaskGroup propagates exceptions and cancels siblings on any failure.
        async with asyncio.TaskGroup() as tg:
            tg.create_task(conductor.run_forever(), name="conductor")
            tg.create_task(v3.run(), name="ryan_spec_v3")
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
