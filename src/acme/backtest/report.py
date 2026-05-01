"""Persist + render BacktestReports.

Writes one JSON per strategy to ~/.acme/backtests/<strategy>_<timestamp>.json,
plus a single fleet-summary JSON for the whole batch. Also has a pretty
console printer for at-a-glance reading.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import structlog

from acme.backtest.bar_replay import BacktestReport, evaluate_stage_0

log = structlog.get_logger(__name__)

REPORT_DIR = Path.home() / ".acme" / "backtests"


def save_report(
    report: BacktestReport,
    starting_balance: float,
    *,
    out_dir: Path = REPORT_DIR,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{report.strategy}_{ts}.json"
    payload = {
        "strategy": report.strategy,
        "profile": report.profile,
        "starting_balance": starting_balance,
        "bars_processed": report.bars_processed,
        "summary": report.summary_dict(),
        "stage_0": evaluate_stage_0(report, starting_balance),
        "trades": [
            {**asdict(t), "entry_t": t.entry_t.isoformat(), "exit_t": t.exit_t.isoformat()}
            for t in report.trades
        ],
        "equity_curve": [(t.isoformat(), v) for t, v in report.equity_curve],
    }
    path.write_text(json.dumps(payload, indent=2, default=str))
    log.info("backtest_report_saved", strategy=report.strategy, path=str(path),
             n_trades=len(report.trades))
    return path


def save_fleet_summary(
    reports: list[BacktestReport],
    starting_balance: float,
    *,
    out_dir: Path = REPORT_DIR,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"_fleet_summary_{ts}.json"
    payload = {
        "generated_at": ts,
        "starting_balance": starting_balance,
        "strategies": [
            {**r.summary_dict(), "stage_0": evaluate_stage_0(r, starting_balance)}
            for r in reports
        ],
    }
    path.write_text(json.dumps(payload, indent=2, default=str))
    log.info("fleet_summary_saved", path=str(path), n_strategies=len(reports))
    return path


def print_table(reports: list[BacktestReport], starting_balance: float) -> None:
    """Pretty-print the fleet summary to stdout."""
    headers = ["Strategy", "n", "Net P&L", "Win%", "PF", "Sharpe", "MaxDD", "Stage 0"]
    rows = []
    for r in reports:
        m = r.metrics
        s0 = evaluate_stage_0(r, starting_balance)
        rows.append([
            r.strategy,
            str(m.n_trades),
            f"${m.net_pnl:,.0f}",
            f"{m.win_rate*100:.0f}%",
            f"{m.profit_factor:.2f}" if m.profit_factor is not None else "—",
            f"{m.sharpe:.2f}",
            f"${m.max_drawdown:,.0f}",
            s0["verdict"],
        ])
    widths = [max(len(h), max((len(r[i]) for r in rows), default=0)) for i, h in enumerate(headers)]
    fmt = "  " + "  ".join(f"{{:<{w}}}" for w in widths)
    print()
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for row in rows:
        print(fmt.format(*row))
    print()
    # Detail block per strategy: show why each gate passed/failed
    for r in reports:
        s0 = evaluate_stage_0(r, starting_balance)
        verdict = s0["verdict"]
        marker = "✓" if verdict == "PASS" else "✗"
        print(f"  {marker} {r.strategy:12s}  Stage 0: {verdict}")
        for gate, info in s0["gates"].items():
            mark = "✓" if info["pass"] else "✗"
            print(f"      {mark} {gate:14s} {info['actual']}")
        print()
