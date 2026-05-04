"""Paper-week promotion gate evaluator.

Per ryanspec-paper-wiring-brief.md Step 7. Reads paper trades from Supabase,
returns a verdict: PROMOTE_LIVE / EXTEND_PAPER / HALT / INVESTIGATE. Decision
is automated but the user reviews before flipping the mode flag — caller is
responsible for the actual mode change.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Literal

import pandas as pd

from acme.db import Db

Verdict = Literal["PROMOTE_LIVE", "EXTEND_PAPER", "HALT", "INVESTIGATE"]

# Hard gates from the brief
MIN_SETTLED_TRADES = 200
MIN_PF_FLOOR = 1.5            # OOS was 2.30 — allow ~35% degradation
MAX_AVG_SLIPPAGE_TICKS = 1.5  # OOS modeled at 1 tick
MIN_OPPOSITE_EXIT_PCT = 0.40  # OOS distribution had ~53% opposite_signal


@dataclass(frozen=True)
class PromotionEvaluation:
    verdict: Verdict
    reason: str
    settled: int
    pf: float | None
    win_rate_pct: float | None
    avg_slippage_ticks: float | None
    exit_distribution: dict[str, float]
    monthly_pf: dict[str, float] | None = None


def evaluate_paper_promotion(trades: pd.DataFrame) -> PromotionEvaluation:
    """Gate logic. `trades` should be all paper trades for the eval window
    (typically the most recent 5 trading days)."""
    settled = trades[trades["exit_reason"].notna()].copy()
    n = len(settled)
    if n < MIN_SETTLED_TRADES:
        return PromotionEvaluation(
            verdict="EXTEND_PAPER",
            reason=f"Only {n} settled trades (need >= {MIN_SETTLED_TRADES})",
            settled=n, pf=None, win_rate_pct=None,
            avg_slippage_ticks=None, exit_distribution={},
        )

    wins = (settled["pnl_dollars"] > 0).sum()
    wr = wins / n
    gross_win = settled.loc[settled["pnl_dollars"] > 0, "pnl_dollars"].sum()
    gross_loss = abs(settled.loc[settled["pnl_dollars"] < 0, "pnl_dollars"].sum())
    pf = gross_win / max(gross_loss, 0.01)

    avg_slip = (
        float(settled["slippage_ticks"].mean())
        if "slippage_ticks" in settled.columns
        and settled["slippage_ticks"].notna().any()
        else None
    )

    exit_dist = (
        settled["exit_reason"].value_counts(normalize=True).to_dict()
    )

    # Monthly stability check
    monthly_pf: dict[str, float] = {}
    if "bar_ts" in settled.columns:
        s = settled.copy()
        s["month"] = pd.to_datetime(s["bar_ts"]).dt.to_period("M")
        for m, g in s.groupby("month"):
            gw = g.loc[g["pnl_dollars"] > 0, "pnl_dollars"].sum()
            gl = abs(g.loc[g["pnl_dollars"] < 0, "pnl_dollars"].sum())
            monthly_pf[str(m)] = float(gw / max(gl, 0.01))

    # Hard gates
    if pf < MIN_PF_FLOOR:
        return PromotionEvaluation(
            verdict="HALT",
            reason=f"Paper PF {pf:.2f} < {MIN_PF_FLOOR} floor",
            settled=n, pf=float(pf), win_rate_pct=float(wr * 100),
            avg_slippage_ticks=avg_slip, exit_distribution=exit_dist,
            monthly_pf=monthly_pf,
        )

    if avg_slip is not None and avg_slip > MAX_AVG_SLIPPAGE_TICKS:
        return PromotionEvaluation(
            verdict="HALT",
            reason=f"Slippage {avg_slip:.2f} ticks > {MAX_AVG_SLIPPAGE_TICKS} ticks (modeled 1.0)",
            settled=n, pf=float(pf), win_rate_pct=float(wr * 100),
            avg_slippage_ticks=avg_slip, exit_distribution=exit_dist,
            monthly_pf=monthly_pf,
        )

    opposite_pct = exit_dist.get("opposite_signal", 0.0)
    if opposite_pct < MIN_OPPOSITE_EXIT_PCT:
        return PromotionEvaluation(
            verdict="INVESTIGATE",
            reason=(f"Opposite-signal exits {opposite_pct*100:.0f}% "
                    f"vs ~53% expected (OOS distribution shift)"),
            settled=n, pf=float(pf), win_rate_pct=float(wr * 100),
            avg_slippage_ticks=avg_slip, exit_distribution=exit_dist,
            monthly_pf=monthly_pf,
        )

    return PromotionEvaluation(
        verdict="PROMOTE_LIVE",
        reason=(f"PF {pf:.2f}, slippage "
                f"{avg_slip if avg_slip is not None else '—'} ticks, "
                f"opposite-exit {opposite_pct*100:.0f}% — all gates pass"),
        settled=n, pf=float(pf), win_rate_pct=float(wr * 100),
        avg_slippage_ticks=avg_slip, exit_distribution=exit_dist,
        monthly_pf=monthly_pf,
    )


def main() -> None:
    p = argparse.ArgumentParser(
        description="Ryan-Spec v3 paper-week promotion gate"
    )
    p.add_argument("--mode", default="paper")
    p.add_argument("--since", default=None,
                   help="ISO date floor (e.g. 2026-05-04T00:00:00)")
    args = p.parse_args()
    db = Db()
    rows = db.select_ryan_spec_v3_trades(mode=args.mode, since=args.since,
                                          limit=10_000)
    if not rows:
        print(f"No {args.mode} trades found.")
        return
    df = pd.DataFrame(rows)
    eval_ = evaluate_paper_promotion(df)
    print("\n=== Ryan-Spec v3 — Paper Promotion Gate ===")
    print(f"Verdict:               {eval_.verdict}")
    print(f"Reason:                {eval_.reason}")
    print(f"Settled trades:        {eval_.settled}")
    if eval_.pf is not None:
        print(f"Profit factor:         {eval_.pf:.2f}")
    if eval_.win_rate_pct is not None:
        print(f"Win rate:              {eval_.win_rate_pct:.1f}%")
    if eval_.avg_slippage_ticks is not None:
        print(f"Avg slippage:          {eval_.avg_slippage_ticks:.2f} ticks")
    print("\nExit distribution:")
    for k, v in sorted(eval_.exit_distribution.items(),
                       key=lambda kv: -kv[1]):
        print(f"  {k:<22} {v*100:5.1f}%")
    if eval_.monthly_pf:
        print("\nMonthly PF:")
        for m, pf_ in sorted(eval_.monthly_pf.items()):
            print(f"  {m}  PF={pf_:.2f}")


if __name__ == "__main__":
    main()
