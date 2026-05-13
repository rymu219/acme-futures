"""OR (opening-range) trades broken down by entry hour CT.

OR pooled is PF 1.30 — barely above breakeven. The question before
shipping the PD-dropped variant is: is OR's edge concentrated in a
few hours (and dragging elsewhere), or is it uniformly mediocre?
Same shape as the time-of-day analysis we ran for the full strategy.

Reads docs/backtest_new_fleet/boundary.csv (must have `reason` column).
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CSV_PATH = Path(__file__).resolve().parents[1] / "docs" / "backtest_new_fleet" / "boundary.csv"
CT = ZoneInfo("America/Chicago")
OR_REASONS = {"boundary_fade_orh", "boundary_fade_orl"}


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
    return {"n": n, "wr": len(wins) / n, "pf": pf, "net": net, "avg": net / n}


def main() -> int:
    if not CSV_PATH.exists():
        print(f"missing: {CSV_PATH}")
        return 1

    by_hour_or: dict[int, list[dict]] = defaultdict(list)
    by_hour_orh: dict[int, list[dict]] = defaultdict(list)
    by_hour_orl: dict[int, list[dict]] = defaultdict(list)
    all_or: list[dict] = []
    for row in csv.DictReader(CSV_PATH.open()):
        reason = row.get("reason", "")
        if reason not in OR_REASONS:
            continue
        row["net_pnl"] = float(row["net_pnl"])
        ts = datetime.fromisoformat(row["entry_ts"]).astimezone(CT)
        hour = ts.hour
        by_hour_or[hour].append(row)
        if reason == "boundary_fade_orh":
            by_hour_orh[hour].append(row)
        else:
            by_hour_orl[hour].append(row)
        all_or.append(row)

    print(f"OR trades total: n={len(all_or)}")
    s_all = _stats(all_or)
    pf_s = f"{s_all['pf']:.2f}" if s_all['pf'] != float('inf') else 'inf'
    print(f"pooled: PF={pf_s}  net=${s_all['net']:+,.2f}  WR={s_all['wr']*100:.1f}%\n")

    print(f"{'hr':>3} | {'n':>4} | {'WR':>5} | {'PF':>5} | {'net':>10} | {'avg':>7}")
    print("-" * 50)
    for h in sorted(by_hour_or.keys()):
        s = _stats(by_hour_or[h])
        pf_s = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "inf"
        print(f"{h:>3} | {s['n']:>4} | "
              f"{s['wr']*100:>4.1f}% | {pf_s:>5} | "
              f"${s['net']:>+9.2f} | ${s['avg']:>+6.2f}")

    # Split ORH vs ORL by hour — direction asymmetry may matter
    print("\n-- ORH (short fades) by hour --")
    print(f"{'hr':>3} | {'n':>4} | {'WR':>5} | {'PF':>5} | {'net':>10}")
    print("-" * 42)
    for h in sorted(by_hour_orh.keys()):
        s = _stats(by_hour_orh[h])
        if s["n"] == 0:
            continue
        pf_s = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "inf"
        print(f"{h:>3} | {s['n']:>4} | {s['wr']*100:>4.1f}% | {pf_s:>5} | ${s['net']:>+9.2f}")

    print("\n-- ORL (long fades) by hour --")
    print(f"{'hr':>3} | {'n':>4} | {'WR':>5} | {'PF':>5} | {'net':>10}")
    print("-" * 42)
    for h in sorted(by_hour_orl.keys()):
        s = _stats(by_hour_orl[h])
        if s["n"] == 0:
            continue
        pf_s = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "inf"
        print(f"{h:>3} | {s['n']:>4} | {s['wr']*100:>4.1f}% | {pf_s:>5} | ${s['net']:>+9.2f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
