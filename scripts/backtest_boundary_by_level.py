"""Break BOUNDARY's 2-year trades down by level type.

BOUNDARY's `reason` field is `boundary_fade_{level_name}` where level_name
is one of {pdh, pdl, onh, onl, orh, orl}. PDH/PDL = previous day, ONH/ONL
= overnight, ORH/ORL = opening range. They have different formation logic
and different market memory — pooling them obscures per-class edge.

This script reads docs/backtest_new_fleet/boundary.csv (which now carries
`reason`), groups by level type, and prints the same shape of table as
the time-of-day breakdown:

  level_type | n | WR | PF | net | avg

Usage:
    uv run python scripts/backtest_boundary_by_level.py
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

CSV_PATH = Path(__file__).resolve().parents[1] / "docs" / "backtest_new_fleet" / "boundary.csv"


def _stats(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "net": 0.0, "avg": 0.0}
    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] < 0]
    gross_win = sum(t["net_pnl"] for t in wins)
    gross_loss = -sum(t["net_pnl"] for t in losses)
    net = sum(t["net_pnl"] for t in trades)
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    return {
        "n": n,
        "wr": len(wins) / n,
        "pf": pf,
        "net": net,
        "avg": net / n,
    }


def main() -> int:
    if not CSV_PATH.exists():
        print(f"missing: {CSV_PATH}")
        print("run `uv run python scripts/backtest_new_fleet.py --strategy boundary` first")
        return 1

    by_level: dict[str, list[dict]] = defaultdict(list)
    all_trades: list[dict] = []
    no_reason = 0
    for row in csv.DictReader(CSV_PATH.open()):
        row["net_pnl"] = float(row["net_pnl"])
        reason = row.get("reason", "")
        if not reason.startswith("boundary_fade_"):
            no_reason += 1
            continue
        level = reason.replace("boundary_fade_", "")
        by_level[level].append(row)
        all_trades.append(row)

    if no_reason:
        print(f"(skipped {no_reason} rows without a recognisable reason)")

    # Print in groups: PDH/PDL, ONH/ONL, ORH/ORL, ALL
    order = ["pdh", "pdl", "onh", "onl", "orh", "orl"]

    print(f"\n{'level':>6} | {'n':>4} | {'WR':>5} | {'PF':>5} | {'net':>10} | {'avg':>7}")
    print("-" * 50)
    for lvl in order:
        s = _stats(by_level.get(lvl, []))
        if s["n"] == 0:
            print(f"{lvl.upper():>6} |    0 |     - |     - |          - |       -")
            continue
        pf_s = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "inf"
        print(f"{lvl.upper():>6} | {s['n']:>4} | "
              f"{s['wr']*100:>4.1f}% | {pf_s:>5} | "
              f"${s['net']:>+9.2f} | ${s['avg']:>+6.2f}")

    # Grouped by class
    classes = {
        "PD (prev-day)": ["pdh", "pdl"],
        "ON (overnight)": ["onh", "onl"],
        "OR (opening-rng)": ["orh", "orl"],
    }
    print(f"\n{'class':>16} | {'n':>4} | {'WR':>5} | {'PF':>5} | {'net':>10} | {'avg':>7}")
    print("-" * 60)
    for cname, lvls in classes.items():
        bucket: list[dict] = []
        for lvl in lvls:
            bucket.extend(by_level.get(lvl, []))
        s = _stats(bucket)
        if s["n"] == 0:
            continue
        pf_s = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "inf"
        print(f"{cname:>16} | {s['n']:>4} | "
              f"{s['wr']*100:>4.1f}% | {pf_s:>5} | "
              f"${s['net']:>+9.2f} | ${s['avg']:>+6.2f}")

    # Totals (sanity)
    s_all = _stats(all_trades)
    print("-" * 60)
    pf_s = f"{s_all['pf']:.2f}" if s_all["pf"] != float("inf") else "inf"
    print(f"{'TOTAL':>16} | {s_all['n']:>4} | "
          f"{s_all['wr']*100:>4.1f}% | {pf_s:>5} | "
          f"${s_all['net']:>+9.2f} | ${s_all['avg']:>+6.2f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
