"""Section 1 — per-variant performance.

For each variant (settled paper trades only): trade count, net P&L,
gross win, gross loss, profit factor, win rate, avg win, avg loss,
ratio, largest single win/loss, max drawdown ($ and % of peak),
annualized Sharpe on daily returns, average bars held, mean MFE/MAE,
exit-reason distribution.

Outputs:
  - docs/v3_audit/variant_summary.csv
  - docs/v3_audit/variant_exit_distribution.csv (long format)
  - appends Section 1 to docs/v3_audit.md

Usage:
  uv run python scripts/v3_audit/section_01_variant.py
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

try:
    from scripts.v3_audit.db import AUDIT_DIR, DOCS_DIR, write_csv  # type: ignore
    from scripts.v3_audit.trades import (  # type: ignore
        append_section,
        df_to_md,
        load_settled_trades,
        low_sample_set,
        max_drawdown,
        profit_factor,
        sharpe_daily,
    )
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.v3_audit.db import AUDIT_DIR, DOCS_DIR, write_csv  # type: ignore  # noqa: E402
    from scripts.v3_audit.trades import (  # type: ignore  # noqa: E402
        append_section,
        df_to_md,
        load_settled_trades,
        low_sample_set,
        max_drawdown,
        profit_factor,
        sharpe_daily,
    )


def compute_variant_summary(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    low = low_sample_set(df)
    rows = []
    for sid, g in df.groupby("strategy_id"):
        g = g.sort_values("entry_ts").reset_index(drop=True)
        n = len(g)
        net = float(g["pnl_dollars"].sum())
        gw = float(g["pnl_pos"].sum())
        gl = float(g["pnl_neg"].sum())
        pf = profit_factor(gw, gl)
        wins_g = g[g["pnl_dollars"] > 0]
        losses_g = g[g["pnl_dollars"] < 0]
        wr = len(wins_g) / n if n else 0.0
        avg_w = float(wins_g["pnl_dollars"].mean()) if len(wins_g) else 0.0
        avg_l = float(losses_g["pnl_dollars"].mean()) if len(losses_g) else 0.0
        ratio = abs(avg_w / avg_l) if avg_l != 0 else float("inf")
        max_win = float(g["pnl_dollars"].max())
        max_loss = float(g["pnl_dollars"].min())
        equity = g["pnl_dollars"].cumsum().tolist()
        mdd, mdd_pct, _, _ = max_drawdown(equity)
        # Daily P&L → annualized Sharpe
        daily = g.groupby("date_ct")["pnl_dollars"].sum()
        sh = sharpe_daily(daily)
        avg_bars = float(g["bars_held"].astype(float).mean())
        mean_mfe = float(g["mfe_atr"].mean()) if "mfe_atr" in g.columns else float("nan")
        mean_mae = float(g["mae_atr"].mean()) if "mae_atr" in g.columns else float("nan")

        rows.append({
            "strategy_id": sid,
            "low_sample": sid in low,
            "n_trades": n,
            "net_pnl": net,
            "gross_win": gw,
            "gross_loss": gl,
            "profit_factor": pf if pf != float("inf") else 9.99,
            "win_rate": wr,
            "avg_win": avg_w,
            "avg_loss": avg_l,
            "win_loss_ratio": ratio if ratio != float("inf") else 9.99,
            "max_win": max_win,
            "max_loss": max_loss,
            "max_drawdown_dollars": mdd,
            "max_drawdown_pct_of_peak": mdd_pct,
            "sharpe_daily_annualized": sh,
            "avg_bars_held": avg_bars,
            "mean_mfe_atr": mean_mfe,
            "mean_mae_atr": mean_mae,
        })

    out = pd.DataFrame(rows).sort_values("net_pnl", ascending=False).reset_index(drop=True)
    return out


def compute_exit_distribution(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    counts = (
        df.groupby(["strategy_id", "exit_reason"])
        .size()
        .reset_index(name="n")
    )
    totals = df.groupby("strategy_id").size().rename("total").reset_index()
    out = counts.merge(totals, on="strategy_id")
    out["pct_of_trades"] = out["n"] / out["total"]
    out = out.sort_values(["strategy_id", "n"], ascending=[True, False])
    return out[["strategy_id", "exit_reason", "n", "pct_of_trades"]]


def render_md(summary: pd.DataFrame, exits: pd.DataFrame) -> str:
    if summary.empty:
        return "## §1 Per-variant performance\n\n(no settled trades)\n"

    # Render summary table — round columns for readability
    summary_disp = summary.copy()
    summary_disp["strategy_id"] = summary_disp.apply(
        lambda r: f"{r['strategy_id']} [low_sample]" if r["low_sample"]
        else r["strategy_id"], axis=1,
    )
    summary_disp = summary_disp.drop(columns=["low_sample"])
    money_cols = [
        "net_pnl", "gross_win", "gross_loss", "avg_win", "avg_loss",
        "max_win", "max_loss", "max_drawdown_dollars",
    ]
    for c in money_cols:
        summary_disp[c] = summary_disp[c].map(lambda v: f"${v:,.2f}")
    pct_cols = ["win_rate", "max_drawdown_pct_of_peak"]
    for c in pct_cols:
        summary_disp[c] = summary_disp[c].map(lambda v, c=c: f"{v*100:.1f}%" if c == "win_rate" else f"{v:.1f}%")
    for c in ("profit_factor", "win_loss_ratio", "sharpe_daily_annualized",
              "avg_bars_held", "mean_mfe_atr", "mean_mae_atr"):
        summary_disp[c] = summary_disp[c].map(lambda v: f"{v:.2f}" if pd.notna(v) else "")

    table = df_to_md(summary_disp)

    # Brief commentary
    net_winners = summary[summary["net_pnl"] > 0]["strategy_id"].tolist()
    net_losers = summary[summary["net_pnl"] <= 0]["strategy_id"].tolist()
    fleet_net = summary["net_pnl"].sum()
    fleet_pf = profit_factor(
        summary["gross_win"].sum(), summary["gross_loss"].sum()
    )
    pf_max = summary.loc[summary["profit_factor"].idxmax()]
    pf_min = summary.loc[summary["profit_factor"].idxmin()]

    # Top exit reasons fleet-wide
    fleet_exit = (
        exits.groupby("exit_reason")["n"].sum()
        .sort_values(ascending=False)
    )
    fleet_total = int(fleet_exit.sum())
    exit_lines = [
        f"- `{er}` — {n:,} ({n/fleet_total*100:.1f}%)"
        for er, n in fleet_exit.items()
    ]

    return f"""## §1 Per-variant performance

