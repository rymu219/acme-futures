"""CONFLUENCE parameter sweep.

Streams 1-min MES bars from the local Databento cache, aggregates to 2-min
bars, computes DayLevels (PDH/PDL/ONH/ONL) per trade-date, and drives
ConfluenceStrategy through the stream. The strategy emits entry signals
(EMA-trend + multi-level cluster touch + next-bar confirmation); this
script manages trailing-stop exits and the 15:00 CT force-close, then
writes per-config trade CSVs and the sweep-summary table.

Sweep:
  confluence_ticks ∈ {4, 6, 8, 10}
  trail_points     ∈ {4, 6, 8}
  stop_ticks       = 8  (fixed)
→ 12 combinations.

The strategy class' signal bracket encodes the initial-stop offset
(stop_ticks below zone floor for long, above zone ceiling for short).
This script reads that offset to set the initial stop price; the
trailing stop math (watermark + trail_points) is layered on top in the
exit loop. Trade-dict schema matches backtest_new_fleet's so the
breakdown script's column reads are identical.

Outputs:
  - docs/backtest_confluence/sweep.csv             sweep summary
  - docs/backtest_confluence/cf{ct}_trl{trl}.csv  per-config trade lists
  - docs/backtest_confluence/primary.csv           best-PF combo (n >= 30)

Usage:
  uv run python scripts/backtest_confluence.py
  uv run python scripts/backtest_confluence.py --start 2024-04-01
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
from acme.levels import trading_date_ct  # noqa: E402
from acme.risk import TOPSTEP_50K, DailyState  # noqa: E402
from acme.strategies.confluence import (  # noqa: E402
    ConfluenceConfig, ConfluenceStrategy,
)
from scripts.backtest_new_fleet import (  # noqa: E402
    compute_levels_for_all_days, stream_2min_bars,
)

CT = ZoneInfo("America/Chicago")

OUT_DIR = Path(__file__).resolve().parents[1] / "docs" / "backtest_confluence"

# Sweep grid — per spec.
CONFLUENCE_TICKS = [4, 6, 8, 10]
TRAIL_POINTS = [4.0, 6.0, 8.0]
STOP_TICKS = 8                       # fixed per spec

HARD_CLOSE_CT = dtime(15, 0)
HARD_CLOSE_MIN = HARD_CLOSE_CT.hour * 60 + HARD_CLOSE_CT.minute


def _ct_minute(bar: Bar) -> int:
    ct = bar.t.astimezone(CT)
    return ct.hour * 60 + ct.minute


def _new_daily_state(td) -> DailyState:
    """Same per-day state seed backtest_new_fleet uses. Today-only P&L
    semantics — peak_balance / MLL drift is suppressed across days."""
    return DailyState(
        trade_date=td,
        starting_balance=50_000.0,
        peak_balance_eod=50_000.0,
        max_loss_limit=48_000.0,
        daily_loss_limit=1_000.0,
        realized_pnl=0.0,
    )


# ───────────────────────── position state ─────────────────────────


@dataclass
class _Position:
    """Open phantom position. watermark = high (long) or low (short)."""
    side: str               # "buy" or "sell"
    entry_price: float
    entry_t: datetime
    size: int
    watermark: float
    initial_stop_price: float    # taken from the Signal's bracket
    reason: str


def _close_record(
    pos: _Position, exit_price: float, exit_t: datetime, outcome: str,
    *, point_value: float, round_turn_fee: float,
) -> dict:
    sign = 1 if pos.side == "buy" else -1
    price_pnl = sign * (exit_price - pos.entry_price) * point_value * pos.size
    net_pnl = round(price_pnl - round_turn_fee * pos.size, 2)
    bars_held_min = max(1, int((exit_t - pos.entry_t).total_seconds() / 60))
    return {
        "strategy": "confluence",
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
    pos: _Position, bar: Bar, trail_points: float,
) -> tuple[float, str] | None:
    """Sign-aware stop check. Returns (exit_price, outcome) or None."""
    if pos.side == "buy":
        trail = pos.watermark - trail_points
        eff = max(pos.initial_stop_price, trail)
        if bar.l <= eff:
            outcome = "trailing_stop" if trail > pos.initial_stop_price else "initial_stop"
            return eff, outcome
    else:
        trail = pos.watermark + trail_points
        eff = min(pos.initial_stop_price, trail)
        if bar.h >= eff:
            outcome = "trailing_stop" if trail < pos.initial_stop_price else "initial_stop"
            return eff, outcome
    return None


# ───────────────────────── core backtest ──────────────────────────


def run_confluence(
    bars: list[Bar], levels_by_date: dict,
    *,
    confluence_ticks: int,
    trail_points: float,
    stop_ticks: int,
) -> list[dict]:
    """Drive ConfluenceStrategy through the bar stream with trailing-stop
    exits + 15:00 CT force-close. Returns trade-close dicts matching the
    fleet schema.
    """
    point_value = MES.point_value
    round_turn_fee = TOPSTEP_50K.round_turn_fees.get("MES", 0.0)
    tick = MES.tick_size

    strategy = ConfluenceStrategy(config=ConfluenceConfig(
        confluence_ticks=confluence_ticks,
        trail_points=trail_points,
        stop_ticks=stop_ticks,
    ))

    closes: list[dict] = []
    open_positions: list[_Position] = []
    current_td = None
    state: DailyState | None = None

    for bar in bars:
        td = trading_date_ct(bar.t)
        if td != current_td:
            # New CT trading day. Defensive: close any leftover position
            # (the 15:00 force-flat should have caught everything but in
            # the rare case bar timestamps don't include a 15:00 CT bar
            # for a holiday-shortened day, we close at this bar's open).
            for pos in open_positions:
                closes.append(_close_record(
                    pos, bar.o, bar.t, "force_close_session",
                    point_value=point_value, round_turn_fee=round_turn_fee,
                ))
            open_positions = []
            state = _new_daily_state(td)
            current_td = td
            lv = levels_by_date.get(td)
            if lv is not None:
                strategy.set_levels(lv)

        ct_min = _ct_minute(bar)

        # ── Force-close gate (runs first; exits at bar.o for the 15:00 bar).
        if ct_min >= HARD_CLOSE_MIN:
            for pos in open_positions:
                closes.append(_close_record(
                    pos, bar.o, bar.t, "force_close_session",
                    point_value=point_value, round_turn_fee=round_turn_fee,
                ))
            open_positions = []
            # NOTE: deliberately skip strategy.on_bar here — confluence
            # doesn't act after hard_close anyway, but more importantly
            # we don't want a 15:00 bar to set a pending touch for 15:02.
            continue

        # ── Exit checks (per position). Mutates open_positions; iterate copy.
        for pos in list(open_positions):
            result = _exit_check(pos, bar, trail_points)
            if result is not None:
                exit_price, outcome = result
                closes.append(_close_record(
                    pos, exit_price, bar.t, outcome,
                    point_value=point_value, round_turn_fee=round_turn_fee,
                ))
                open_positions.remove(pos)
            else:
                if pos.side == "buy" and bar.h > pos.watermark:
                    pos.watermark = bar.h
                elif pos.side == "sell" and bar.l < pos.watermark:
                    pos.watermark = bar.l

        # ── Per-strategy phantom position state for the on_bar call.
        signed = sum(p.size if p.side == "buy" else -p.size for p in open_positions)
        sig = strategy.on_bar(
            bar, state=state, profile=TOPSTEP_50K,
            current_position=signed,
            current_balance_unrealized=50_000.0,    # constant; can_open_new is permissive
        )
        if sig is None or sig.size <= 0 or sig.bracket is None:
            continue

        # ── New entry. Convert the bracket's stop offset (ticks) into
        # the absolute initial-stop price; the trailing-stop maths layer
        # on top of that.
        stop_off = sig.bracket.stop_loss_offset_ticks * tick
        if sig.side == "buy":
            entry = bar.c
            init_stop = entry - stop_off
        else:
            entry = bar.c
            init_stop = entry + stop_off

        open_positions.append(_Position(
            side=sig.side, entry_price=entry, entry_t=bar.t, size=sig.size,
            watermark=entry,
            initial_stop_price=init_stop,
            reason=sig.reason,
        ))

    # Flush leftovers at the last bar.
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
    """Mirrors backtest_new_fleet._strategy_stats. bar-1 cohort =
    bars_held_minutes <= 2 (the bar immediately after entry)."""
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
    bar1 = [c for c in closes if c.get("bars_held_minutes")
            and c["bars_held_minutes"] <= 2]
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
                   help="ISO date; default: cache start.")
    p.add_argument("--end", default=None,
                   help="ISO date; default: cache end.")
    args = p.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC) if args.start else None
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC) if args.end else None

    print(f"Loading 2-min bars from cache (start={start}, end={end})...")
    t0 = time_mod.time()
    bars = stream_2min_bars(start=start, end=end)
    print(f"  loaded in {time_mod.time() - t0:.1f}s")

    if not bars:
        print("No bars in range. Exiting.")
        return 1

    print("Computing per-day levels (PDH/PDL/ONH/ONL)...")
    t0 = time_mod.time()
    levels_by_date = compute_levels_for_all_days(bars)
    print(f"  computed {len(levels_by_date)} day-level sets "
          f"in {time_mod.time() - t0:.1f}s\n")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"CONFLUENCE sweep — stop_ticks fixed at {STOP_TICKS}")
    print(f"{'conf_tk':>8} {'trail':>6} | {'n':>4} | {'WR':>5} | "
          f"{'PF':>5} | {'net':>10} | {'avg':>8} | bar1")
    print("-" * 84)

    sweep_rows = []
    best = None    # (sort_key, stats, closes, config_key)

    for ct in CONFLUENCE_TICKS:
        for trl in TRAIL_POINTS:
            t_combo = time_mod.time()
            closes = run_confluence(
                bars, levels_by_date,
                confluence_ticks=ct, trail_points=trl, stop_ticks=STOP_TICKS,
            )
            s = _stats(closes)
            elapsed = time_mod.time() - t_combo

            cfg_key = f"cf{ct}_trl{int(trl)}"
            sweep_rows.append({
                "confluence_ticks": ct,
                "stop_ticks": STOP_TICKS,
                "trail_points": trl,
                **s,
                "elapsed_s": round(elapsed, 1),
            })
            print(f"{ct:>8} {trl:>6.1f} | {s['n']:>4} | "
                  f"{s['win_rate']*100:>4.1f}% | "
                  f"{s['profit_factor']:>4.2f} | "
                  f"${s['net_pnl']:>+9.2f} | "
                  f"${s['avg_pnl']:>+7.2f} | "
                  f"{s['bar1_n']:>3}/${s['bar1_net']:>+8.2f}")

            _write_csv(OUT_DIR / f"{cfg_key}.csv", closes, TRADE_COLS)

            if s["n"] >= 30:
                key = (s["profit_factor"], s["net_pnl"])
                if best is None or key > best[0]:
                    best = (key, s, closes, cfg_key)

    # Sweep summary CSV
    sweep_cols = ["confluence_ticks", "stop_ticks", "trail_points",
                  "n", "net_pnl", "win_rate", "profit_factor", "avg_pnl",
                  "max_win", "max_loss",
                  "bar1_n", "bar1_net", "bar1_wr", "elapsed_s"]
    _write_csv(OUT_DIR / "sweep.csv", sweep_rows, sweep_cols)

    if best is not None:
        _, s, closes, cfg_key = best
        _write_csv(OUT_DIR / "primary.csv", closes, TRADE_COLS)
        print(f"\nBest combo: {cfg_key} → PF={s['profit_factor']:.2f}, "
              f"net=${s['net_pnl']:+,.2f}, n={s['n']}")
        print(f"Copied to {OUT_DIR / 'primary.csv'} for the breakdown script.")
    else:
        print("\nNo combo had n >= 30 trades; primary.csv not written.")

    print(f"\nWrote sweep summary + 12 per-config CSVs to {OUT_DIR}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
