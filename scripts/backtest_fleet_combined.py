"""Combined BOUNDARY + OVERNIGHT_DRIFT + GAP_FILL equity curve.

Applies fleet-coordination rules:
  - While OVERNIGHT_DRIFT holds (entry → exit), BOUNDARY suppresses
    new entries. OVERNIGHT_DRIFT runs 17:00-08:00 CT.
  - While GAP_FILL holds (entry → exit), BOUNDARY suppresses new
    entries. GAP_FILL runs 08:30-13:00 CT.
  - OVERNIGHT_DRIFT and GAP_FILL windows don't overlap, so they
    never conflict with each other.

Sizing:
  - BOUNDARY: as backtested (1-4 contracts dynamic via $25 budget)
  - OVERNIGHT_DRIFT: as backtested (5 fixed contracts via $510 budget)
  - GAP_FILL: 5 fixed contracts. The strategy currently runs at
    1 contract (variable size off $100 budget); we simulate 5x by
    multiplying each trade's P&L by 5. Deterministic — at MES scale
    contract count doesn't change execution quality, so this is
    equivalent to having sized at 5 from the start.

Topstep MLL+DLL flagged on the combined equity curve.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
BOUNDARY_CSV = Path("docs/backtest_new_fleet/boundary.csv")
OD_CSV = Path("docs/backtest_new_fleet/overnight_drift.csv")
GAP_FILL_CSV = Path("docs/backtest_new_fleet/gap_fill.csv")

GAP_FILL_CONTRACT_SCALAR = 5  # simulate 5-contract sizing

TOPSTEP_DAILY = 1_000.0
TOPSTEP_MLL = 2_000.0


def _load(path: Path, scale_pnl: float = 1.0) -> list[dict]:
    out = []
    for row in csv.DictReader(path.open()):
        row["net_pnl"] = float(row["net_pnl"]) * scale_pnl
        row["entry_dt"] = datetime.fromisoformat(row["entry_ts"])
        row["exit_dt"] = datetime.fromisoformat(row["exit_ts"])
        out.append(row)
    return out


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


def _row(label: str, s: dict, width: int = 32) -> str:
    if s["n"] == 0:
        return f"{label:>{width}} | n=0"
    pf_s = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "inf"
    return (f"{label:>{width}} | {s['n']:>4} | "
            f"{s['wr']*100:>4.1f}% | {pf_s:>5} | "
            f"${s['net']:>+11.2f} | ${s['avg']:>+8.2f}")


def main() -> int:
    boundary = _load(BOUNDARY_CSV)
    od = _load(OD_CSV)
    gap_fill = _load(GAP_FILL_CSV, scale_pnl=GAP_FILL_CONTRACT_SCALAR)

    # Build active-position windows for each overnight/intraday holder
    od_windows = [(t["entry_dt"], t["exit_dt"]) for t in od]
    gf_windows = [(t["entry_dt"], t["exit_dt"]) for t in gap_fill]

    boundary_kept: list[dict] = []
    suppressed_by_od: list[dict] = []
    suppressed_by_gf: list[dict] = []
    for t in boundary:
        entry = t["entry_dt"]
        in_od = any(s <= entry < e for s, e in od_windows)
        in_gf = any(s <= entry < e for s, e in gf_windows)
        if in_od:
            suppressed_by_od.append(t)
        elif in_gf:
            suppressed_by_gf.append(t)
        else:
            boundary_kept.append(t)

    combined = boundary_kept + od + gap_fill
    combined.sort(key=lambda r: r["exit_dt"])

    # ───── per-strategy + combined ─────
    print(f"{'':>32} | {'n':>4} | {'WR':>5} | {'PF':>5} | {'net':>12} | {'avg':>9}")
    print("-" * 88)
    print(_row("BOUNDARY (raw)", _stats(boundary)))
    print(_row("OVERNIGHT_DRIFT", _stats(od)))
    print(_row("GAP_FILL @ 5 contracts", _stats(gap_fill)))
    print("-" * 88)
    print(_row("BOUNDARY (after suppression)", _stats(boundary_kept)))
    print(_row("  suppressed by OD", _stats(suppressed_by_od)))
    print(_row("  suppressed by GAP_FILL", _stats(suppressed_by_gf)))
    print("-" * 88)
    print(_row("COMBINED FLEET", _stats(combined)))

    # ───── monthly equity + Topstep ─────
    print("\nMonthly P&L (combined fleet — equity = cumulative):")
    print(f"{'month':>9} | {'n':>3} | {'BD$':>8} | {'OD$':>8} | "
          f"{'GF$':>8} | {'net':>10} | {'equity':>10} | "
          f"{'worst day':>10} | flags")
    print("-" * 108)

    by_month = defaultdict(list)
    for t in combined:
        exit_ct = t["exit_dt"].astimezone(CT)
        by_month[exit_ct.strftime("%Y-%m")].append(t)

    running = 0.0
    peak = 0.0
    overall_max_dd = 0.0
    mll_breach = 0
    dll_breach = 0
    for month_key in sorted(by_month):
        month_trades = by_month[month_key]
        bd_pnl = sum(t["net_pnl"] for t in month_trades if t["strategy"] == "boundary")
        od_pnl = sum(t["net_pnl"] for t in month_trades if t["strategy"] == "overnight_drift")
        gf_pnl = sum(t["net_pnl"] for t in month_trades if t["strategy"] == "gap_fill")
        net_month = bd_pnl + od_pnl + gf_pnl

        daily = defaultdict(float)
        for t in month_trades:
            d = t["exit_dt"].astimezone(CT).date()
            daily[d] += t["net_pnl"]
        worst_day = min(daily.values()) if daily else 0.0

        flags = []
        if worst_day < -TOPSTEP_DAILY:
            flags.append(f"DLL@${worst_day:+.0f}")
            dll_breach += 1
        had_mll = False
        for d in sorted(daily):
            running += daily[d]
            peak = max(peak, running)
            dd = peak - running
            overall_max_dd = max(overall_max_dd, dd)
            if dd > TOPSTEP_MLL and not had_mll:
                flags.append(f"MLL@dd=${dd:+.0f}")
                had_mll = True
                mll_breach += 1

        flag_str = "  ".join(flags) if flags else ""
        print(f"{month_key:>9} | {len(month_trades):>3} | "
              f"${bd_pnl:>+7.2f} | ${od_pnl:>+7.2f} | "
              f"${gf_pnl:>+7.2f} | ${net_month:>+9.2f} | "
              f"${running:>+9.2f} | ${worst_day:>+9.2f} | {flag_str}")

    print(f"\nOverall peak-to-trough drawdown: ${overall_max_dd:.2f}  "
          f"(Topstep trailing MLL ceiling: ${TOPSTEP_MLL:.0f})")
    print(f"Months with DLL breach: {dll_breach} / {len(by_month)}")
    print(f"Months with MLL breach: {mll_breach} / {len(by_month)}")

    # ───── hour-of-day distribution ─────
    print("\nBy CT entry hour (combined book):")
    by_hour = defaultdict(list)
    for t in combined:
        h = t["entry_dt"].astimezone(CT).hour
        by_hour[h].append(t)
    print(f"{'hr':>3} | {'sources':>20} | {'n':>4} | {'PF':>5} | "
          f"{'net':>11} | {'avg':>9}")
    print("-" * 70)
    for h in sorted(by_hour):
        s_all = _stats(by_hour[h])
        srcs = sorted({t["strategy"] for t in by_hour[h]})
        src_label = "+".join(s.replace("_", "")[:4].upper() for s in srcs)
        pf_s = f"{s_all['pf']:.2f}" if s_all["pf"] != float("inf") else "inf"
        print(f"{h:>3} | {src_label:>20} | {s_all['n']:>4} | {pf_s:>5} | "
              f"${s_all['net']:>+10.2f} | ${s_all['avg']:>+8.2f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
