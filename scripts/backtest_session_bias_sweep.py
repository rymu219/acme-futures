"""Sweep SESSION's bias-decay guard threshold over the full 2-year cache.

Background: the 2-year backtest at master shows SESSION at PF 0.81 over
11,436 trades. Today's live data (2026-05-12 18:36-18:44 CT) showed
5 consecutive 1-bar stops totalling -$115 as price marched down while
SESSION's 12-hour bias classifier was still locked UP from the morning
rally. The shipped fix (`bias_decay_atr_thresh=0.5`) is supposed to
suppress entries when the most recent 30 min of bars moves against
the bias by >= 0.5 x ATR.

This sweep runs SESSION at thresh in {0.0, 0.3, 0.5, 0.7} and reports
the trade count, PF, net, win rate, and bar-1 cohort for each. The
0.0 row gives us the no-guard baseline; the 0.5 row should match the
main 2-year backtest exactly (sanity check). 0.3 and 0.7 probe whether
the current default is the optimum or whether a tighter/looser guard
would do better.

Usage:
    uv run python scripts/backtest_session_bias_sweep.py
"""
from __future__ import annotations

import csv
import logging
import sys
import time as time_mod
from pathlib import Path

# Silence structlog before any acme.* imports
logging.basicConfig(level=logging.WARNING)
import structlog  # noqa: E402

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acme.strategies.session import SessionConfig, SessionStrategy  # noqa: E402
from scripts.backtest_new_fleet import (  # noqa: E402
    _strategy_stats,
    backtest_strategy,
    stream_2min_bars,
)

OUT_DIR = Path(__file__).resolve().parents[1] / "docs" / "backtest_new_fleet"


THRESHOLDS = [0.0, 0.3, 0.5, 0.7]


def main() -> int:
    print("Loading 2-min bars from cache...")
    t0 = time_mod.time()
    bars = stream_2min_bars()
    print(f"  loaded in {time_mod.time() - t0:.1f}s\n")

    rows: list[dict] = []
    for thresh in THRESHOLDS:
        cfg = SessionConfig(bias_decay_atr_thresh=thresh)
        instance = SessionStrategy(config=cfg)
        t0 = time_mod.time()
        closes = backtest_strategy(instance, bars, name="session")
        stats = _strategy_stats(closes)
        elapsed = time_mod.time() - t0
        rows.append({
            "bias_decay_atr_thresh": thresh,
            **stats,
            "elapsed_s": round(elapsed, 1),
        })
        guard_label = "OFF" if thresh == 0.0 else f"{thresh:.1f}"
        print(f"  thresh={guard_label:<4}  n={stats['n']:>6}  "
              f"WR={stats['win_rate']*100:>4.1f}%  PF={stats['profit_factor']:>4.2f}  "
              f"net=${stats['net_pnl']:>+9.2f}  "
              f"avg=${stats['avg_pnl']:>+6.2f}  "
              f"bar1_n={stats['bar1_n']:>3}  bar1_net=${stats['bar1_net']:>+8.2f}  "
              f"({elapsed:.1f}s)")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cols = list(rows[0].keys()) if rows else []
    out_path = OUT_DIR / "session_bias_sweep.csv"
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"\nWrote {out_path}")

    # Sanity check: the 0.5 row should match the main 2-year backtest's
    # SESSION row from docs/backtest_new_fleet/summary.csv
    main_summary = OUT_DIR / "summary.csv"
    if main_summary.exists():
        with main_summary.open() as f:
            for r in csv.DictReader(f):
                if r["strategy"] == "session":
                    main_n = int(r["n"])
                    main_net = float(r["net_pnl"])
                    sweep_05 = next(
                        (x for x in rows if x["bias_decay_atr_thresh"] == 0.5),
                        None,
                    )
                    if sweep_05:
                        print("\nSanity check (main summary vs sweep at 0.5):")
                        print(f"  main:  n={main_n}  net=${main_net:+,.2f}")
                        print(f"  sweep: n={sweep_05['n']}  net=${sweep_05['net_pnl']:+,.2f}")
                        if main_n == sweep_05["n"] and abs(main_net - sweep_05["net_pnl"]) < 0.01:
                            print("  ✓ MATCH")
                        else:
                            print("  ✗ MISMATCH — wrapper not using same code path")
                    break

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
