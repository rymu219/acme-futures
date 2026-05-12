"""Section 5 — concentration and tail dependence.

For each variant compute:
  - Net P&L with top 5 winners removed
  - Net P&L with top 10 winners removed
  - Net P&L with bottom 5 losers removed
  - Single largest day's P&L contribution
  - Largest single day P&L per variant (for the Topstep $1,500 consistency rule)

Flag any variant whose net P&L is >100% attributable to its top 5 trades.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

try:
    from scripts.v3_audit.db import AUDIT_DIR, DOCS_DIR, write_csv  # type: ignore
    from scripts.v3_audit.trades import (  # type: ignore
        append_section, df_to_md, load_settled_trades,
    )
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.v3_audit.db import AUDIT_DIR, DOCS_DIR, write_csv  # type: ignore  # noqa: E402
    from scripts.v3_audit.trades import (  # type: ignore  # noqa: E402
        append_section, df_to_md, load_settled_trades,
    )


CONSISTENCY_CAP = 1500.0  # Topstep 50K best-single-day rule


def tail_table(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict] = []
    for sid, g in df.groupby("strategy_id"):
        g = g.sort_values("pnl_dollars", ascending=False)
        n = len(g)
        net = float(g["pnl_dollars"].sum())
        # top winners removed
        top5_net = float(g.iloc[5:]["pnl_dollars"].sum()) if n > 5 else 0.0
        top10_net = float(g.iloc[10:]["pnl_dollars"].sum()) if n > 10 else 0.0
        # bottom losers removed (keep only above the 5 worst)
        bot5_net = float(g.iloc[:-5]["pnl_dollars"].sum()) if n > 5 else 0.0
        # largest single day
        daily = g.groupby("date_ct")["pnl_dollars"].sum()
        max_day_pnl = float(daily.max()) if not daily.empty else 0.0
        max_day_date = daily.idxmax() if not daily.empty else None
        min_day_pnl = float(daily.min()) if not daily.empty else 0.0
        min_day_date = daily.idxmin() if not daily.empty else None

        # Tail attribution: how much of net P&L is from top 5 trades?
        top5_sum = float(g.iloc[:5]["pnl_dollars"].sum()) if n >= 5 else net
        # "% of net from top 5" = top5_sum / net  (only meaningful when net > 0)
        if abs(net) > 0.01:
            pct_top5 = top5_sum / net * 100.0
        else:
            pct_top5 = float("nan")

        rows.append({
            "strategy_id": sid,
            "n_trades": n,
            "net_pnl": net,
            "net_pnl_minus_top5": top5_net,
            "net_pnl_minus_top10": top10_net,
            "net_pnl_minus_bottom5": bot5_net,
            "top5_pnl_sum": top5_sum,
            "pct_of_net_from_top5": pct_top5,
            "max_day_pnl": max_day_pnl,
            "max_day_date": str(max_day_date) if max_day_date else "",
            "min_day_pnl": min_day_pnl,
            "min_day_date": str(min_day_date) if min_day_date else "",
            "consistency_cap_breach_day": (
                str(max_day_date) if max_day_pnl >= CONSISTENCY_CAP and max_day_date else
                str(min_day_date) if min_day_pnl <= -CONSISTENCY_CAP and min_day_date else
                ""
            ),
            "tail_driven": (
                "yes" if (net > 0 and top5_sum > net) else
                "ambiguous" if (net < 0 and bot5_net > 0) else
                "no"
            ),
        })
    return pd.DataFrame(rows).sort_values("net_pnl", ascending=False).reset_index(drop=True)


def render_md(table: pd.DataFrame, df: pd.DataFrame) -> str:
    if table.empty:
        return "## §5 Concentration and tail dependence\n\n(no settled trades)\n"

    disp = table.copy()
    for c in ("net_pnl", "net_pnl_minus_top5", "net_pnl_minus_top10",
              "net_pnl_minus_bottom5", "top5_pnl_sum", "max_day_pnl", "min_day_pnl"):
        disp[c] = disp[c].map(lambda v: f"${v:+,.2f}")
    disp["pct_of_net_from_top5"] = disp["pct_of_net_from_top5"].map(
        lambda v: "—" if pd.isna(v) else f"{v:+.0f}%"
    )

    # Trim to interesting columns for the report; full data in CSV
    md_disp = disp[[
        "strategy_id", "n_trades", "net_pnl",
        "net_pnl_minus_top5", "net_pnl_minus_bottom5",
        "pct_of_net_from_top5", "max_day_pnl", "tail_driven",
    ]].rename(columns={
        "strategy_id": "variant",
        "n_trades": "n",
        "net_pnl": "net P&L",
        "net_pnl_minus_top5": "minus top5",
        "net_pnl_minus_bottom5": "minus bot5",
        "pct_of_net_from_top5": "top5 / net",
        "max_day_pnl": "max day",
        "tail_driven": "tail?",
    })

    # Fleet-wide consistency check
    daily_fleet = df.groupby("date_ct")["pnl_dollars"].sum().sort_index()
    max_fleet_day = float(daily_fleet.max())
    max_fleet_date = daily_fleet.idxmax()
    min_fleet_day = float(daily_fleet.min())
    min_fleet_date = daily_fleet.idxmin()
    breach_up = max_fleet_day >= CONSISTENCY_CAP
    breach_down = min_fleet_day <= -CONSISTENCY_CAP

    tail_winners = table[table["tail_driven"] == "yes"]["strategy_id"].tolist()
    tail_ambig = table[table["tail_driven"] == "ambiguous"]["strategy_id"].tolist()

    return f"""## §5 Concentration and tail dependence

[`docs/v3_audit/tail_dependence.csv`](docs/v3_audit/tail_dependence.csv)

For each variant: net P&L with extremes removed, plus largest single
trading-day P&L (the metric the Topstep 50K consistency rule cares about
— best single day must stay below ${CONSISTENCY_CAP:,.0f}).

{df_to_md(md_disp)}

**Tail-driven (positive net depends on top-5 wins):** {', '.join(tail_winners) if tail_winners else '(none)'}

**Ambiguous (negative net but removing top-5 makes it worse):** {', '.join(tail_ambig) if tail_ambig else '(none)'}

### Topstep $1,500 consistency-rule check

Best single trading-day per variant (max_day_pnl) is in the table above.
At the fleet level over the 7d window:

| Direction | Day | Net P&L |
|---|---|---:|
| Best fleet day | {max_fleet_date} | ${max_fleet_day:+,.2f} |
| Worst fleet day | {min_fleet_date} | ${min_fleet_day:+,.2f} |

| Rule | Status |
|---|---|
| Best-day-up < $1,500 (consistency rule) | **{'PASS' if not breach_up else 'BREACH'}** |
| Best-day-down > -$1,500 (informal) | **{'PASS' if not breach_down else 'BREACH'}** |

This is paper P&L for the *entire fleet running simultaneously*; the
real-money 50K rule applies to one strategy. The per-variant max_day
column is the relevant view for sizing the future fleet.
"""


def main() -> int:
    df = load_settled_trades()
    table = tail_table(df)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    write_csv("tail_dependence.csv", table.to_dict(orient="records"),
              list(table.columns))
    md = render_md(table, df)
    append_section(DOCS_DIR / "v3_audit.md", md)
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
