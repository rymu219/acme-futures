"""CONFLUENCE breakdown.

Pooled / by-direction / by-confluence-type / by-level-count /
by-CT-entry-hour / by-exit-type / bar-1 cohort / monthly equity with
Topstep DLL+MLL flags. Reads a per-trade CSV produced by
`scripts/backtest_confluence.py`.

Default input is `docs/backtest_confluence/primary.csv` (best-PF combo
from the sweep with n >= 30). Override with `--csv` to break down a
different combo.

Reason-field encoding (set in ConfluenceStrategy._build_signal):

  confluence_{long|short}_n{N}_{LVL1+LVL2+...}

…where N is the number of levels in the zone and LVL* are sorted level
names joined with '+'. The breakdown parses both fields from the
reason string.

Output format mirrors backtest_gap_fill_breakdown.py and
backtest_vwap_momentum_breakdown.py so the analysis reads side-by-side
with the existing fleet's breakdowns.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CT = ZoneInfo("America/Chicago")

DEFAULT_CSV = (Path(__file__).resolve().parents[1]
               / "docs" / "backtest_confluence" / "primary.csv")

# Topstep 50K Combine
TOPSTEP_DAILY = 1_000.0
TOPSTEP_MLL = 2_000.0


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
          f"${s['net']:>+10.2f} | ${s['avg']:>+8.2f}")


def _parse_reason(reason: str) -> tuple[int, str]:
    """Extract (n_levels, level_combo) from a confluence reason string.

    Format: confluence_{long|short}_n{N}_{LVL1+LVL2+...}

    Returns (0, "") if parsing fails — defensive against malformed rows.
    """
    parts = reason.split("_")
    n_levels = 0
    levels = ""
    for tok in parts:
        if tok.startswith("n") and tok[1:].isdigit():
            n_levels = int(tok[1:])
        elif "+" in tok or tok in {"VWAP", "SETT", "PDH", "PDL", "ONH", "ONL"}:
            levels = tok
    return n_levels, levels


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--csv", default=str(DEFAULT_CSV),
                   help="Per-trade CSV path (default: primary.csv from the sweep).")
    args = p.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"missing: {csv_path}")
        print("Run `uv run python scripts/backtest_confluence.py` first.")
        return 1

    enriched: list[dict] = []
    by_direction: dict[str, list[dict]] = defaultdict(list)
    by_combo: dict[str, list[dict]] = defaultdict(list)
    by_level_count: dict[str, list[dict]] = defaultdict(list)
    by_hour: dict[int, list[dict]] = defaultdict(list)
    by_outcome: dict[str, list[dict]] = defaultdict(list)
    by_month: dict[str, list[dict]] = defaultdict(list)
    bar1: list[dict] = []
    for row in csv.DictReader(csv_path.open()):
        row["net_pnl"] = float(row["net_pnl"])
        row["bars_held_minutes"] = int(row.get("bars_held_minutes") or 0)
        entry_dt = datetime.fromisoformat(row["entry_ts"]).astimezone(CT)
        row["entry_dt_ct"] = entry_dt
        direction = "long" if row.get("side") == "buy" else "short"
        row["direction"] = direction
        n_levels, combo = _parse_reason(row.get("reason", ""))
        row["n_levels"] = n_levels
        row["combo"] = combo
        # Bucket by level count: 2 vs 3+.
        lc_bucket = "2 levels" if n_levels == 2 else (
            "3+ levels" if n_levels >= 3 else "unknown")

        enriched.append(row)
        by_direction[direction].append(row)
        if combo:
            by_combo[combo].append(row)
        by_level_count[lc_bucket].append(row)
        by_hour[entry_dt.hour].append(row)
        by_outcome[row.get("outcome", "")].append(row)
        by_month[entry_dt.strftime("%Y-%m")].append(row)
        if row["bars_held_minutes"] <= 2:
            bar1.append(row)

    if not enriched:
        print(f"No rows in {csv_path}.")
        return 1

    print(f"Source: {csv_path}\n")

    # ───── pooled ─────
    print(f"{'':>22} | {'n':>4} | {'WR':>5} | {'PF':>5} | {'net':>11} | {'avg':>9}")
    print("-" * 72)
    _print_row("POOLED", _stats(enriched))

    # ───── by direction ─────
    print("\nBy direction:")
    print(f"{'':>22} | {'n':>4} | {'WR':>5} | {'PF':>5} | {'net':>11} | {'avg':>9}")
    print("-" * 72)
    _print_row("LONG",  _stats(by_direction.get("long", [])))
    _print_row("SHORT", _stats(by_direction.get("short", [])))

    # ───── by confluence type (level pair / triple) ─────
    # Sort by trade count desc so the dominant combinations show first.
    print("\nBy confluence type (which levels stacked):")
    print(f"{'':>22} | {'n':>4} | {'WR':>5} | {'PF':>5} | {'net':>11} | {'avg':>9}")
    print("-" * 72)
    for combo in sorted(by_combo, key=lambda k: -len(by_combo[k])):
        _print_row(combo, _stats(by_combo[combo]))

    # ───── by level count (2 vs 3+) ─────
    # The thesis says "more confluence = stronger conviction" — this
    # is where to verify or refute that.
    print("\nBy number of levels in zone:")
    print(f"{'':>22} | {'n':>4} | {'WR':>5} | {'PF':>5} | {'net':>11} | {'avg':>9}")
    print("-" * 72)
    for bucket in ("2 levels", "3+ levels", "unknown"):
        if by_level_count.get(bucket):
            _print_row(bucket, _stats(by_level_count[bucket]))

    # ───── by CT entry hour (09:00-14:xx) ─────
    print("\nBy CT entry hour:")
    print(f"{'':>22} | {'n':>4} | {'WR':>5} | {'PF':>5} | {'net':>11} | {'avg':>9}")
    print("-" * 72)
    for h in range(9, 15):
        label = f"{h:02d}:xx CT"
        _print_row(label, _stats(by_hour.get(h, [])))

    # ───── by exit type ─────
    print("\nBy exit type:")
    print(f"{'':>22} | {'n':>4} | {'WR':>5} | {'PF':>5} | {'net':>11} | {'avg':>9}")
    print("-" * 72)
    for o in ("trailing_stop", "initial_stop", "force_close_session",
              "force_close_end_of_backtest"):
        _print_row(o, _stats(by_outcome.get(o, [])))

    # ───── bar-1 cohort ─────
    print("\nBar-1 cohort (bars_held_minutes <= 2):")
    print(f"{'':>22} | {'n':>4} | {'WR':>5} | {'PF':>5} | {'net':>11} | {'avg':>9}")
    print("-" * 72)
    _print_row("bar-1 only", _stats(bar1))
    rest = [t for t in enriched if t["bars_held_minutes"] > 2]
    _print_row("rest (>1 bar)", _stats(rest))

    # ───── monthly equity + Topstep flags ─────
    print("\nMonthly P&L (Topstep DLL=$1K, trailing MLL=$2K):")
    print(f"{'month':>9} | {'n':>3} | {'net':>10} | {'equity':>10} | "
          f"{'worst day':>10} | flags")
    print("-" * 76)
    running = 0.0
    peak = 0.0
    overall_max_dd = 0.0
    dll_breach = 0
    mll_breach = 0
    for month_key in sorted(by_month):
        month_trades = by_month[month_key]
        daily: dict = defaultdict(float)
        for t in month_trades:
            d = datetime.fromisoformat(t["exit_ts"]).astimezone(CT).date()
            daily[d] += t["net_pnl"]
        net_month = sum(t["net_pnl"] for t in month_trades)
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
              f"${net_month:>+9.2f} | ${running:>+9.2f} | "
              f"${worst_day:>+9.2f} | {flag_str}")

    print(f"\nOverall peak-to-trough drawdown: ${overall_max_dd:.2f}  "
          f"(MLL ceiling: ${TOPSTEP_MLL:.0f})")
    print(f"Months with DLL breach: {dll_breach} / {len(by_month)}")
    print(f"Months with MLL breach: {mll_breach} / {len(by_month)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
