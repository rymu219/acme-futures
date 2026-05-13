"""Retag historical v3 trades with their distance to the nearest day-level.

Validates BOUNDARY's core thesis (level-proximity predicts reversal)
against the v3 paper-trade history before BOUNDARY ships.

⚠ Cache currency note (2026-05-12): the Databento parquet cache at
`~/.acme/backtest_cache/` ends 2026-04-30, but the v3 trade history
begins 2026-05-04. There's a 4-day gap that makes this script
*unrunnable for validation* against the current trades until the cache
is refreshed.

Until then: BOUNDARY ships in SHADOW just like IGNITION/SESSION/REGIME
— PerfTracker validates it from live SHADOW data. The original plan's
"validate-before-ship" gate is replaced with "validate-via-SHADOW."
Refresh the cache later to re-enable this script for historical analysis.

For every settled trade in `ryan_spec_v3_trades`:
  1. Read the trade's entry price + entry date (CT trade date).
  2. Compute that day's PDH/PDL/ONH/ONL/ORH/ORL from the Databento
     cached parquet bars (acme.backtest.data + acme.levels).
  3. Find the closest level and the signed distance in ticks
     (positive = above level, negative = below).
  4. Bucket-analyse by distance from level and report whether the
     trade's outcome (pnl_dollars > 0) is more / less likely near a
     level than far from one.

Output:
  - docs/v3_audit/level_retag.csv (per-trade rows with distance + level)
  - docs/v3_audit/level_proximity_buckets.csv (rollup by distance bucket)
  - prints a summary to stdout, including the verdict on the thesis.

Read-only. Doesn't write to the trades table.

Usage:
  uv run python scripts/retag_v3_trades.py
  uv run python scripts/retag_v3_trades.py --limit 1000     # subsample
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path so we can import scripts.v3_audit.* etc.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acme.backtest.data import iter_bars  # noqa: E402
from acme.contracts import MES  # noqa: E402
from acme.levels import compute_day_levels, nearest_level_distance, trading_date_ct  # noqa: E402
from scripts.v3_audit.db import (  # noqa: E402
    AUDIT_DIR,
    fetch_all_v3_trades,
    get_client,
    write_csv,
)

# Buckets in TICKS for distance to nearest level (signed).
# Pos = price above level, Neg = price below level.
DISTANCE_BUCKETS = [
    ("<-40t", lambda t: t < -40),
    ("-40..-20t", lambda t: -40 <= t < -20),
    ("-20..-10t", lambda t: -20 <= t < -10),
    ("-10..-4t", lambda t: -10 <= t < -4),
    ("near(-4..+4t)", lambda t: -4 <= t <= 4),       # within BOUNDARY's default buffer
    ("+4..+10t", lambda t: 4 < t <= 10),
    ("+10..+20t", lambda t: 10 < t <= 20),
    ("+20..+40t", lambda t: 20 < t <= 40),
    (">+40t", lambda t: t > 40),
]


def bucket_for(ticks: float) -> str:
    for name, fn in DISTANCE_BUCKETS:
        if fn(ticks):
            return name
    return "?"


def load_levels_cache(min_date: date, max_date: date) -> dict[date, Any]:
    """Compute DayLevels for every date in the range, returning a dict
    keyed by trade_date. Reads the Databento cache once and shards bars
    per CT trade date.

    The compute loop streams bars from `iter_bars()` which yields UTC
    timestamps; we partition into per-day buckets (trade-date-keyed
    rolling window of bars covering [prior-day-RTH-open, current-day-09:00 CT]).
    """
    # We need bars spanning min_date-1 (for PDH/PDL of min_date) through
    # max_date inclusive.
    start_utc = datetime.combine(min_date - timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    end_utc = datetime.combine(max_date + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    bars_by_window: dict[date, list[Any]] = defaultdict(list)

    print(f"Loading bars from cache: {start_utc} → {end_utc}")
    bar_count = 0
    for b in iter_bars(start=start_utc, end=end_utc):
        bar_count += 1
        td = trading_date_ct(b.t)
        # A single bar can contribute to multiple trade-date windows
        # (e.g. a prior-day RTH bar is PDH source for *the next* trade date).
        bars_by_window[td].append(b)
        bars_by_window[td + timedelta(days=1)].append(b)
    print(f"Streamed {bar_count:,} bars across {len(bars_by_window)} day-windows")

    levels_by_date: dict[date, Any] = {}
    for td, bars in bars_by_window.items():
        if td < min_date or td > max_date:
            continue
        levels_by_date[td] = compute_day_levels(bars, td, contract="MES")
    return levels_by_date


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=None,
                   help="Subsample trades for a quick run.")
    p.add_argument("--mode", default="paper", choices=("paper", "live", "shadow"))
    args = p.parse_args()

    sb = get_client()
    print(f"Fetching {args.mode} trades from ryan_spec_v3_trades...")
    trades = fetch_all_v3_trades(sb, mode=args.mode)
    settled = [t for t in trades if t.get("exit_ts") and t.get("pnl_dollars") is not None]
    if args.limit:
        settled = settled[-args.limit:]
    print(f"Working with {len(settled):,} settled trades")
    if not settled:
        print("No trades found.")
        return 1

    # Determine date range
    def _td(row: dict) -> date | None:
        ts = row.get("entry_ts")
        if not ts:
            return None
        try:
            d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            return None
        return trading_date_ct(d)

    trade_dates = {_td(t) for t in settled}
    trade_dates.discard(None)
    if not trade_dates:
        print("Could not determine trade dates from rows.")
        return 1
    min_date = min(trade_dates)
    max_date = max(trade_dates)
    print(f"Trade-date range: {min_date} → {max_date}")

    levels_by_date = load_levels_cache(min_date, max_date)

    # Per-trade retag
    per_trade_rows: list[dict] = []
    bucket_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "wins": 0, "net_pnl": 0.0}
    )
    not_classified = 0

    tick = MES.tick_size
    for t in settled:
        td = _td(t)
        if td is None:
            not_classified += 1
            continue
        levels = levels_by_date.get(td)
        if levels is None:
            not_classified += 1
            continue
        try:
            entry = float(t["entry_price"])
        except (TypeError, ValueError):
            not_classified += 1
            continue
        name, ticks = nearest_level_distance(entry, levels, tick_size=tick)
        if name is None or ticks is None:
            not_classified += 1
            continue
        b = bucket_for(ticks)
        pnl = float(t["pnl_dollars"])
        is_win = pnl > 0
        per_trade_rows.append({
            "trade_id": t.get("id"),
            "strategy_id": t.get("strategy_id"),
            "direction": t.get("direction"),
            "trade_date": str(td),
            "entry_price": entry,
            "nearest_level": name,
            "distance_ticks": round(ticks, 2),
            "bucket": b,
            "pnl_dollars": pnl,
            "is_win": int(is_win),
        })
        stat = bucket_stats[b]
        stat["n"] += 1
        stat["wins"] += int(is_win)
        stat["net_pnl"] += pnl

    print(f"Classified {len(per_trade_rows):,} trades; {not_classified:,} not classified")

    # Write CSVs
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    write_csv(
        "level_retag.csv", per_trade_rows,
        ["trade_id", "strategy_id", "direction", "trade_date",
         "entry_price", "nearest_level", "distance_ticks",
         "bucket", "pnl_dollars", "is_win"],
    )

    bucket_rows = []
    for name, _ in DISTANCE_BUCKETS:
        s = bucket_stats.get(name, {"n": 0, "wins": 0, "net_pnl": 0.0})
        if s["n"] == 0:
            bucket_rows.append({"bucket": name, "n": 0, "win_rate": "",
                                "net_pnl": "", "avg_pnl": ""})
            continue
        bucket_rows.append({
            "bucket": name,
            "n": s["n"],
            "win_rate": round(s["wins"] / s["n"], 3),
            "net_pnl": round(s["net_pnl"], 2),
            "avg_pnl": round(s["net_pnl"] / s["n"], 2),
        })
    write_csv("level_proximity_buckets.csv", bucket_rows,
              ["bucket", "n", "win_rate", "net_pnl", "avg_pnl"])

    # Print summary + verdict
    print()
    print(f"{'bucket':<16}{'n':>8}{'WR':>8}{'net':>12}{'avg':>10}")
    print("-" * 56)
    for row in bucket_rows:
        wr = f"{row['win_rate']*100:.1f}%" if row["win_rate"] != "" else "—"
        net = f"${row['net_pnl']:,.2f}" if row["net_pnl"] != "" else "—"
        avg = f"${row['avg_pnl']:.2f}" if row["avg_pnl"] != "" else "—"
        print(f"{row['bucket']:<16}{row['n']:>8}{wr:>8}{net:>12}{avg:>10}")

    near = bucket_stats.get("near(-4..+4t)", {"n": 0, "wins": 0, "net_pnl": 0.0})
    other_n = sum(s["n"] for k, s in bucket_stats.items() if k != "near(-4..+4t)")
    other_wins = sum(s["wins"] for k, s in bucket_stats.items() if k != "near(-4..+4t)")
    other_net = sum(s["net_pnl"] for k, s in bucket_stats.items() if k != "near(-4..+4t)")

    print()
    print("Thesis test — 'near (-4..+4 ticks) vs everywhere else':")
    if near["n"] > 0 and other_n > 0:
        near_wr = near["wins"] / near["n"]
        other_wr = other_wins / other_n
        print(f"  near bucket: n={near['n']:,}  WR {near_wr*100:.1f}%  net ${near['net_pnl']:,.2f}")
        print(f"  elsewhere:   n={other_n:,}  WR {other_wr*100:.1f}%  net ${other_net:,.2f}")
        if near_wr > other_wr + 0.05:
            print("  ► thesis supported: trades near a level win >5pp more than elsewhere.")
        elif near_wr < other_wr - 0.05:
            print("  ► thesis CONTRADICTED: trades near a level win >5pp less than elsewhere.")
        else:
            print("  ► thesis inconclusive: near-bucket WR within 5pp of elsewhere.")
    else:
        print("  insufficient data for either bucket.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
