"""OVERNIGHT_DRIFT breakdown.

Pooled / by-exit-type / by-bias-body-bucket / force-close P&L
histogram / monthly equity with Topstep MLL+DLL breach scan.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CSV_PATH = Path(__file__).resolve().parents[1] / "docs" / "backtest_new_fleet" / "overnight_drift.csv"
CT = ZoneInfo("America/Chicago")

TOPSTEP_DAILY_LIMIT = 1_000.0
TOPSTEP_TRAILING_MLL = 2_000.0


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


def _print_row(label: str, s: dict, width: int = 24) -> None:
    if s["n"] == 0:
        print(f"{label:>{width}} | n=0")
        return
    pf_s = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "inf"
    print(f"{label:>{width}} | {s['n']:>4} | "
          f"{s['wr']*100:>4.1f}% | {pf_s:>5} | "
          f"${s['net']:>+10.2f} | ${s['avg']:>+8.2f}")


def main() -> int:
    if not CSV_PATH.exists():
        print(f"missing: {CSV_PATH}")
        return 1

    by_outcome: dict[str, list[dict]] = defaultdict(list)
    by_body_bucket: dict[str, list[dict]] = defaultdict(list)
    by_month: dict[str, list[dict]] = defaultdict(list)
    enriched: list[dict] = []

    for row in csv.DictReader(CSV_PATH.open()):
        reason = row.get("reason", "")
        if not reason.startswith("overnight_drift_long_b"):
            continue
        row["net_pnl"] = float(row["net_pnl"])
        # Parse body from reason tail: overnight_drift_long_b{NNNN}
        body_int = int(reason.rsplit("_b", 1)[-1])
        body = body_int / 100.0
        row["body"] = body
        entry_dt = datetime.fromisoformat(row["entry_ts"]).astimezone(CT)
        row["entry_dt_ct"] = entry_dt
        month_key = entry_dt.strftime("%Y-%m")

        if body < 1.0:
            bucket = "0-1pt"
        elif body < 2.0:
            bucket = "1-2pt"
        else:
            bucket = "2-3pt"
        by_body_bucket[bucket].append(row)
        by_outcome[row["outcome"]].append(row)
        by_month[month_key].append(row)
        enriched.append(row)

    # ───── pooled ─────
    print(f"{'':>24} | {'n':>4} | {'WR':>5} | {'PF':>5} | {'net':>11} | {'avg':>9}")
    print("-" * 74)
    _print_row("POOLED", _stats(enriched))

    # ───── by exit type ─────
    print("\nBy exit type:")
    print(f"{'':>24} | {'n':>4} | {'WR':>5} | {'PF':>5} | {'net':>11} | {'avg':>9}")
    print("-" * 74)
    for o in ("stop", "force_close_session", "target"):
        _print_row(o, _stats(by_outcome.get(o, [])))

    # ───── by body bucket ─────
    print("\nBy bias-bar body size (long, weak only):")
    print(f"{'':>24} | {'n':>4} | {'WR':>5} | {'PF':>5} | {'net':>11} | {'avg':>9}")
    print("-" * 74)
    for bucket in ("0-1pt", "1-2pt", "2-3pt"):
        _print_row(bucket, _stats(by_body_bucket.get(bucket, [])))

    # ───── force-close P&L histogram ($100 buckets) ─────
    force_close = by_outcome.get("force_close_session", [])
    if force_close:
        print(f"\nForce-close P&L histogram ({len(force_close)} trades; "
              f"bucket = $100; 5-contract sizing):")
        pnls = [t["net_pnl"] for t in force_close]
        lo = min(pnls)
        hi = max(pnls)
        # Bucket by $100, anchored at zero
        import math
        lo_bucket = int(math.floor(lo / 100))
        hi_bucket = int(math.floor(hi / 100))
        counts: dict[int, int] = defaultdict(int)
        bucket_pnl: dict[int, float] = defaultdict(float)
        for p in pnls:
            b = int(math.floor(p / 100))
            counts[b] += 1
            bucket_pnl[b] += p
        max_count = max(counts.values()) if counts else 1
        for b in range(lo_bucket, hi_bucket + 1):
            n = counts.get(b, 0)
            net = bucket_pnl.get(b, 0.0)
            bar_width = int(40 * n / max_count) if max_count else 0
            label = f"${b*100:>+5d} to ${(b+1)*100:>+5d}"
            bar = "█" * bar_width
            print(f"  {label} | n={n:>3} | net=${net:>+9.2f} | {bar}")

    # ───── monthly equity + Topstep breach scan ─────
    print("\nMonthly P&L (5-contract sizing; Topstep DLL=$1K, trailing MLL=$2K):")
    print(f"{'month':>9} | {'n':>3} | {'net':>10} | {'worst day':>10} | "
          f"{'eq end':>10} | flags")
    print("-" * 80)
    running_equity = 0.0
    peak_equity = 0.0
    overall_max_dd = 0.0
    months_with_breach = 0
    for month_key in sorted(by_month):
        month_trades = by_month[month_key]
        daily_pnl: dict = defaultdict(float)
        for t in month_trades:
            exit_ct = datetime.fromisoformat(t["exit_ts"]).astimezone(CT)
            daily_pnl[exit_ct.date()] += t["net_pnl"]

        net_month = sum(t["net_pnl"] for t in month_trades)
        worst_day_pnl = min(daily_pnl.values()) if daily_pnl else 0.0

        flags = []
        if worst_day_pnl < -TOPSTEP_DAILY_LIMIT:
            flags.append(f"DLL@${worst_day_pnl:+.0f}")
        for d in sorted(daily_pnl):
            running_equity += daily_pnl[d]
            peak_equity = max(peak_equity, running_equity)
            dd = peak_equity - running_equity
            overall_max_dd = max(overall_max_dd, dd)
            if dd > TOPSTEP_TRAILING_MLL and not any(
                    f.startswith("MLL") for f in flags):
                flags.append(f"MLL@dd=${dd:+.0f}")
        if flags:
            months_with_breach += 1
        flag_str = "  ".join(flags) if flags else ""
        print(f"{month_key:>9} | {len(month_trades):>3} | "
              f"${net_month:>+9.2f} | ${worst_day_pnl:>+9.2f} | "
              f"${running_equity:>+9.2f} | {flag_str}")

    print(f"\nOverall peak-to-trough drawdown:  ${overall_max_dd:.2f}  "
          f"(Topstep trailing MLL ceiling: ${TOPSTEP_TRAILING_MLL:.0f})")
    print(f"Months flagged for breach:        {months_with_breach} / {len(by_month)}")

    # ───── tail concentration ─────
    pnls = sorted((t["net_pnl"] for t in enriched), reverse=True)
    n = len(pnls)
    if n >= 10:
        top10pct_n = max(1, n // 10)
        bot10pct_n = max(1, n // 10)
        top10_sum = sum(pnls[:top10pct_n])
        bot10_sum = sum(pnls[-bot10pct_n:])
        net_total = sum(pnls)
        print("\nP&L tail concentration:")
        print(f"  top 10% ({top10pct_n}) sum:    ${top10_sum:>+10.2f}")
        print(f"  bottom 10% ({bot10pct_n}) sum: ${bot10_sum:>+10.2f}")
        print(f"  net (all):                ${net_total:>+10.2f}")
        if abs(net_total) > 0.01:
            print(f"  top 10% / |net|: {top10_sum / abs(net_total) * 100:.1f}%   "
                  f"bot 10% / |net|: {bot10_sum / abs(net_total) * 100:.1f}%")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
