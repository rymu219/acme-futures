"""Backtest CLI for the v3 / v3.1 / v4 / v5 fleet.

Runs all 16 variants from `acme.runner.VARIANTS` against cached MES bars
and produces a per-variant + family stats summary.

Usage:
    uv run python -m acme.backtest.v3_run                    # full window
    uv run python -m acme.backtest.v3_run --since 2025-04-01 # narrower
    uv run python -m acme.backtest.v3_run --variant v3-canon # single variant

Output:
    docs/backtest-<timestamp>.md  — markdown stats table + per-family rollup
    /tmp/acme-backtest-<timestamp>-trades.csv  — per-trade ledger for analysis

The cum_delta synthesis uses bar-shape-vs-volume (see v3_replay.py). It's
an approximation of the live runner's quote-tick-driven cum_delta. The
backtest is good for relative comparisons across variants — not absolute
calibration of cum_delta thresholds.
"""
from __future__ import annotations

import argparse
import csv
import time
from datetime import UTC, datetime
from pathlib import Path

from acme.backtest.data import iter_bars
from acme.backtest.v3_replay import BacktestTrade, replay
from acme.runner import VARIANTS


def _parse_iso(s: str | None) -> datetime | None:
    if s is None:
        return None
    if "T" in s:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    return datetime.fromisoformat(s + "T00:00:00+00:00").replace(tzinfo=UTC)