[`docs/v3_audit/variant_summary.csv`](docs/v3_audit/variant_summary.csv)
· [`docs/v3_audit/variant_exit_distribution.csv`](docs/v3_audit/variant_exit_distribution.csv)

Universe: settled paper trades only (open / orphaned rows excluded).
{len(summary)} variants, {int(summary['n_trades'].sum()):,} settled trades,
fleet net P&L **${fleet_net:,.2f}**, fleet PF **{fleet_pf:.2f}**.

{table}

**Net winners (7d):** {', '.join(net_winners) if net_winners else '(none)'}
**Net losers:** {', '.join(net_losers) if net_losers else '(none)'}

**Best PF:** `{pf_max['strategy_id']}` at {pf_max['profit_factor']:.2f}
({int(pf_max['n_trades'])} trades).
**Worst PF:** `{pf_min['strategy_id']}` at {pf_min['profit_factor']:.2f}
({int(pf_min['n_trades'])} trades).

**Fleet exit-reason distribution (settled only):**

{chr(10).join(exit_lines)}

`opposite_signal` dominates at >80% across nearly every variant — this is
the system's defining behavior. Whether that's a feature or a bug is
the central §3 question (1-bar churn) and §6 question (correlated
exits causing correlated whipsaws).
"""


def main() -> int:
    df = load_settled_trades()
    summary = compute_variant_summary(df)
    exits = compute_exit_distribution(df)

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    write_csv(
        "variant_summary.csv",
        summary.to_dict(orient="records"),
        list(summary.columns),
    )
    write_csv(
        "variant_exit_distribution.csv",
        exits.to_dict(orient="records"),
        list(exits.columns),
    )

    md = render_md(summary, exits)
    append_section(DOCS_DIR / "v3_audit.md", md)

    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
