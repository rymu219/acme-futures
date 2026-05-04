"""Aggregate trade-tagged regime data into the strategy×regime expectancy matrix
and identify coverage gaps in the historical regime sequence.

Phase 4 of C-2.
"""

from __future__ import annotations

import argparse
import math
from collections import defaultdict
from typing import Any

import structlog

from acme.db import Db
from acme.regime.habitat import REGIME_TO_FIT_KEY, eligible_strategies
from acme.registry import StrategyRegistry

log = structlog.get_logger(__name__)


# Strategies + their declared regime_fit weights. Read once from the registry on
# the runner; we accept the registry as input so this module doesn't import
# every strategy class itself.
def compute_regime_performance(db: Db, registry: StrategyRegistry) -> int:
    """Aggregate trade_regime_tags into regime_strategy_performance. Upserts
    one row per (strategy, regime). Returns the number of rows written.
    """
    log.info("regime_perf_aggregation_start")
    # Pull every tagged trade from Supabase, paginated.
    tags: list[dict] = []
    page_size = 1000
    offset = 0
    while True:
        try:
            res = (
                db.client.table("trade_regime_tags")
                .select("strategy, regime_at_entry, pnl, outcome")
                .range(offset, offset + page_size - 1)
                .execute()
            )
        except Exception as e:
            log.error("trade_tags_fetch_failed", error=str(e))
            return 0
        rows = res.data or []
        if not rows:
            break
        tags.extend(rows)
        if len(rows) < page_size:
            break
        offset += page_size
    log.info("trade_tags_loaded", n=len(tags))

    # Build per-strategy regime_fit lookup so we can mark habitat_match.
    fit_by_strategy: dict[str, dict[str, float]] = {}
    for rec in registry.list_active():
        if rec.metadata is not None:
            fit_by_strategy[rec.name] = dict(rec.metadata.regime_fit)

    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in tags:
        if t.get("pnl") is None:
            continue
        grouped[(t["strategy"], t["regime_at_entry"])].append(t)

    written = 0
    for (strategy, regime), trades in grouped.items():
        wins = [t for t in trades if (t.get("pnl") or 0) > 0]
        losses = [t for t in trades if (t.get("pnl") or 0) < 0]
        n_trades = len(trades)
        n_wins = len(wins)
        n_losses = len(losses)
        win_rate = n_wins / n_trades if n_trades else 0.0
        avg_win = sum(float(t["pnl"]) for t in wins) / n_wins if n_wins else 0.0
        avg_loss = sum(float(t["pnl"]) for t in losses) / n_losses if n_losses else 0.0
        gross_win = sum(float(t["pnl"]) for t in wins)
        gross_loss = -sum(float(t["pnl"]) for t in losses)
        profit_factor = gross_win / gross_loss if gross_loss > 0 else None
        expectancy = win_rate * avg_win - (1 - win_rate) * abs(avg_loss)
        # Approx daily-Sharpe over closed trades — assume each trade is one
        # observation; not annualized properly but a useful relative metric.
        pnls = [float(t["pnl"]) for t in trades]
        if len(pnls) >= 2:
            mean_p = sum(pnls) / len(pnls)
            var = sum((p - mean_p) ** 2 for p in pnls) / (len(pnls) - 1)
            std = math.sqrt(var)
            sharpe_approx = (mean_p / std) if std > 0 else 0.0
        else:
            sharpe_approx = 0.0

        # habitat_match: regime maps to a regime_fit key (trending/ranging) AND
        # this strategy's fit for that key is >= 0.7.
        fit_key = REGIME_TO_FIT_KEY.get(regime)
        habitat_match = False
        if fit_key is not None:
            fit = fit_by_strategy.get(strategy, {}).get(fit_key, 0.0)
            habitat_match = fit >= 0.7

        row = {
            "strategy": strategy,
            "regime": regime,
            "trade_count": n_trades,
            "win_count": n_wins,
            "loss_count": n_losses,
            "win_rate": round(win_rate, 4),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
            "expectancy": round(expectancy, 2),
            "sharpe_approx": round(sharpe_approx, 4),
            "habitat_match": habitat_match,
        }
        db.upsert_regime_perf(row)
        written += 1
    log.info("regime_perf_aggregation_done", rows=written)
    return written


