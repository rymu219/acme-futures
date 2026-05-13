"""Sweep ORB_PULLBACK across opening-range widths (15/30/45 min).

The user spec lists opening_range_minutes as a sweep parameter — this
mirrors backtest_session_bias_sweep.py: load bars once, run the
strategy three times, print the comparison.
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

from acme.strategies.orb_pullback import ORBPullbackConfig, ORBPullbackStrategy  # noqa: E402
from scripts.backtest_new_fleet import (  # noqa: E402
    _strategy_stats,
    backtest_strategy,
    stream_2min_bars,
)

OUT_DIR = Path(__file__).resolve().parents[1] / "docs" / "backtest_new_fleet"
WIDTHS = [15, 30, 45]


def main() -> int:
    print("Loading 2-min bars from cache...")
    t0 = time_mod.time()
    bars = stream_2min_bars()
    print(f"  loaded in {time_mod.time() - t0:.1f}s\n")

    rows: list[dict] = []
    for width in WIDTHS:
        cfg = ORBPullbackConfig(opening_range_minutes=width)
        s = ORBPullbackStrategy(config=cfg)
        t0 = time_mod.time()
        closes = backtest_strategy(s, bars, name="orb_pullback")
        stats = _strategy_stats(closes)
        elapsed = time_mod.time() - t0
        rows.append({
            "opening_range_minutes": width,
            **stats,
            "elapsed_s": round(elapsed, 1),
        })
        print(f"  OR={width:>2}min  n={stats['n']:>4}  "
              f"WR={stats['win_rate']*100:>4.1f}%  "
              f"PF={stats['profit_factor']:>4.2f}  "
              f"net=${stats['net_pnl']:>+9.2f}  "
              f"avg=${stats['avg_pnl']:>+6.2f}  "
              f"bar1={stats['bar1_n']:>3}/${stats['bar1_net']:>+7.2f}  "
              f"({elapsed:.1f}s)")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "orb_pullback_sweep.csv"
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
