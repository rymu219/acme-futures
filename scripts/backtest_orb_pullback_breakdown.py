"""ORB_PULLBACK — by-direction and by-hour breakdown.

Reads docs/backtest_new_fleet/orb_pullback.csv (which carries `reason`
encoding the entry direction). Reports the same shape of tables we use
for BOUNDARY: pooled stats, per-direction, per-CT-hour.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CSV_PATH = Path(__file__).resolve().parents[1] / "docs" / "backtest_new_fleet" / "orb_pullback.csv"
CT = ZoneInfo("America/Chicago")


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


def _print_row(label: str, s: dict, width: int = 16) -> None:
    pf_s = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "inf"
    print(f"{label:>{width}} | {s['n']:>4} | "
          f"{s['wr']*100:>4.1f}% | {pf_s:>5} | "
          f"${s['net']:>+9.2f} | ${s['avg']:>+6.2f}")


def main() -> int:
    if not CSV_PATH.exists():
        print(f"missing: {CSV_PATH}")
        return 1

    by_direction: dict[str, list[dict]] = defaultdict(list)
    by_hour: dict[int, list[dict]] = defaultdict(list)
    by_hour_long: dict[int, list[dict]] = defaultdict(list)
    by_hour_short: dict[int, list[dict]] = defaultdict(list)
    all_trades: list[dict] = []
    for row in csv.DictReader(CSV_PATH.open()):
        reason = row.get("reason", "")
        if not reason.startswith("orb_pullback_"):
            continue
        row["net_pnl"] = float(row["net_pnl"])
        direction = reason.replace("orb_pullback_", "")  # "long" or "short"
        ts = datetime.fromisoformat(row["entry_ts"]).astimezone(CT)
        h = ts.hour
        by_direction[direction].append(row)
        by_hour[h].append(row)
        if direction == "long":
            by_hour_long[h].append(row)
        else:
            by_hour_short[h].append(row)
        all_trades.append(row)

    print(f"\n{'':>16} | {'n':>4} | {'WR':>5} | {'PF':>5} | {'net':>10} | {'avg':>7}")
    print("-" * 60)
    _print_row("POOLED", _stats(all_trades))
    print("-" * 60)
    for d in ("long", "short"):
        _print_row(d.upper(), _stats(by_direction.get(d, [])))

    print("\nBy CT hour (pooled long+short):")
    print(f"{'hr':>3} | {'n':>4} | {'WR':>5} | {'PF':>5} | {'net':>10} | {'avg':>7}")
    print("-" * 50)
    for h in sorted(by_hour):
        s = _stats(by_hour[h])
        pf_s = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "inf"
        print(f"{h:>3} | {s['n']:>4} | "
              f"{s['wr']*100:>4.1f}% | {pf_s:>5} | "
              f"${s['net']:>+9.2f} | ${s['avg']:>+6.2f}")

    print("\nBy CT hour x direction:")
    print(f"{'hr':>3} | {'long n/PF/net':>30} | {'short n/PF/net':>30}")
    print("-" * 70)
    hours = sorted(set(by_hour_long) | set(by_hour_short))
    for h in hours:
        sl = _stats(by_hour_long.get(h, []))
        ss = _stats(by_hour_short.get(h, []))
        sl_pf = f"{sl['pf']:.2f}" if sl["pf"] != float("inf") else "inf"
        ss_pf = f"{ss['pf']:.2f}" if ss["pf"] != float("inf") else "inf"
        l_part = f"{sl['n']:>3} / {sl_pf:>4} / ${sl['net']:>+8.2f}" if sl['n'] else "       —"
        s_part = f"{ss['n']:>3} / {ss_pf:>4} / ${ss['net']:>+8.2f}" if ss['n'] else "       —"
        print(f"{h:>3} | {l_part:>30} | {s_part:>30}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