def analyze_coverage_gaps(
    db: Db, registry: StrategyRegistry, *, min_confidence: float = 0.4
) -> dict[str, Any]:
    """Walk through every market_regimes row in chronological order and identify
    contiguous periods where the active regime had no eligible strategies (or
    confidence too low to trade). Writes one coverage_gaps row per uncovered
    period and returns a summary dict.
    """
    log.info("coverage_gap_analysis_start")
    # Pull all regimes, paginated.
    regimes: list[dict] = []
    page_size = 1000
    offset = 0
    while True:
        try:
            res = (
                db.client.table("market_regimes")
                .select("ts, regime, confidence, adx, atr_ratio, hurst")
                .order("ts", desc=False)
                .range(offset, offset + page_size - 1)
                .execute()
            )
        except Exception as e:
            log.error("market_regimes_fetch_failed", error=str(e))
            return {}
        rows = res.data or []
        if not rows:
            break
        regimes.extend(rows)
        if len(rows) < page_size:
            break
        offset += page_size
    log.info("market_regimes_loaded", n=len(regimes))

    bars_by_regime: dict[str, int] = defaultdict(int)
    covered_bars_by_regime: dict[str, int] = defaultdict(int)
    gaps_written = 0

    in_gap = False
    gap_regime: str | None = None
    gap_bars = 0
    gap_first: dict | None = None

    for r in regimes:
        regime = r["regime"]
        conf = float(r.get("confidence") or 0.0)
        bars_by_regime[regime] += 1
        eligible = eligible_strategies(registry, regime, conf, min_confidence=min_confidence)
        if eligible:
            covered_bars_by_regime[regime] += 1
            if in_gap and gap_first is not None:
                _write_gap(db, gap_first, gap_bars)
                gaps_written += 1
            in_gap = False
            gap_first = None
            continue
        # uncovered bar
        if not in_gap:
            in_gap = True
            gap_regime = regime
            gap_bars = 1
            gap_first = r
        elif regime == gap_regime:
            gap_bars += 1
        else:
            if gap_first is not None:
                _write_gap(db, gap_first, gap_bars)
                gaps_written += 1
            gap_regime = regime
            gap_bars = 1
            gap_first = r

    if in_gap and gap_first is not None:
        _write_gap(db, gap_first, gap_bars)
        gaps_written += 1

    summary = {
        "regime_bars": dict(bars_by_regime),
        "covered_bars_by_regime": dict(covered_bars_by_regime),
        "gaps_written": gaps_written,
    }
    log.info("coverage_gap_analysis_done", summary=summary)
    return summary


def _write_gap(db: Db, first_row: dict, duration_bars: int) -> None:
    db.insert_coverage_gap({
        "ts": first_row["ts"],
        "regime": first_row["regime"],
        "duration_bars": duration_bars,
        "adx": first_row.get("adx"),
        "atr_ratio": first_row.get("atr_ratio"),
        "hurst": first_row.get("hurst"),
        "notes": None,
    })


def main() -> None:
    p = argparse.ArgumentParser(description="Regime analytics: perf matrix + coverage gaps")
    p.add_argument("--perf-only", action="store_true", help="Only recompute perf matrix")
    p.add_argument("--gaps-only", action="store_true", help="Only re-analyze coverage gaps")
    args = p.parse_args()

    db = Db()
    registry = StrategyRegistry(db=None)
    # Auto-register the seed fleet so habitat_match works without runner running.
    from acme.contracts import MES
    from acme.strategies.anti import AntiStrategy
    from acme.strategies.bb_mr import BollingerMeanReversionStrategy
    from acme.strategies.donchian import DonchianBreakoutStrategy
    from acme.strategies.ema_cross import EmaCrossStrategy
    from acme.strategies.orb import OpeningRangeBreakoutStrategy
    from acme.strategies.supertrend import SupertrendStrategy
    from acme.strategies.turtle_soup import TurtleSoupStrategy
    from acme.strategies.turtles_system2 import TurtlesSystem2Strategy
    seeds = [
        ("ema_cross", EmaCrossStrategy),
        ("anti", AntiStrategy),
        ("orb", OpeningRangeBreakoutStrategy),
        ("donchian", DonchianBreakoutStrategy),
        ("bb_mr", BollingerMeanReversionStrategy),
        ("turtle_soup", TurtleSoupStrategy),
        ("supertrend", SupertrendStrategy),
        ("turtles_system2", TurtlesSystem2Strategy),
    ]
    for name, cls in seeds:
        registry.upsert(name=name, version="1", state="SHADOW", tier=2)
        registry.attach_instance(name, cls(contract=MES))

    if not args.gaps_only:
        compute_regime_performance(db, registry)
    if not args.perf_only:
        analyze_coverage_gaps(db, registry)


if __name__ == "__main__":
    main()
