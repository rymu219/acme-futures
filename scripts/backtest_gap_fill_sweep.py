"""GAP_FILL parameter sweep.

min_gap_points ∈ {2, 4, 6, 8}  ×  stop_gap_multiple ∈ {0.5, 1.0, 1.5}
= 12 combinations. Load bars once, run the strategy each time, print
the comparison table and write CSV.
"""
from __future__ import annotations

import csv
import logging
import sys
import time as time_mod
from pathlib import Path

logging.basicConfig(level=logging.WARNING)
import structlog  # noqa: E402

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acme.strategies.gap_fill import GapFillConfig, GapFillStrategy  # noqa: E402
from scripts.backtest_new_fleet import (  # noqa: E402
    _strategy_stats,
    backtest_strategy,
    stream_2min_bars,
)

OUT_DIR = Path(__file__).resolve().parents[1] / "docs" / "backtest_new_fleet"
MIN_GAPS = [2.0, 4.0, 6.0, 8.0]
STOP_MULTS = [0.5, 1.0, 1.5]


def main() -> int:
    print("Loading 2-min bars from cache...")
    t0 = time_mod.time()
    bars = stream_2min_bars()
    print(f"  loaded in {time_mod.time() - t0:.1f}s\n")

    print(f"{'min_gap':>8} {'stop_mult':>10} | {'n':>4} | {'WR':>5} | "
          f"{'PF':>5} | {'net':>10} | {'avg':>8} | bar1")
    print("-" * 84)

    rows = []
    for mg in MIN_GAPS:
        for sm in STOP_MULTS:
            cfg = GapFillConfig(min_gap_points=mg, stop_gap_multiple=sm)
            s = GapFillStrategy(config=cfg)
            t0 = time_mod.time()
            closes = backtest_strategy(s, bars, name="gap_fill")
            stats = _strategy_stats(closes)
            elapsed = time_mod.time() - t0
            rows.append({
                "min_gap_points": mg,
                "stop_gap_multiple": sm,
                **stats,
                "elapsed_s": round(elapsed, 1),
            })
            print(f"{mg:>8.1f} {sm:>10.2f} | {stats['n']:>4} | "
                  f"{stats['win_rate']*100:>4.1f}% | "
                  f"{stats['profit_factor']:>4.2f} | "
                  f"${stats['net_pnl']:>+9.2f} | "
                  f"${stats['avg_pnl']:>+7.2f} | "
                  f"{stats['bar1_n']:>3}/${stats['bar1_net']:>+8.2f}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "gap_fill_sweep.csv"
    cols = list(rows[0].keys()) if rows else []
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
