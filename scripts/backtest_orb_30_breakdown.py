"""ORB_30 breakdown: pooled / by-direction / by-CT-hour / by-range-width.

Reads docs/backtest_new_fleet/orb_30.csv plus the cached 1-min bars to
compute each trade's 08:30-09:00 CT range width, then bins into tight /
medium / wide terciles. This is the extra breakdown the spec asked for —
'tells us whether the strategy works better on high-vol open days or
quiet ones'.
"""
from __future__ import annotations

import csv
import logging
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logging.basicConfig(level=logging.WARNING)
import structlog  # noqa: E402

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.backtest_new_fleet import stream_2min_bars  # noqa: E402

CSV_PATH = Path(__file__).resolve().parents[1] / "docs" / "backtest_new_fleet" / "orb_30.csv"
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


def _print_row(label: str, s: dict, width: int = 18) -> None:
    if s["n"] == 0:
        print(f"{label:>{width}} | n=0")
        return
    pf_s = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "inf"
    print(f"{label:>{width}} | {s['n']:>4} | "
          f"{s['wr']*100:>4.1f}% | {pf_s:>5} | "
          f"${s['net']:>+9.2f} | ${s['avg']:>+6.2f}")


def compute_day_ranges() -> dict[date, float]:
    """For each CT calendar date, return (range_high - range_low) across
    08:30-09:00 CT 2-min bars. Days with no bars in window are absent."""
    bars = stream_2min_bars()
    hi: dict[date, float] = {}
    lo: dict[date, float] = {}
    for b in bars:
        ct = b.t.astimezone(CT)
        mod = ct.hour * 60 + ct.minute
        if not (510 <= mod < 540):
            continue
        d = ct.date()
        hi[d] = b.h if d not in hi else max(hi[d], b.h)
        lo[d] = b.l if d not in lo else min(lo[d], b.l)
    return {d: hi[d] - lo[d] for d in hi}


def main() -> int:
    if not CSV_PATH.exists():
        print(f"missing: {CSV_PATH}")
        return 1

    print("Computing 08:30-09:00 CT ranges from cached bars...")
    day_ranges = compute_day_ranges()
    print(f"  {len(day_ranges)} days with a range\n")

    by_direction: dict[str, list[dict]] = defaultdict(list)
    by_hour: dict[int, list[dict]] = defaultdict(list)
    by_outcome: dict[str, list[dict]] = defaultdict(list)
    enriched: list[dict] = []
    for row in csv.DictReader(CSV_PATH.open()):
        reason = row.get("reason", "")
        if not reason.startswith("orb30_"):
            continue
        row["net_pnl"] = float(row["net_pnl"])
        entry_dt = datetime.fromisoformat(row["entry_ts"]).astimezone(CT)
        d = entry_dt.date()
        row["range_width"] = day_ranges.get(d)
        if row["range_width"] is None:
            continue
        direction = reason.replace("orb30_", "")
        by_direction[direction].append(row)
        by_hour[entry_dt.hour].append(row)
        by_outcome[row["outcome"]].append(row)
        enriched.append(row)

    # ───── pooled / direction ─────
    print(f"{'':>18} | {'n':>4} | {'WR':>5} | {'PF':>5} | {'net':>10} | {'avg':>7}")
    print("-" * 64)
    _print_row("POOLED", _stats(enriched))
    print("-" * 64)
    for d in ("long", "short"):
        _print_row(d.upper(), _stats(by_direction.get(d, [])))

    # ───── by CT hour ─────
    print("\nBy CT entry hour:")
    print(f"{'hr':>3} | {'n':>4} | {'WR':>5} | {'PF':>5} | {'net':>10} | {'avg':>7}")
    print("-" * 50)
    for h in sorted(by_hour):
        s = _stats(by_hour[h])
        pf_s = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "inf"
        print(f"{h:>3} | {s['n']:>4} | "
              f"{s['wr']*100:>4.1f}% | {pf_s:>5} | "
              f"${s['net']:>+9.2f} | ${s['avg']:>+6.2f}")

    # ───── by outcome (stop / target / force_close_session) ─────
    print("\nBy outcome:")
    for o, trades in sorted(by_outcome.items(), key=lambda x: -len(x[1])):
        _print_row(o, _stats(trades))

    # ───── by range-width tercile ─────
    print("\nBy 08:30-09:00 range-width tercile:")
    enriched_sorted = sorted(enriched, key=lambda r: r["range_width"])
    n = len(enriched_sorted)
    if n >= 3:
        t1_cut = enriched_sorted[n // 3]["range_width"]
        t2_cut = enriched_sorted[2 * n // 3]["range_width"]
        tight = [r for r in enriched_sorted if r["range_width"] < t1_cut]
        medium = [r for r in enriched_sorted
                  if t1_cut <= r["range_width"] < t2_cut]
        wide = [r for r in enriched_sorted if r["range_width"] >= t2_cut]
        print(f"  tercile cutpoints (pts):  tight < {t1_cut:.2f}  |  "
              f"{t1_cut:.2f}-{t2_cut:.2f}  |  >= {t2_cut:.2f}")
        print(f"{'':>18} | {'n':>4} | {'WR':>5} | {'PF':>5} | {'net':>10} | {'avg':>7}")
        print("-" * 64)
        _print_row(f"TIGHT (<{t1_cut:.1f})", _stats(tight))
        _print_row(f"MEDIUM ({t1_cut:.1f}-{t2_cut:.1f})", _stats(medium))
        _print_row(f"WIDE (>={t2_cut:.1f})", _stats(wide))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
