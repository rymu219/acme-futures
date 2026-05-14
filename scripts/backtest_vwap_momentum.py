"""VWAP_MOMENTUM parameter sweep.

Streams 1-min MES bars from the local Databento cache, aggregates to
2-min bars, and runs a custom backtest loop that implements:

  - intraday VWAP from 08:30 CT (volume-weighted typical price)
  - symmetric long+short entries on the FIRST in-window crossing of
    +entry_threshold_pts (long) or -entry_threshold_pts (short).
    Window default 09:00-10:00 CT (the hour the by-hour breakdown on
    the long-only run showed carries the strategy's edge). Carry-over
    pre-window crossings consume the trigger flag without entering.
  - initial stop entry -/+ initial_stop_pts (sign-aware)
  - trailing stop tracks watermark ± trail_distance_pts (high for
    longs, low for shorts)
  - hard close at 13:00 CT
  - one trade per DIRECTION per session — long and short can both
    fire on the same session if both thresholds cross in-window

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
from dataclasses import dataclass
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
# hard_close. Entries only allowed strictly inside [entry_window_start,
# entry_window_end) — a narrower band carved out of the session window.
# Per the by-hour breakdown on the unrestricted run, 09:xx CT delivered
# PF 1.43 / +$756 while the rest of the session was net-flat to negative.
# This gate is the "one variable" change vs. the original sweep.
SESSION_OPEN_CT = dtime(8, 30)
HARD_CLOSE_CT = dtime(13, 0)
ENTRY_WINDOW_START_CT = dtime(9, 0)
ENTRY_WINDOW_END_CT = dtime(10, 0)


def _to_minutes(t: dtime) -> int:
    return t.hour * 60 + t.minute


SESSION_OPEN_MIN = _to_minutes(SESSION_OPEN_CT)
HARD_CLOSE_MIN = _to_minutes(HARD_CLOSE_CT)
ENTRY_WINDOW_START_MIN = _to_minutes(ENTRY_WINDOW_START_CT)
ENTRY_WINDOW_END_MIN = _to_minutes(ENTRY_WINDOW_END_CT)


def _ct_parts(bar: Bar) -> tuple[object, int]:
    """Returns (CT date, CT minute-of-day) for a bar."""
    ct = bar.t.astimezone(CT)
    return ct.date(), ct.hour * 60 + ct.minute


# ───────────────────────── core backtest ──────────────────────────


@dataclass
class _Position:
    """Open phantom position.

    `watermark` tracks the high (for long) or low (for short) since entry.
    Trailing stop computes off this — `watermark - trail_distance` for
    long (stop sits below the high), `watermark + trail_distance` for
    short (stop sits above the low).
    """
    side: str               # "buy" or "sell"
    entry_price: float
    entry_t: datetime
    size: int
    watermark: float
    reason: str


def _close_record(
    pos: _Position, exit_price: float, exit_t: datetime, outcome: str,
    *, point_value: float, round_turn_fee: float,
) -> dict:
    """Build a trade-close dict matching backtest_new_fleet's schema. P&L
    is sign-aware: longs profit on price up, shorts on price down."""
    sign = 1 if pos.side == "buy" else -1
    price_pnl = sign * (exit_price - pos.entry_price) * point_value * pos.size
    net_pnl = round(price_pnl - round_turn_fee * pos.size, 2)
    bars_held_min = max(
        1, int((exit_t - pos.entry_t).total_seconds() / 60))
    return {
        "strategy": "vwap_momentum",
        "entry_ts": pos.entry_t.isoformat(),
        "exit_ts": exit_t.isoformat(),
        "side": pos.side,
        "entry_price": pos.entry_price,
        "exit_price": exit_price,
        "net_pnl": net_pnl,
        "outcome": outcome,
        "bars_held_minutes": bars_held_min,
        "reason": pos.reason,
    }


def _exit_check(
    pos: _Position, bar: Bar, initial_stop_pts: float, trail_distance_pts: float,
) -> tuple[float, str] | None:
    """Returns (exit_price, outcome) if this bar hits the stop, else None.

    Symmetric long/short logic:
      long  → stops below entry; exit when bar.l <= max(init, trail)
      short → stops above entry; exit when bar.h >= min(init, trail)

    Outcome flag tracks which stop bound: "trailing_stop" when the trail
    is tighter than the initial; "initial_stop" otherwise (including ties,
    matching the long-only convention from the prior implementation).
    """
    if pos.side == "buy":
        init = pos.entry_price - initial_stop_pts
        trail = pos.watermark - trail_distance_pts
        eff = max(init, trail)
        if bar.l <= eff:
            outcome = "trailing_stop" if trail > init else "initial_stop"
            return eff, outcome
    else:   # "sell"
        init = pos.entry_price + initial_stop_pts
        trail = pos.watermark + trail_distance_pts
        eff = min(init, trail)
        if bar.h >= eff:
            outcome = "trailing_stop" if trail < init else "initial_stop"
            return eff, outcome
    return None


def run_vwap_momentum(
    bars: list[Bar],
    *,
    entry_threshold_pts: float,
    initial_stop_pts: float,
    trail_distance_pts: float,
    allow_longs: bool = True,
    allow_shorts: bool = True,
) -> list[dict]:
    """Run VWAP_MOMENTUM (trailing stop) over the bar stream.

    Trade-close dicts match backtest_new_fleet's schema (strategy, entry_ts,
    exit_ts, side, entry_price, exit_price, net_pnl, outcome,
    bars_held_minutes, reason). `side` is "buy" for longs, "sell" for shorts.

    Symmetric long+short by default. `long_triggered` and `short_triggered`
    are independent one-shot flags per session: the FIRST crossing of
    +threshold (long) and -threshold (short) consumes the respective
    trigger. A session can fire BOTH a long and a short if both crossings
    happen in-window independently. Carry-over (pre-window crossing) and
    after-window crossings consume the trigger flag without entering.

    Open positions are tracked in a list — at most 2 concurrent (one long,
    one short). Each has its own watermark (high for long, low for short)
    and exits via its own effective stop. Force-close at 13:00 CT closes
    all open positions at the bar's open.
    """
    point_value = MES.point_value
    round_turn_fee = TOPSTEP_50K.round_turn_fees.get("MES", 0.0)

    closes: list[dict] = []
    # Per-session state
    current_date = None
    cum_tpv = 0.0
    cum_v = 0.0
    long_triggered = False
    short_triggered = False
    open_positions: list[_Position] = []

    for bar in bars:
        ct_date, ct_minute = _ct_parts(bar)

        # New CT day → reset session state.
        if ct_date != current_date:
            # Defensive: any leftover position from the previous day shouldn't
            # exist (13:00 force-close catches them), but exit at this bar's
            # open just in case HARD_CLOSE didn't fire.
            for pos in open_positions:
                closes.append(_close_record(
                    pos, bar.o, bar.t, "force_close_session",
                    point_value=point_value, round_turn_fee=round_turn_fee,
                ))
            open_positions = []
            current_date = ct_date
            cum_tpv = 0.0
            cum_v = 0.0
            long_triggered = False
            short_triggered = False

        # Pre-session bars are skipped entirely (no VWAP cumulation, no
        # exits — the force-close at 13:00 closes anything still open).
        if ct_minute < SESSION_OPEN_MIN:
            continue

        # ── Force-close gate (runs first; exits at bar.o for the 13:00 bar).
        if ct_minute >= HARD_CLOSE_MIN:
            for pos in open_positions:
                closes.append(_close_record(
                    pos, bar.o, bar.t, "force_close_session",
                    point_value=point_value, round_turn_fee=round_turn_fee,
                ))
            open_positions = []
            continue

        # ── VWAP update — done BEFORE entry/exit so the 08:30 bar sees a
        # valid VWAP (= its own typical price).
        if bar.v > 0:
            tp = (bar.h + bar.l + bar.c) / 3.0
            cum_tpv += tp * bar.v
            cum_v += bar.v
        vwap = (cum_tpv / cum_v) if cum_v > 0 else None

        # ── Exit check for each open position. We iterate a copy because
        # we mutate open_positions on exit. Watermark update happens only
        # on the no-exit branch — once the stop fires we don't care about
        # the bar's high/low past that point.
        for pos in list(open_positions):
            result = _exit_check(pos, bar, initial_stop_pts, trail_distance_pts)
            if result is not None:
                exit_price, outcome = result
                closes.append(_close_record(
                    pos, exit_price, bar.t, outcome,
                    point_value=point_value, round_turn_fee=round_turn_fee,
                ))
                open_positions.remove(pos)
            else:
                # Update watermark for next bar's trail.
                if pos.side == "buy" and bar.h > pos.watermark:
                    pos.watermark = bar.h
                elif pos.side == "sell" and bar.l < pos.watermark:
                    pos.watermark = bar.l

        # ── Trigger evaluation. Each direction has its own one-shot flag.
        # Same-bar fires are allowed for both directions independently
        # (price could in principle hit +threshold and -threshold on the
        # same bar via a wide swing, though it's extremely rare). The
        # opposite-side trigger doesn't care whether a long/short position
        # is currently open.
        if vwap is None:
            continue

        distance = bar.c - vwap
        in_window = (ENTRY_WINDOW_START_MIN <= ct_minute < ENTRY_WINDOW_END_MIN)

        # Long-side trigger.
        if allow_longs and not long_triggered and distance >= entry_threshold_pts:
            long_triggered = True
            if in_window:
                dist_int = int(round(distance * 100))
                open_positions.append(_Position(
                    side="buy", entry_price=bar.c, entry_t=bar.t, size=1,
                    watermark=bar.c,
                    reason=f"vwap_momentum_long_d{dist_int:+07d}",
                ))

        # Short-side trigger — mirror of long.
        if allow_shorts and not short_triggered and distance <= -entry_threshold_pts:
            short_triggered = True
            if in_window:
                dist_int = int(round(distance * 100))
                open_positions.append(_Position(
                    side="sell", entry_price=bar.c, entry_t=bar.t, size=1,
                    watermark=bar.c,
                    reason=f"vwap_momentum_short_d{dist_int:+07d}",
                ))

    # Flush any still-open positions at the last bar (only happens if the
    # bar stream ends mid-session before 13:00).
    if open_positions and bars:
        last = bars[-1]
        for pos in open_positions:
            closes.append(_close_record(
                pos, last.c, last.t, "force_close_end_of_backtest",
                point_value=point_value, round_turn_fee=round_turn_fee,
            ))

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
    direction_group = p.add_mutually_exclusive_group()
    direction_group.add_argument(
        "--longs-only", action="store_true",
        help="Run with shorts disabled (mirror image of --shorts-only).",
    )
    direction_group.add_argument(
        "--shorts-only", action="store_true",
        help="Run with longs disabled. Useful for isolating direction-specific edge.",
    )
    args = p.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC) if args.start else None
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC) if args.end else None

    # Direction mode — default is symmetric. The mutually-exclusive group
    # above guarantees at most one of the two flags is set.
    allow_longs = not args.shorts_only
    allow_shorts = not args.longs_only
    if allow_longs and allow_shorts:
        mode = "long+short"
    elif allow_longs:
        mode = "long-only"
    else:
        mode = "short-only"

    print(f"Loading 2-min bars from cache (start={start}, end={end})...")
    t0 = time_mod.time()
    bars = stream_2min_bars(start=start, end=end)
    print(f"  loaded in {time_mod.time() - t0:.1f}s\n")

    if not bars:
        print("No bars in range. Exiting.")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"VWAP_MOMENTUM sweep — {mode}, initial_stop fixed at {INITIAL_STOP_PTS}pt")
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
                allow_longs=allow_longs,
                allow_shorts=allow_shorts,
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
