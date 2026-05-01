"""Backtest CLI — runs all 5 seed strategies through historical MES and writes
per-strategy reports plus a fleet summary.

Usage:
    uv run python -m acme.backtest.run                      # all 5 strategies
    uv run python -m acme.backtest.run --strategy ema_cross # one strategy
    uv run python -m acme.backtest.run --since 2025-04-01   # narrower window
"""

from __future__ import annotations

import argparse
import time

import structlog

from acme.backtest.bar_replay import run_backtest
from acme.backtest.data import iter_bars
from acme.backtest.report import print_table, save_fleet_summary, save_report
from acme.contracts import MES
from acme.risk import TOPSTEP_50K
from acme.strategies.anti import AntiStrategy
from acme.strategies.bb_mr import BollingerMeanReversionStrategy
from acme.strategies.donchian import DonchianBreakoutStrategy
from acme.strategies.ema_cross import EmaCrossStrategy
from acme.strategies.orb import OpeningRangeBreakoutStrategy
from acme.strategies.supertrend import SupertrendStrategy
from acme.strategies.turtle_soup import TurtleSoupStrategy
from acme.strategies.turtles_system2 import TurtlesSystem2Strategy

log = structlog.get_logger(__name__)


SEED_FLEET = {
    "ema_cross":       lambda: EmaCrossStrategy(contract=MES),
    "anti":            lambda: AntiStrategy(contract=MES),
    "orb":             lambda: OpeningRangeBreakoutStrategy(contract=MES),
    "donchian":        lambda: DonchianBreakoutStrategy(contract=MES),
    "bb_mr":           lambda: BollingerMeanReversionStrategy(contract=MES),
    "turtle_soup":     lambda: TurtleSoupStrategy(contract=MES),
    "supertrend":      lambda: SupertrendStrategy(contract=MES),
    "turtles_system2": lambda: TurtlesSystem2Strategy(contract=MES),
}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Acme Futures backtest runner")
    p.add_argument("--strategy", choices=sorted(SEED_FLEET.keys()), default=None,
                   help="Run a single strategy (default: all 5)")
    p.add_argument("--since", default=None,
                   help="Earliest bar to include, ISO date (e.g. 2025-04-01)")
    p.add_argument("--until", default=None,
                   help="Latest bar to include, ISO date (e.g. 2026-04-30)")
    p.add_argument("--starting-balance", type=float, default=50_000.0)
    return p.parse_args()


def _parse_iso(s: str | None):
    if s is None:
        return None
    from datetime import UTC, datetime
    if "T" in s:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    return datetime.fromisoformat(s + "T00:00:00+00:00").replace(tzinfo=UTC)


def main() -> None:
    args = _parse_args()
    start = _parse_iso(args.since)
    end = _parse_iso(args.until)
    starting_balance = args.starting_balance

    names = [args.strategy] if args.strategy else list(SEED_FLEET.keys())
    print(f"\nBacktesting {len(names)} strategies on MES ({args.since or 'beginning'} → {args.until or 'end'})")
    print(f"Starting balance: ${starting_balance:,.0f}")
    print()

    reports = []
    for name in names:
        builder = SEED_FLEET[name]
        strat = builder()
        t0 = time.time()
        # Re-iterate bars per strategy. For 5 strategies × ~700K bars this is
        # ~3.5M iterations; should be a few minutes total. Each strategy needs
        # its own indicator state so we can't share a single pass.
        bars_iter = iter_bars(start=start, end=end)
        report = run_backtest(
            strat, bars_iter,
            profile=TOPSTEP_50K,
            starting_balance=starting_balance,
        )
        elapsed = time.time() - t0
        log.info("backtest_done", strategy=name, elapsed_sec=round(elapsed, 1),
                 n_trades=report.metrics.n_trades, net_pnl=round(report.metrics.net_pnl, 2))
        save_report(report, starting_balance)
        reports.append(report)

    save_fleet_summary(reports, starting_balance)
    print_table(reports, starting_balance)


if __name__ == "__main__":
    main()
