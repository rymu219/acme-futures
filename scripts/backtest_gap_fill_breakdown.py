"""GAP_FILL breakdown.

Pooled / by-direction / by-gap-size-bucket / by-exit-type / monthly
equity (Topstep DLL+MLL flag). Plus the gap-fill rate: of all
qualifying sessions, what fraction does price actually touch the prior
close at any point during 08:30-13:00 CT (independent of whether the
strategy held a winning trade).
"""
from __future__ import annotations

import csv
import logging
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

logging.basicConfig(level=logging.WARNING)
import structlog  # noqa: E402

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.backtest_new_fleet import stream_2min_bars  # noqa: E402

CSV_PATH = Path(__file__).resolve().parents[1] / "docs" / "backtest_new_fleet" / "gap_fill.csv"
CT = ZoneInfo("America/Chicago")

TOPSTEP_DAILY = 1_000.0
TOPSTEP_MLL = 2_000.0

MIN_GAP = 4.0  # match the strategy default for the gap-fill-rate computation


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


def compute_gap_fill_rate(bars: list[dict]) -> dict:
    """For each CT calendar date with a qualifying gap (|gap| >= MIN_GAP),
    record (a) whether price touched the prior-close between 08:30-13:00 CT
    and (b) whether it touched in the fade direction."""
    prior_closes: dict = {}
    # collect close at 15:58 CT bar (= 16:00 print)
    for b in bars:
        ct = b["t"].astimezone(CT)
        if ct.hour == 15 and ct.minute == 58:
            prior_closes[ct.date()] = b["c"]

    by_session: dict = {}  # session date -> {gap, prior_close, open, fill_touched}
    for b in bars:
        ct = b["t"].astimezone(CT)
        d = ct.date()
        mod = ct.hour * 60 + ct.minute
        if mod == 510:  # 08:30 CT — open of RTH
            # Look back 1-7 days for prior close
            pc = None
            for delta in range(1, 8):
                if (d - timedelta(days=delta)) in prior_closes:
                    pc = prior_closes[d - timedelta(days=delta)]
                    break
            if pc is None:
                continue
            gap = b["o"] - pc
            if abs(gap) < MIN_GAP:
                continue
            by_session[d] = {
                "gap": gap, "prior_close": pc, "open": b["o"],
                "fill_touched": False,
            }
        # Within trade window 08:30-13:00, check if prior_close was touched
        if d in by_session and 510 <= mod < 780:
            sess = by_session[d]
            pc = sess["prior_close"]
            if sess["gap"] > 0 and b["l"] <= pc:
                sess["fill_touched"] = True
            if sess["gap"] < 0 and b["h"] >= pc:
                sess["fill_touched"] = True

    return by_session


def main() -> int:
    if not CSV_PATH.exists():
        print(f"missing: {CSV_PATH}")
        return 1

    # Load trades
    enriched: list[dict] = []
    by_direction: dict[str, list[dict]] = defaultdict(list)
    by_outcome: dict[str, list[dict]] = defaultdict(list)
    by_month: dict[str, list[dict]] = defaultdict(list)
    by_dow: dict[int, list[dict]] = defaultdict(list)
    by_gap_bucket: dict[str, list[dict]] = defaultdict(list)
    for row in csv.DictReader(CSV_PATH.open()):
        reason = row.get("reason", "")
        if not reason.startswith("gap_fill_"):
            continue
        row["net_pnl"] = float(row["net_pnl"])
        entry_dt = datetime.fromisoformat(row["entry_ts"]).astimezone(CT)
        row["entry_dt_ct"] = entry_dt
        # reason: gap_fill_{long|short}_g{signed integer hundredths}
        parts = reason.split("_g")
        if len(parts) != 2:
            continue
        direction = parts[0].replace("gap_fill_", "")
        gap = int(parts[1]) / 100.0
        row["direction"] = direction
        row["gap"] = gap
        abs_gap = abs(gap)
        if abs_gap < 4:
            bucket = "0-4pt"
        elif abs_gap < 8:
            bucket = "4-8pt"
        elif abs_gap < 12:
            bucket = "8-12pt"
        else:
            bucket = "12pt+"
        row["gap_bucket"] = bucket

        enriched.append(row)
        by_direction[direction].append(row)
        by_outcome[row.get("outcome", "")].append(row)
        by_month[entry_dt.strftime("%Y-%m")].append(row)
        by_dow[entry_dt.weekday()].append(row)
        by_gap_bucket[bucket].append(row)

    # ───── pooled / direction ─────
    print(f"{'':>22} | {'n':>4} | {'WR':>5} | {'PF':>5} | {'net':>11} | {'avg':>9}")
    print("-" * 72)
    _print_row("POOLED", _stats(enriched))
    print("-" * 72)
    for d in ("long", "short"):
        _print_row(f"{d.upper()} (fade)", _stats(by_direction.get(d, [])))

    # ───── by gap bucket ─────
    print("\nBy gap size bucket:")
    print(f"{'':>22} | {'n':>4} | {'WR':>5} | {'PF':>5} | {'net':>11} | {'avg':>9}")
    print("-" * 72)
    for bucket in ("0-4pt", "4-8pt", "8-12pt", "12pt+"):
        _print_row(bucket, _stats(by_gap_bucket.get(bucket, [])))

    # ───── by exit type ─────
    print("\nBy exit type:")
    print(f"{'':>22} | {'n':>4} | {'WR':>5} | {'PF':>5} | {'net':>11} | {'avg':>9}")
    print("-" * 72)
    for o in ("target", "stop", "force_close_session"):
        _print_row(o, _stats(by_outcome.get(o, [])))

    # ───── DOW ─────
    print("\nBy CT entry day-of-week:")
    print(f"{'':>22} | {'n':>4} | {'WR':>5} | {'PF':>5} | {'net':>11} | {'avg':>9}")
    print("-" * 72)
    for dow_idx, name in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri"]):
        _print_row(name, _stats(by_dow.get(dow_idx, [])))

    # ───── monthly + Topstep ─────
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

    # ───── gap fill rate (independent of strategy) ─────
    print("\nGap-fill rate (qualifying gaps |g|>=4.0pt, fill = price "
          "touched prior_close during 08:30-13:00 CT):")
    bars_raw = stream_2min_bars()
    # Convert to dict-of-bars for the analyzer (keep just what we need)
    bars = [{"t": b.t, "o": b.o, "h": b.h, "l": b.l, "c": b.c} for b in bars_raw]
    sessions = compute_gap_fill_rate(bars)
    total = len(sessions)
    filled = sum(1 for s in sessions.values() if s["fill_touched"])
    print(f"  qualifying sessions: {total}")
    print(f"  filled:              {filled}  ({filled*100/max(1,total):.1f}%)")
    # Break out by gap-up vs gap-down
    gap_up = [s for s in sessions.values() if s["gap"] > 0]
    gap_dn = [s for s in sessions.values() if s["gap"] < 0]
    fill_up = sum(1 for s in gap_up if s["fill_touched"])
    fill_dn = sum(1 for s in gap_dn if s["fill_touched"])
    print(f"  gap-up sessions:   {len(gap_up)}  filled: {fill_up}  "
          f"({fill_up*100/max(1,len(gap_up)):.1f}%)")
    print(f"  gap-down sessions: {len(gap_dn)}  filled: {fill_dn}  "
          f"({fill_dn*100/max(1,len(gap_dn)):.1f}%)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
