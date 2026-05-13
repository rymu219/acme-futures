"""OVERNIGHT_MOMENTUM breakdown.

Pooled / by-direction / by-bias-strength / by-exit-type, plus
Topstep MLL/DLL breach analysis assuming 5-contract sizing (which is
the default this strategy ships with).
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CSV_PATH = Path(__file__).resolve().parents[1] / "docs" / "backtest_new_fleet" / "overnight_momentum.csv"
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


def _print_row(label: str, s: dict, width: int = 22) -> None:
    if s["n"] == 0:
        print(f"{label:>{width}} | n=0")
        return
    pf_s = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "inf"
    print(f"{label:>{width}} | {s['n']:>4} | "
          f"{s['wr']*100:>4.1f}% | {pf_s:>5} | "
          f"${s['net']:>+10.2f} | ${s['avg']:>+7.2f}")


def main() -> int:
    if not CSV_PATH.exists():
        print(f"missing: {CSV_PATH}")
        return 1

    by_direction: dict[str, list[dict]] = defaultdict(list)
    by_strength: dict[str, list[dict]] = defaultdict(list)
    by_outcome: dict[str, list[dict]] = defaultdict(list)
    by_month: dict[str, list[dict]] = defaultdict(list)
    enriched: list[dict] = []
    for row in csv.DictReader(CSV_PATH.open()):
        reason = row.get("reason", "")
        if not reason.startswith("overnight_"):
            continue
        row["net_pnl"] = float(row["net_pnl"])
        # reason: overnight_{long|short}_{strong|weak}
        parts = reason.split("_")
        if len(parts) < 3:
            continue
        direction = parts[1]
        strength = parts[2]
        entry_dt = datetime.fromisoformat(row["entry_ts"]).astimezone(CT)
        row["entry_dt_ct"] = entry_dt
        row["direction"] = direction
        row["strength"] = strength
        month_key = entry_dt.strftime("%Y-%m")
        by_direction[direction].append(row)
        by_strength[strength].append(row)
        by_outcome[row["outcome"]].append(row)
        by_month[month_key].append(row)
        enriched.append(row)

    # ───── pooled / direction / strength ─────
    print(f"{'':>22} | {'n':>4} | {'WR':>5} | {'PF':>5} | {'net':>11} | {'avg':>8}")
    print("-" * 72)
    _print_row("POOLED", _stats(enriched))
    print("-" * 72)
    for d in ("long", "short"):
        _print_row(d.upper(), _stats(by_direction.get(d, [])))
    print("-" * 72)
    for s_ in ("strong", "weak"):
        _print_row(f"{s_.upper()} bias (|body|{'>' if s_=='strong' else '<='}3pt)",
                   _stats(by_strength.get(s_, [])))

    # ───── by direction × strength ─────
    print("\nBy direction × strength:")
    print(f"{'':>22} | {'n':>4} | {'WR':>5} | {'PF':>5} | {'net':>11} | {'avg':>8}")
    print("-" * 72)
    for d in ("long", "short"):
        for s_ in ("strong", "weak"):
            bucket = [r for r in enriched
                      if r["direction"] == d and r["strength"] == s_]
            _print_row(f"{d.upper()} {s_}", _stats(bucket))

    # ───── by exit outcome ─────
    print("\nBy exit type:")
    print(f"{'':>22} | {'n':>4} | {'WR':>5} | {'PF':>5} | {'net':>11} | {'avg':>8}")
    print("-" * 72)
    for o in ("target", "stop", "force_close_session"):
        _print_row(o, _stats(by_outcome.get(o, [])))

    # ───── monthly equity curve + Topstep breach scan ─────
    print("\nMonthly P&L (5-contract sizing; flags Topstep breaches):")
    print(f"{'month':>9} | {'n':>3} | {'net':>10} | {'worst day':>10} | {'min equity':>10} | flags")
    print("-" * 76)
    running_equity = 0.0
    peak_equity = 0.0
    for month_key in sorted(by_month):
        month_trades = by_month[month_key]
        # Per-day P&L (CT trading day; group by CT date of entry_ts to
        # get the *evening* day — losses on Mon evening hit Mon's DLL)
        daily_pnl: dict = defaultdict(float)
        for t in month_trades:
            # The DLL is tied to the calendar day in which the P&L is
            # realised. For a trade entered Mon 17:00 and closed Tue
            # 04:00, the P&L books on Tue. Use exit_ts for the day key.
            exit_ct = datetime.fromisoformat(t["exit_ts"]).astimezone(CT)
            daily_pnl[exit_ct.date()] += t["net_pnl"]

        net_month = sum(t["net_pnl"] for t in month_trades)
        worst_day_pnl = min(daily_pnl.values()) if daily_pnl else 0.0

        # Track equity intra-month bar-by-bar for trailing-MLL breach.
        # Simple model: peak rises to peak(running_equity); MLL breach
        # if running_equity drops more than $2K below peak. Loose proxy
        # (true MLL pegs at peak-EOD, but this catches the right shape).
        flags = []
        if worst_day_pnl < -TOPSTEP_DAILY_LIMIT:
            flags.append(f"DLL@${worst_day_pnl:+.0f}")
        # Sequential equity scan for trailing-MLL breach within month
        month_min_equity = running_equity
        for d in sorted(daily_pnl):
            running_equity += daily_pnl[d]
            peak_equity = max(peak_equity, running_equity)
            if running_equity < month_min_equity:
                month_min_equity = running_equity
            if peak_equity - running_equity > TOPSTEP_TRAILING_MLL and not any(
                    f.startswith("MLL") for f in flags):
                flags.append(
                    f"MLL@dd=${peak_equity - running_equity:+.0f}"
                )
        flag_str = "  ".join(flags) if flags else ""
        print(f"{month_key:>9} | {len(month_trades):>3} | "
              f"${net_month:>+9.2f} | ${worst_day_pnl:>+9.2f} | "
              f"${month_min_equity:>+9.2f} | {flag_str}")

    # ───── distribution: head/tail concentration ─────
    pnls = sorted((t["net_pnl"] for t in enriched), reverse=True)
    n = len(pnls)
    if n >= 10:
        top10pct_n = max(1, n // 10)
        bot10pct_n = max(1, n // 10)
        top10_sum = sum(pnls[:top10pct_n])
        bot10_sum = sum(pnls[-bot10pct_n:])
        net_total = sum(pnls)
        print("\nP&L tail concentration:")
        print(f"  top 10% ({top10pct_n} trades) sum:    ${top10_sum:>+10.2f}")
        print(f"  bottom 10% ({bot10pct_n} trades) sum: ${bot10_sum:>+10.2f}")
        print(f"  net (all):                            ${net_total:>+10.2f}")
        if abs(net_total) > 0.01:
            print(f"  top 10% / |net|: {top10_sum / abs(net_total) * 100:.1f}%   "
                  f"bot 10% / |net|: {bot10_sum / abs(net_total) * 100:.1f}%")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
