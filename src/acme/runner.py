"""Multi-variant entry-point: one ProjectX connection, N parallel V3Runtime
instances, each running its own engine configuration.

Why multi-variant: today's analysis (docs/2026-05-05-trading-day-analysis.md)
showed v3-canon's PF degraded from OOS 2.21 to 1.08, primarily because the
filter is firing at cum_delta extremes far outside its calibrated range
(median |cum_delta| at entry was 16k vs 670 threshold) and opposite_signal
is exiting too eagerly (88% of exits, vs OOS 53%). Rather than picking one
fix, we ship four variant hypotheses alongside the canonical configuration
and let live shadow data compare them:

  - v3-canon    — control, OOS-validated config
  - v3-trail    — trailing stop / let-winners-run (option A from the brief)
  - v3-min2bar  — refuse opposite_signal exits before bar 2
  - v3-armor    — suppress opposite_signal exits when MFE >= 2 ATR
  - v3-pctile   — dynamic filter using 5th/95th percentile of recent cum_delta

All five share ONE ProjectXAdapter (SignalR fan-out at the broker layer
lets them all consume the same market hub connection). Each writes trades
tagged with its strategy_id; each has its own heartbeat / kill-switch row.

Modes:
  default   — submits real market orders
  --dry-run — phantom-position simulation; no orders submitted
"""

from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import dataclass
from typing import Any

import structlog

from acme.db import Db
from acme.ryan_spec.v3_runtime import (
    SESSION_END_CT,
    SESSION_OPEN_CT,
    V3Runtime,
    _parse_hhmm,
)

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class _VariantSpec:
    """One v3 variant's engine-flag configuration."""
    strategy_id: str
    description: str
    # engine flags (defaults match canonical)
    enable_trailing_stop: bool = False
    trail_be_lock_atr_mult: float = 1.0
    trail_atr_mult: float = 1.0
    min_bars_before_opposite_exit: int = 0
    opposite_signal_armor_mfe_atr: float | None = None
    filter_mode: str = "static"
    filter_pctile_window_bars: int = 60
    filter_pctile: float = 5.0
    # Time-of-day exits. Defaults False across the fleet right now — see
    # 2026-05-06: user wants 24-hour shadow data with pure-thesis exits
    # (stop / opposite_signal only). Flip back to True per-variant if you
    # want to honor RTH session_end / time_stop again.
    enable_session_end_exit: bool = False
    enable_time_stop: bool = False


# The fleet. To disable a variant, comment it out or set ACME_V3_VARIANTS env
# var to a comma-separated subset of strategy_ids.
VARIANTS: list[_VariantSpec] = [
    _VariantSpec(
        strategy_id="v3-canon",
        description="Canonical OOS-validated configuration (control)",
    ),
    _VariantSpec(
        strategy_id="v3-trail",
        description="Trailing stop: BE-lock at +1 ATR, trail 1 ATR behind MFE past +2 ATR",
        enable_trailing_stop=True,
        trail_be_lock_atr_mult=1.0,
        trail_atr_mult=1.0,
    ),
    _VariantSpec(
        strategy_id="v3-min2bar",
        description="Refuse opposite_signal exit before bar 2 — drops 1-bar noise trades",
        min_bars_before_opposite_exit=2,
    ),
    _VariantSpec(
        strategy_id="v3-armor",
        description="Suppress opposite_signal when MFE >= 2 ATR — let winners run past first reversal",
        opposite_signal_armor_mfe_atr=2.0,
    ),
    _VariantSpec(
        strategy_id="v3-pctile",
        description="Percentile-based filter: bottom 5% of last 60 bars instead of static -670",
        filter_mode="pctile",
        filter_pctile_window_bars=60,
        filter_pctile=5.0,
    ),
]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Acme Futures multi-variant runner")
    p.add_argument("--dry-run", action="store_true",
                   help="Log intended orders to Supabase without submitting them")
    return p.parse_args()


def _select_variants() -> list[_VariantSpec]:
    """Filter the fleet to a subset if ACME_V3_VARIANTS is set."""
    requested = os.getenv("ACME_V3_VARIANTS", "").strip()
    if not requested:
        return VARIANTS
    wanted = {s.strip() for s in requested.split(",") if s.strip()}
    selected = [v for v in VARIANTS if v.strategy_id in wanted]
    if not selected:
        raise SystemExit(
            f"ACME_V3_VARIANTS={requested!r} matched no known variant. "
            f"Known: {[v.strategy_id for v in VARIANTS]}"
        )
    return selected


def _build_runtime(
    spec: _VariantSpec, broker: Any, db: Db, *, dry_run: bool,
) -> V3Runtime:
    """Construct one V3Runtime configured per the variant spec."""
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
        strategy_id=spec.strategy_id,
        enable_trailing_stop=spec.enable_trailing_stop,
        trail_be_lock_atr_mult=spec.trail_be_lock_atr_mult,
        trail_atr_mult=spec.trail_atr_mult,
        min_bars_before_opposite_exit=spec.min_bars_before_opposite_exit,
        opposite_signal_armor_mfe_atr=spec.opposite_signal_armor_mfe_atr,
        filter_mode=spec.filter_mode,  # type: ignore[arg-type]
        filter_pctile_window_bars=spec.filter_pctile_window_bars,
        filter_pctile=spec.filter_pctile,
        enable_session_end_exit=spec.enable_session_end_exit,
        enable_time_stop=spec.enable_time_stop,
    )


async def _amain(dry_run: bool) -> None:
    from acme.broker.projectx import ProjectXAdapter

    db = Db()
    broker = ProjectXAdapter()
    variants = _select_variants()

    runtimes = [
        _build_runtime(spec, broker, db, dry_run=dry_run) for spec in variants
    ]
    log.info(
        "runner_starting_multi_variant",
        dry_run=dry_run,
        variants=[r.strategy_id for r in runtimes],
        count=len(runtimes),
    )
    for spec in variants:
        log.info("variant_enabled", strategy_id=spec.strategy_id,
                 description=spec.description)

    try:
        # TaskGroup propagates exceptions and cancels siblings on any failure.
        # Each runtime opens its own quote stream consumer queue against the
        # adapter's shared SignalR connection — the fan-out happens inside
        # ProjectXAdapter._market_mux.
        async with asyncio.TaskGroup() as tg:
            for rt in runtimes:
                tg.create_task(rt.run(), name=f"runtime:{rt.strategy_id}")
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