def _stats(trades: list[BacktestTrade]) -> dict:
    if not trades:
        return {
            "n": 0, "n_long": 0, "n_short": 0, "wr": 0.0, "pf": None,
            "net_pnl": 0.0, "avg_pnl": 0.0, "best": 0.0, "worst": 0.0,
            "max_dd": 0.0, "exit_mix": {},
        }
    pnls = [t.pnl_dollars for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gw = sum(wins)
    gl = -sum(losses)
    pf = (gw / gl) if gl > 0 else None
    # Max drawdown over the trade-by-trade equity curve
    cum, peak, mdd = 0.0, 0.0, 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    # Exit reason mix
    from collections import Counter
    exit_counts = Counter(t.exit_reason for t in trades)
    exit_mix = {k: v / len(trades) for k, v in exit_counts.items()}
    return {
        "n": len(trades),
        "n_long": sum(1 for t in trades if t.direction == "long"),
        "n_short": sum(1 for t in trades if t.direction == "short"),
        "wr": len(wins) / len(trades) * 100,
        "pf": pf,
        "net_pnl": sum(pnls),
        "avg_pnl": sum(pnls) / len(pnls),
        "best": max(pnls),
        "worst": min(pnls),
        "max_dd": mdd,
        "exit_mix": exit_mix,
    }


def _family_of(sid: str) -> str:
    """Map strategy_id to its family bucket: v3 / v3.1 / v4 / v5."""
    if sid.startswith("v3.1-"):
        return "v3.1"
    if sid.startswith("v3-"):
        return "v3"
    if sid.startswith("v4-"):
        return "v4"
    if sid.startswith("v5-"):
        return "v5"
    return "other"


def _render_markdown(
    stats_by_sid: dict[str, dict],
    *,
    bars_2m: int,
    elapsed_sec: float,
    since: datetime | None,
    until: datetime | None,
    variant_order: list[str],
) -> str:
    """Build a markdown report with per-variant table + per-family rollup."""
    lines: list[str] = []
    lines.append(f"# Backtest report — generated {datetime.now(UTC).isoformat()}")
    lines.append("")
    lines.append(f"- Window: {since or 'beginning'} → {until or 'end'}")
    lines.append(f"- 2-min bars processed: {bars_2m:,}")
    lines.append(f"- Elapsed: {elapsed_sec:.1f}s")
    lines.append(f"- Variants: {len(stats_by_sid)}")
    lines.append("")
    lines.append("## Per-variant summary")
    lines.append("")
    lines.append(f"| {'variant':18} | {'n':>5} | {'WR':>5} | {'PF':>5} | "
                 f"{'net':>10} | {'avg':>7} | {'best':>8} | {'worst':>8} | "
                 f"{'MDD':>10} |")
    lines.append("|" + "---|" * 9)
    for sid in variant_order:
        s = stats_by_sid.get(sid)
        if s is None or s["n"] == 0:
            lines.append(f"| {sid:18} | {'—':>5} | {'—':>5} | {'—':>5} | "
                         f"{'—':>10} | {'—':>7} | {'—':>8} | {'—':>8} | {'—':>10} |")
            continue
        pf_str = f"{s['pf']:.2f}" if s['pf'] is not None else "—"
        lines.append(
            f"| {sid:18} | {s['n']:>5} | {s['wr']:>4.0f}% | {pf_str:>5} | "
            f"${s['net_pnl']:>9.0f} | ${s['avg_pnl']:>6.2f} | "
            f"${s['best']:>7.0f} | ${s['worst']:>7.0f} | ${s['max_dd']:>9.0f} |"
        )

    # Per-family rollup
    families: dict[str, dict] = {}
    for sid, s in stats_by_sid.items():
        f = _family_of(sid)
        agg = families.setdefault(f, {"n": 0, "net_pnl": 0.0, "wins": 0, "losses": 0})
        agg["n"] += s["n"]
        agg["net_pnl"] += s["net_pnl"]
        # Approx wins/losses for family WR
        agg["wins"] += int(s["n"] * s["wr"] / 100)
    lines.append("")
    lines.append("## Per-family rollup")
    lines.append("")
    lines.append(f"| {'family':6} | {'n_trades':>9} | {'net':>10} | {'WR':>5} |")
    lines.append("|" + "---|" * 4)
    for fam in ("v3", "v3.1", "v4", "v5"):
        agg = families.get(fam)
        if not agg or agg["n"] == 0:
            lines.append(f"| {fam:6} | {'—':>9} | {'—':>10} | {'—':>5} |")
            continue
        wr = agg["wins"] / agg["n"] * 100 if agg["n"] else 0
        lines.append(f"| {fam:6} | {agg['n']:>9} | ${agg['net_pnl']:>9.0f} | {wr:>4.0f}% |")

    # Best / worst variant call-outs
    nonempty = {k: v for k, v in stats_by_sid.items() if v["n"] > 0}
    if nonempty:
        best = max(nonempty.items(), key=lambda kv: kv[1]["net_pnl"])
        worst = min(nonempty.items(), key=lambda kv: kv[1]["net_pnl"])
        best_pf = f"{best[1]['pf']:.2f}" if best[1]["pf"] is not None else "—"
        lines.append("")
        lines.append("## Headline")
        lines.append("")
        lines.append(f"- **Best variant:** `{best[0]}` net ${best[1]['net_pnl']:.0f}, "
                     f"PF {best_pf}")
        lines.append(f"- **Worst variant:** `{worst[0]}` net ${worst[1]['net_pnl']:.0f}")

    lines.append("")
    lines.append("## Caveats")
    lines.append("")
    lines.append(
        "- `cum_delta_session` is synthesized from each bar's close-position-in-range "
        "scaled by volume. The live runner uses quote-tick reconstruction; this "
        "is a proxy. Cross-variant *comparisons* are reliable; absolute thresholds "
        "(e.g. the static -670) may not transfer cleanly between live and backtest."
    )
    lines.append(
        "- 2-min bars are aggregated from cached 1-min Databento bars. Aligned to "
        "even minutes, partial trailing groups dropped."
    )
    lines.append(
        "- Slippage is **not** modeled. Entry / exit fills assume bar close; stops "
        "fill at the stop level. Real fills will be worse — subtract ~1 tick / leg "
        "when interpreting."
    )
    return "\n".join(lines) + "\n"


def _write_trade_csv(stats_by_sid: dict[str, list[BacktestTrade]], path: Path) -> None:
    """Per-trade CSV ledger for downstream analysis (win-loss anatomy, etc.)."""
    fields = ["strategy_id", "direction", "entry_t", "exit_t",
              "entry_price", "exit_price", "exit_reason", "pnl_dollars",
              "bars_held", "atr_at_entry", "cum_delta_at_entry",
              "mfe_atr", "mae_atr"]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for trades in stats_by_sid.values():
            for t in trades:
                w.writerow({k: getattr(t, k) for k in fields})


def main() -> None:
    p = argparse.ArgumentParser(description="v3+ fleet backtest")
    p.add_argument("--since", default=None, help="ISO date, e.g. 2025-04-01")
    p.add_argument("--until", default=None, help="ISO date, e.g. 2026-04-30")
    p.add_argument("--variant", default=None,
                   help="Single variant strategy_id (default: all)")
    p.add_argument("--report-out", default=None,
                   help="Write markdown report here (default: docs/backtest-<ts>.md)")
    args = p.parse_args()

    since = _parse_iso(args.since)
    until = _parse_iso(args.until)

    selected = list(VARIANTS)
    if args.variant:
        selected = [v for v in VARIANTS if v.strategy_id == args.variant]
        if not selected:
            raise SystemExit(f"Unknown variant {args.variant!r}")

    print(f"Backtesting {len(selected)} variant(s) on MES "
          f"({args.since or 'beginning'} → {args.until or 'end'})")

    t0 = time.time()
    bars = iter_bars(start=since, end=until)
    result = replay(bars, selected)
    elapsed = time.time() - t0

    stats_by_sid = {sid: _stats(trades) for sid, trades in result.trades.items()}

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    report_path = Path(args.report_out) if args.report_out \
        else Path("docs") / f"backtest-{ts}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    variant_order = [v.strategy_id for v in selected]
    md = _render_markdown(
        stats_by_sid, bars_2m=result.bars_2m, elapsed_sec=elapsed,
        since=since, until=until, variant_order=variant_order,
    )
    report_path.write_text(md)
    print(f"\nReport: {report_path}")

    csv_path = Path("/tmp") / f"acme-backtest-{ts}-trades.csv"
    _write_trade_csv(result.trades, csv_path)
    print(f"Trades:  {csv_path}")
    print(f"Elapsed: {elapsed:.1f}s for {result.bars_2m:,} 2-min bars\n")

    # Quick stdout summary
    print(f"{'variant':20} {'n':>5} {'WR':>5} {'PF':>5} {'net':>10}")
    for sid in variant_order:
        s = stats_by_sid.get(sid, {"n": 0, "wr": 0, "pf": None, "net_pnl": 0})
        if s["n"] == 0:
            print(f"  {sid:18} {'(no trades)':>30}")
            continue
        pf_str = f"{s['pf']:.2f}" if s["pf"] is not None else "—"
        print(f"  {sid:18} {s['n']:>5} {s['wr']:>4.0f}% {pf_str:>5} "
              f"${s['net_pnl']:>9.0f}")


if __name__ == "__main__":
    main()
