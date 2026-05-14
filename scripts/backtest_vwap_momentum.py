"""VWAP_MOMENTUM parameter sweep.

Streams 1-min MES bars from the local Databento cache, aggregates to
2-min bars, and runs a custom backtest loop that implements:

  - intraday VWAP from 08:30 CT (volume-weighted typical price)
  - long entry on close >= VWAP + entry_threshold_pts
  - initial stop entry - initial_stop_pts
  - trailing stop = high_watermark - trail_distance_pts
  - effective stop = max(initial_stop, trailing_stop)
  - hard close at 13:00 CT
  - one trade per session, long only

Sweep:
  entry_threshold_pts ∈ {4, 6, 8, 10}
  trail_distance_pts  ∈ {4, 6, 8}
  initial_stop_pts    = 8  (fixed)
→ 12 combinations.

The custom loop is necessary because the existing `backtest_strategy()`
in backtest_new_fleet.py uses a fixed bracket (stop + target) and has no
hook to tighten the stop as a position runs. The trade-dict schema we
write here is identical to what backtest_strategy() produces, so the
breakdown script (backtest_vwap_momentum_breakdown.py) works on the same
columns the gap_fill / boundary breakdown scripts do.

Outputs:
  - docs/backtest_vwap_momentum/sweep.csv       sweep summary
  - docs/backtest_vwap_momentum/{thr}_{trl}.csv per-config trade lists
  - docs/backtest_vwap_momentum/primary.csv     copy of the best-PF combo

Usage:
  uv run python scripts/backtest_vwap_momentum.py
  uv run python scripts/backtest_vwap_momentum.py --start 2026-01-01
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
import time as time_mod
from datetime import UTC, datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

logging.basicConfig(level=logging.WARNING)
import structlog  # noqa: E402

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acme.broker.base import Bar  # noqa: E402
from acme.contracts import MES  # noqa: E402
from acme.risk import TOPSTEP_50K  # noqa: E402
from scripts.backtest_new_fleet import stream_2min_bars  # noqa: E402

CT = ZoneInfo("America/Chicago")

OUT_DIR = Path(__file__).resolve().parents[1] / "docs" / "backtest_vwap_momentum"

# Sweep grid — per spec.
ENTRY_THRESHOLDS = [4.0, 6.0, 8.0, 10.0]
TRAIL_DISTANCES = [4.0, 6.0, 8.0]
INITIAL_STOP_PTS = 8.0           # fixed per spec

# Session window (CT). VWAP cumulates from session_open; force-close at
# hard_close. Entries only allowed strictly inside [session_open, hard_close).
SESSION_OPEN_CT = dtime(8, 30)
HARD_CLOSE_CT = dtime(13, 0)


def _to_minutes(t: dtime) -> int:
    return t.hour * 60 + t.minute


SESSION_OPEN_MIN = _to_minutes(SESSION_OPEN_CT)
HARD_CLOSE_MIN = _to_minutes(HARD_CLOSE_CT)


def _ct_parts(bar: Bar) -> tuple[object, int]:
    """Returns (CT date, CT minute-of-day) for a bar."""
    ct = bar.t.astimezone(CT)
    return ct.date(), ct.hour * 60 + ct.minute


# ───────────────────────── core backtest ──────────────────────────


def run_vwap_momentum(
    bars: list[Bar],
    *,
    entry_threshold_pts: float,
    initial_stop_pts: float,
    trail_distance_pts: float,
) -> list[dict]:
    """Run the VWAP_MOMENTUM strategy with trailing stop over the bar stream.

    Returns a list of trade-close dicts matching backtest_new_fleet's schema:
    {strategy, entry_ts, exit_ts, side, entry_price, exit_price, net_pnl,
     outcome, bars_held_minutes, reason}.

    Exit precedence on a bar (long position):
      1. If bar.l <= effective_stop → exit at effective_stop, outcome
         "trailing_stop" if hw - trail > initial_stop_price else "initial_stop".
      2. Else: update high watermark with bar.h.
      3. If bar is at/after HARD_CLOSE_MIN → exit at bar.o, outcome
         "force_close_session". This runs even after step 1/2 because the
         force-close bar's exit happens at the open, before the bar trades
         (matches gap_fill convention).
    """
    point_value = MES.point_value
    round_turn_fee = TOPSTEP_50K.round_turn_fees.get("MES", 0.0)

    closes: list[dict] = []
    # Per-session state
    current_date = None
    cum_tpv = 0.0
    cum_v = 0.0
    entered_today = False
    # Open position state (long only)
    pos_entry_price = 0.0
    pos_entry_t = None
    pos_size = 0
    pos_high_watermark = 0.0
    pos_reason = ""

    for bar in bars:
        ct_date, ct_minute = _ct_parts(bar)

        # New CT day → reset session state.
        if ct_date != current_date:
            # Any leftover open position from the previous day shouldn't
            # exist (force-close at 13:00 each day catches them), but be
            # defensive — force-close at the prior bar's close if we somehow
            # carry across a day boundary.
            if pos_size > 0:
                # This is the previous bar's close, but we already exited at
                # 13:00 in normal flow; only reachable if HARD_CLOSE somehow
                # didn't fire. Use this bar's open as a safe exit.
                exit_price = bar.o
                price_pnl = (exit_price - pos_entry_price) * point_value * pos_size
                net_pnl = round(price_pnl - round_turn_fee * pos_size, 2)
                bars_held_min = max(
                    1, int((bar.t - pos_entry_t).total_seconds() / 60))
                closes.append({
                    "strategy": "vwap_momentum",
                    "entry_ts": pos_entry_t.isoformat(),
                    "exit_ts": bar.t.isoformat(),
                    "side": "buy",
                    "entry_price": pos_entry_price,
                    "exit_price": exit_price,
                    "net_pnl": net_pnl,
                    "outcome": "force_close_session",
                    "bars_held_minutes": bars_held_min,
                    "reason": pos_reason,
                })
                pos_size = 0
            current_date = ct_date
            cum_tpv = 0.0
            cum_v = 0.0
            entered_today = False

        # Only operate during the session window. Pre-08:30 and post-13:00
        # bars are skipped entirely (no VWAP cumulation, no entries, no
        # exits — by design, since the force-close at 13:00 handles closing).
        if ct_minute < SESSION_OPEN_MIN:
            continue

        # ── Force-close gate (runs first; exits at bar.o before any other
        # logic for the 13:00 bar). Mirrors backtest_new_fleet's
        # wants_force_flat handling.
        if ct_minute >= HARD_CLOSE_MIN:
            if pos_size > 0:
                exit_price = bar.o
                price_pnl = (exit_price - pos_entry_price) * point_value * pos_size
                net_pnl = round(price_pnl - round_turn_fee * pos_size, 2)
                bars_held_min = max(
                    1, int((bar.t - pos_entry_t).total_seconds() / 60))
                closes.append({
                    "strategy": "vwap_momentum",
                    "entry_ts": pos_entry_t.isoformat(),
                    "exit_ts": bar.t.isoformat(),
                    "side": "buy",
                    "entry_price": pos_entry_price,
                    "exit_price": exit_price,
                    "net_pnl": net_pnl,
                    "outcome": "force_close_session",
                    "bars_held_minutes": bars_held_min,
                    "reason": pos_reason,
                })
                pos_size = 0
            continue   # nothing else happens at or after 13:00

        # ── VWAP update with current bar. Done BEFORE entry/exit so the
        # 08:30 bar sees a valid VWAP (= its own typical price).
        if bar.v > 0:
            tp = (bar.h + bar.l + bar.c) / 3.0
            cum_tpv += tp * bar.v
            cum_v += bar.v
        vwap = (cum_tpv / cum_v) if cum_v > 0 else None

        # ── Exit check (long position only).
        if pos_size > 0:
            # Update high watermark to bar's high BEFORE computing trail —
            # standard convention: trail moves to (running max high) − dist.
            # Note: bar.h could trigger a higher trail before bar.l triggers
            # the stop on the same bar; we use bar.h for hw update first to
            # be conservative (gives the trail the benefit of seeing high
            # before low).
            initial_stop_price = pos_entry_price - initial_stop_pts
            trailing_stop_price = pos_high_watermark - trail_distance_pts
            effective_stop = max(initial_stop_price, trailing_stop_price)

            if bar.l <= effective_stop:
                # Stop fired. Determine which one bound.
                outcome = ("trailing_stop"
                           if trailing_stop_price > initial_stop_price
                           else "initial_stop")
                exit_price = effective_stop
                price_pnl = (exit_price - pos_entry_price) * point_value * pos_size
                net_pnl = round(price_pnl - round_turn_fee * pos_size, 2)
                bars_held_min = max(
                    1, int((bar.t - pos_entry_t).total_seconds() / 60))
                closes.append({
                    "strategy": "vwap_momentum",
                    "entry_ts": pos_entry_t.isoformat(),
                    "exit_ts": bar.t.isoformat(),
                    "side": "buy",
                    "entry_price": pos_entry_price,
                    "exit_price": exit_price,
                    "net_pnl": net_pnl,
                    "outcome": outcome,
                    "bars_held_minutes": bars_held_min,
                    "reason": pos_reason,
                })
                pos_size = 0
            else:
                # No exit — update watermark for next bar's trail check.
                if bar.h > pos_high_watermark:
                    pos_high_watermark = bar.h

            # Whether we exited or not, do NOT consider entering on the same
            # bar — one-trade-per-session locks anything further.
            continue

        # ── Entry check (only when flat and haven't traded yet today).
        if entered_today or vwap is None:
            continue

        distance = bar.c - vwap
        if distance < entry_threshold_pts:
            continue

        # Entry. Convention matches backtest_new_fleet: entry price = bar.c,
        # no slippage in the dry-run path.
        pos_entry_price = bar.c
        pos_entry_t = bar.t
        pos_size = 1
        pos_high_watermark = bar.c   # watermark starts at entry close
        dist_int = int(round(distance * 100))
        pos_reason = f"vwap_momentum_long_d{dist_int:+07d}"
        entered_today = True

    # Flush any still-open position at the last bar (rare; happens only if
    # the bar stream ends mid-session before 13:00). Match the
    # force_close_end_of_backtest convention from backtest_new_fleet.
    if pos_size > 0 and bars:
        last = bars[-1]
        exit_price = last.c
        price_pnl = (exit_price - pos_entry_price) * point_value * pos_size
        net_pnl = round(price_pnl - round_turn_fee * pos_size, 2)
        bars_held_min = max(
            1, int((last.t - pos_entry_t).total_seconds() / 60))
        closes.append({
            "strategy": "vwap_momentum",
            "entry_ts": pos_entry_t.isoformat(),
            "exit_ts": last.t.isoformat(),
            "side": "buy",
            "entry_price": pos_entry_price,
            "exit_price": exit_price,
            "net_pnl": net_pnl,
            "outcome": "force_close_end_of_backtest",
            "bars_held_minutes": bars_held_min,
            "reason": pos_reason,
        })

    return closes


# ───────────────────────── stats + reporting ───────────────────────


def _stats(closes: list[dict]) -> dict:
    """Mirrors backtest_new_fleet._strategy_stats so the sweep table reads
    the same as gap_fill's. bar-1 cohort = trades exiting on the bar
    immediately after entry (bars_held_minutes <= 2 for 2-min bars)."""
    n = len(closes)
    if n == 0:
        return {"n": 0, "net_pnl": 0.0, "win_rate": 0.0,
                "profit_factor": 0.0, "avg_pnl": 0.0,
                "max_win": 0.0, "max_loss": 0.0,
                "bar1_n": 0, "bar1_net": 0.0, "bar1_wr": 0.0}
    pnls = [c["net_pnl"] for c in closes]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gw = sum(wins)
    gl = -sum(losses)
    bar1 = [c for c in closes if c.get("bars_held_minutes") and
            c["bars_held_minutes"] <= 2]
    bar1_pnls = [c["net_pnl"] for c in bar1]
    return {
        "n": n,
        "net_pnl": round(sum(pnls), 2),
        "win_rate": round(len(wins) / n, 3),
        "profit_factor": round(gw / max(gl, 0.01), 2),
        "avg_pnl": round(sum(pnls) / n, 2),
        "max_win": round(max(pnls), 2),
        "max_loss": round(min(pnls), 2),
        "bar1_n": len(bar1),
        "bar1_net": round(sum(bar1_pnls), 2) if bar1_pnls else 0.0,
        "bar1_wr": round(
            sum(1 for p in bar1_pnls if p > 0) / max(len(bar1), 1), 3
        ),
    }


def _write_csv(path: Path, rows: list[dict], cols: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


TRADE_COLS = ["strategy", "entry_ts", "exit_ts", "side",
              "entry_price", "exit_price", "net_pnl", "outcome",
              "bars_held_minutes", "reason"]


# ───────────────────────── main ───────────────────────────────────


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", default=None,
                   help="ISO date (e.g. 2024-04-01); default: cache start.")
    p.add_argument("--end", default=None,
                   help="ISO date (e.g. 2026-04-30); default: cache end.")
    args = p.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC) if args.start else None
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC) if args.end else None

    print(f"Loading 2-min bars from cache (start={start}, end={end})...")
    t0 = time_mod.time()
    bars = stream_2min_bars(start=start, end=end)
    print(f"  loaded in {time_mod.time() - t0:.1f}s\n")

    if not bars:
        print("No bars in range. Exiting.")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"VWAP_MOMENTUM sweep — initial_stop fixed at {INITIAL_STOP_PTS}pt")
    print(f"{'entry_thr':>9} {'trail':>6} | {'n':>4} | {'WR':>5} | "
          f"{'PF':>5} | {'net':>10} | {'avg':>8} | bar1")
    print("-" * 84)

    sweep_rows = []
    best = None   # (pf, summary_dict, closes, config_key)

    for thr in ENTRY_THRESHOLDS:
        for trl in TRAIL_DISTANCES:
            t_combo = time_mod.time()
            closes = run_vwap_momentum(
                bars,
                entry_threshold_pts=thr,
                initial_stop_pts=INITIAL_STOP_PTS,
                trail_distance_pts=trl,
            )
            s = _stats(closes)
            elapsed = time_mod.time() - t_combo

            config_key = f"thr{int(thr)}_trl{int(trl)}"
            row = {
                "entry_threshold_pts": thr,
                "initial_stop_pts": INITIAL_STOP_PTS,
                "trail_distance_pts": trl,
                **s,
                "elapsed_s": round(elapsed, 1),
            }
            sweep_rows.append(row)
            print(f"{thr:>9.1f} {trl:>6.1f} | {s['n']:>4} | "
                  f"{s['win_rate']*100:>4.1f}% | "
                  f"{s['profit_factor']:>4.2f} | "
                  f"${s['net_pnl']:>+9.2f} | "
                  f"${s['avg_pnl']:>+7.2f} | "
                  f"{s['bar1_n']:>3}/${s['bar1_net']:>+8.2f}")

            # Per-config trade list — feeds the breakdown script.
            _write_csv(OUT_DIR / f"{config_key}.csv", closes, TRADE_COLS)

            # Track the best (by PF, then by net as a tiebreaker; ignore
            # combos with <30 trades to filter noise).
            if s["n"] >= 30:
                key = (s["profit_factor"], s["net_pnl"])
                if best is None or key > best[0]:
                    best = (key, s, closes, config_key)

    # Sweep summary CSV
    sweep_cols = ["entry_threshold_pts", "initial_stop_pts", "trail_distance_pts",
                  "n", "net_pnl", "win_rate", "profit_factor", "avg_pnl",
                  "max_win", "max_loss",
                  "bar1_n", "bar1_net", "bar1_wr", "elapsed_s"]
    _write_csv(OUT_DIR / "sweep.csv", sweep_rows, sweep_cols)

    # Primary CSV — copy of the best combo for the breakdown script default.
    if best is not None:
        _, s, closes, config_key = best
        _write_csv(OUT_DIR / "primary.csv", closes, TRADE_COLS)
        print(f"\nBest combo: {config_key} → PF={s['profit_factor']:.2f}, "
              f"net=${s['net_pnl']:+,.2f}, n={s['n']}")
        print(f"Copied to {OUT_DIR / 'primary.csv'} for the breakdown script.")
    else:
        print("\nNo combo had n >= 30 trades; primary.csv not written.")

    print(f"\nWrote sweep summary + 12 per-config CSVs to {OUT_DIR}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
