"""Backtest the 4 new-fleet strategies against the full cached Databento data.

Streams 1-min MES bars from the local parquet cache
(`~/.acme/backtest_cache/`), aggregates to 2-min bars (the strategies'
native timeframe), and drives each strategy through every bar
independently. Phantom positions are tracked per-strategy; stops and
targets are checked against subsequent bars' high/low.

BOUNDARY needs day-levels (PDH/PDL/ONH/ONL/ORH/ORL); we compute them
per trade-date from the same bar stream and call `set_levels()` at
each day rollover.

Each strategy is backtested in isolation (its own phantom-position
state, its own open-trades list). No cross-strategy interaction —
this is per-strategy validation, not full-fleet portfolio simulation.

Outputs:
  - per-strategy summary printed to stdout
  - `docs/backtest_new_fleet/{strategy}.csv` — one row per closed trade
  - `docs/backtest_new_fleet/summary.csv` — fleet rollup

Usage:
  uv run python scripts/backtest_new_fleet.py
  uv run python scripts/backtest_new_fleet.py --strategy ignition
  uv run python scripts/backtest_new_fleet.py --start 2026-01-01 --end 2026-04-30
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
import time as time_mod
from datetime import UTC, datetime
from pathlib import Path

# Silence structlog's dry_run_close info logs — hundreds of thousands
# of bars produce hundreds of thousands of log lines and obscure the
# actual output. Done before any acme.* imports so structlog picks it
# up at config time.
logging.basicConfig(level=logging.WARNING)
import structlog  # noqa: E402
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
)

# Repo root on sys.path so we can import acme.*
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acme.backtest.data import iter_bars  # noqa: E402
from acme.broker.base import Bar, BracketSpec  # noqa: E402
from acme.conductor.bar_aggregator import BarAggregator  # noqa: E402
from acme.conductor.dry_run import DryRunPosition, check_dry_run_exits  # noqa: E402
from acme.contracts import MES  # noqa: E402
from acme.levels import compute_day_levels, trading_date_ct  # noqa: E402
from acme.risk import TOPSTEP_50K, DailyState  # noqa: E402
from acme.strategies.base import Signal  # noqa: E402
from acme.strategies.boundary import BoundaryStrategy  # noqa: E402
from acme.strategies.ignition import IgnitionStrategy  # noqa: E402
from acme.strategies.regime import RegimeStrategy  # noqa: E402
from acme.strategies.session import SessionStrategy  # noqa: E402


OUT_DIR = Path(__file__).resolve().parents[1] / "docs" / "backtest_new_fleet"


# ───────────────────────── shared helpers ──────────────────────────


def _bracket_to_prices(entry: float, side: str, bracket: BracketSpec,
                       tick: float) -> tuple[float, float]:
    """Convert a BracketSpec (offset in ticks) → (stop_price, target_price)."""
    stop_off = bracket.stop_loss_offset_ticks * tick
    tgt_off = bracket.take_profit_offset_ticks * tick
    if side == "buy":
        return entry - stop_off, entry + tgt_off
    return entry + stop_off, entry - tgt_off


def _new_daily_state(td) -> DailyState:
    return DailyState(
        trade_date=td,
        starting_balance=50_000.0,
        peak_balance_eod=50_000.0,
        max_loss_limit=48_000.0,
        daily_loss_limit=1_000.0,
        realized_pnl=0.0,
    )


# ───────────────────────── per-strategy backtest ───────────────────


def backtest_strategy(strategy, bars_2min: list[Bar], *, name: str,
                     levels_by_date: dict | None = None) -> list[dict]:
    """Drive a strategy through every bar; track its phantom positions
    via the same machinery the conductor uses in dry-run.

    Returns a list of close-records (one dict per closed trade)."""
    closes: list[dict] = []
    open_positions: list[DryRunPosition] = []
    net_position = 0
    realized = 0.0
    current_td = None
    state = None

    # Bracketed close handling re-uses check_dry_run_exits which writes
    # via Db.log_event when db is supplied. We pass db=None; the function
    # still returns the close list, which is what we need.

    for bar in bars_2min:
        td = trading_date_ct(bar.t)
        if td != current_td:
            # New trading day — refresh state and (for BOUNDARY) levels
            state = _new_daily_state(td)
            current_td = td
            if levels_by_date is not None and hasattr(strategy, "set_levels"):
                lv = levels_by_date.get(td)
                if lv is not None:
                    strategy.set_levels(lv)

        # 1. Check exits on this bar (high/low touching stop or target).
        # IMPORTANT: check_dry_run_exits mutates open_positions in place
        # (removes closed entries). Snapshot before the call so we can
        # look up entry_bar_t and compute bars_held.
        if open_positions:
            snapshot = list(open_positions)
            delta, closed = check_dry_run_exits(
                open_positions, bar, contract_id="MES",
                point_value=MES.point_value,
                round_turn_fee=TOPSTEP_50K.round_turn_fees.get("MES", 0.0),
                db=None,
            )
            net_position += delta
            for cl in closed:
                realized += cl.net_pnl
                # Find the matching position in the pre-mutation snapshot
                matched = next(
                    (p for p in snapshot
                     if abs(p.entry_price - cl.entry_price) < 1e-9
                     and p.side == cl.side
                     and p.size == cl.size),
                    None,
                )
                if matched is not None:
                    entry_ts = matched.entry_bar_t.isoformat()
                    bars_held_min = max(
                        1,
                        int((cl.closed_at - matched.entry_bar_t).total_seconds() / 60),
                    )
                else:
                    entry_ts = ""
                    bars_held_min = 0
                closes.append({
                    "strategy": name,
                    "entry_ts": entry_ts,
                    "exit_ts": cl.closed_at.isoformat(),
                    "side": cl.side,
                    "entry_price": cl.entry_price,
                    "exit_price": cl.exit_price,
                    "net_pnl": cl.net_pnl,
                    "outcome": cl.outcome,
                    "bars_held_minutes": bars_held_min,
                })

        # 2. Call strategy on this bar
        balance = 50_000.0 + realized
        sig = strategy.on_bar(
            bar, state=state, profile=TOPSTEP_50K,
            current_position=net_position,
            current_balance_unrealized=balance,
        )
        if sig is None or sig.size == 0 or sig.bracket is None:
            continue

        # 3. Open a phantom position. Convert bracket offsets to prices.
        stop_p, tgt_p = _bracket_to_prices(
            bar.c, sig.side, sig.bracket, MES.tick_size,
        )
        pos = DryRunPosition(
            side=sig.side, size=sig.size,
            entry_price=bar.c,
            stop_price=stop_p,
            target_price=tgt_p,
            entry_bar_t=bar.t,
            reason=sig.reason or "",
            strategy=name,
        )
        open_positions.append(pos)
        delta = sig.size if sig.side == "buy" else -sig.size
        net_position += delta

    # Force-close any still-open positions at the last bar's close
    if open_positions and bars_2min:
        last = bars_2min[-1]
        for p in open_positions:
            sign = 1 if p.side == "buy" else -1
            price_pnl = sign * (last.c - p.entry_price) * MES.point_value * p.size
            net_pnl = price_pnl - TOPSTEP_50K.round_turn_fees.get("MES", 0.0) * p.size
            closes.append({
                "strategy": name,
                "entry_ts": p.entry_bar_t.isoformat(),
                "exit_ts": last.t.isoformat(),
                "side": p.side,
                "entry_price": p.entry_price,
                "exit_price": last.c,
                "net_pnl": round(net_pnl, 2),
                "outcome": "force_close_end_of_backtest",
                "bars_held_minutes": int((last.t - p.entry_bar_t).total_seconds() / 60),
            })

    return closes


# ───────────────────────── bar streaming ───────────────────────────


def stream_2min_bars(*, start: datetime | None = None,
                     end: datetime | None = None) -> list[Bar]:
    """Stream 1-min OHLCV bars from the cache and properly aggregate to
    2-min OHLCV bars.

    Earlier version of this fed only the close as a tick to BarAggregator,
    which collapsed the bar's true high/low range to the min/max of
    closes. That dramatically under-counted bracket-stop / target hits
    in the backtest (most stops were 1.5x ATR away, but the synthetic
    bar's range was tiny). Now we compose 2-min OHLCV from the
    constituent 1-min bars correctly.
    """
    bars2: list[Bar] = []
    n_in = 0
    # Current 2-min bucket
    bucket_start: datetime | None = None
    bo = bh = bl_ = bc = 0.0
    bv = 0
    for b in iter_bars(start=start, end=end):
        n_in += 1
        # Floor to 2-min boundary
        floor = b.t.replace(
            minute=(b.t.minute // 2) * 2, second=0, microsecond=0,
        )
        if bucket_start is None:
            bucket_start = floor
            bo, bh, bl_, bc = b.o, b.h, b.l, b.c
            bv = b.v
            continue
        if floor == bucket_start:
            bh = max(bh, b.h)
            bl_ = min(bl_, b.l)
            bc = b.c
            bv += b.v
            continue
        # New bucket — emit the previous one
        bars2.append(Bar(t=bucket_start, o=bo, h=bh, l=bl_, c=bc, v=bv))
        bucket_start = floor
        bo, bh, bl_, bc = b.o, b.h, b.l, b.c
        bv = b.v
    # Flush last bucket
    if bucket_start is not None:
        bars2.append(Bar(t=bucket_start, o=bo, h=bh, l=bl_, c=bc, v=bv))
    print(f"  streamed {n_in:,} 1-min bars → {len(bars2):,} 2-min bars "
          f"(avg range={sum(b.h-b.l for b in bars2)/max(len(bars2),1):.2f}pt)")
    return bars2


def compute_levels_for_all_days(bars_2min: list[Bar]) -> dict:
    """One DayLevels per trade-date, computed from the full bar stream.
    Used by BOUNDARY's set_levels()."""
    by_date: dict = {}
    if not bars_2min:
        return by_date
    # For each trade_date in the stream, compute levels from all bars
    # in [prev_day_RTH_open, current_day_OR_end].
    dates = sorted({trading_date_ct(b.t) for b in bars_2min})
    for td in dates:
        # compute_day_levels reads what it needs from the bar list
        # (filters by CT window internally)
        by_date[td] = compute_day_levels(bars_2min, td, contract="MES")
    return by_date


# ───────────────────────── stats + reporting ───────────────────────


def _strategy_stats(closes: list[dict]) -> dict:
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


# ───────────────────────── main ───────────────────────────────────


STRATEGIES = {
    "ignition": IgnitionStrategy,
    "session":  SessionStrategy,
    "regime":   RegimeStrategy,
    "boundary": BoundaryStrategy,
}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--strategy", default=None,
                   choices=list(STRATEGIES),
                   help="Backtest one strategy only (default: all four).")
    p.add_argument("--start", default=None,
                   help="ISO date (e.g. 2026-01-01); default: from cache start.")
    p.add_argument("--end", default=None,
                   help="ISO date (e.g. 2026-04-30); default: cache end.")
    args = p.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC) if args.start else None
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC) if args.end else None

    print(f"Loading bars from cache (start={start}, end={end})...")
    t0 = time_mod.time()
    bars2 = stream_2min_bars(start=start, end=end)
    print(f"  loaded in {time_mod.time() - t0:.1f}s\n")

    if not bars2:
        print("No bars in range. Exiting.")
        return 1

    # Levels only needed for BOUNDARY — compute once + share
    levels_by_date = None
    if args.strategy in (None, "boundary"):
        print("Computing day-levels for BOUNDARY...")
        t0 = time_mod.time()
        levels_by_date = compute_levels_for_all_days(bars2)
        print(f"  computed {len(levels_by_date)} day-level sets "
              f"in {time_mod.time() - t0:.1f}s\n")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary_rows = []

    to_run = [args.strategy] if args.strategy else list(STRATEGIES)
    for name in to_run:
        cls = STRATEGIES[name]
        print(f"═══ {name.upper()} ═══")
        t0 = time_mod.time()
        instance = cls()
        closes = backtest_strategy(
            instance, bars2, name=name,
            levels_by_date=levels_by_date if name == "boundary" else None,
        )
        stats = _strategy_stats(closes)
        elapsed = time_mod.time() - t0
        print(f"  ran in {elapsed:.1f}s; closes={stats['n']}")
        print(f"  net=${stats['net_pnl']:,.2f}  WR={stats['win_rate']*100:.1f}%  "
              f"PF={stats['profit_factor']:.2f}  avg=${stats['avg_pnl']:+,.2f}")
        if stats['n']:
            print(f"  max_win=${stats['max_win']:+,.2f}  "
                  f"max_loss=${stats['max_loss']:+,.2f}")
            print(f"  bar-1 cohort: n={stats['bar1_n']}  "
                  f"net=${stats['bar1_net']:+,.2f}  "
                  f"WR={stats['bar1_wr']*100:.1f}%")

        # Per-strategy CSV
        cols = ["strategy", "entry_ts", "exit_ts", "side",
                "entry_price", "exit_price", "net_pnl", "outcome",
                "bars_held_minutes"]
        _write_csv(OUT_DIR / f"{name}.csv", closes, cols)

        summary_rows.append({"strategy": name, **stats, "elapsed_s": round(elapsed, 1)})
        print()

    # Fleet summary
    cols = ["strategy", "n", "net_pnl", "win_rate", "profit_factor",
            "avg_pnl", "max_win", "max_loss",
            "bar1_n", "bar1_net", "bar1_wr", "elapsed_s"]
    _write_csv(OUT_DIR / "summary.csv", summary_rows, cols)
    print(f"\nWrote {len(summary_rows)} strategy results to {OUT_DIR}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
