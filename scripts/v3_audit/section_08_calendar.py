"""Section 8 — day-of-week and calendar effects.

Group P&L by day of week (CT). 7-day audit window is too short for
week-of-month — that gets a note instead of a table.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

try:
    from scripts.v3_audit.db import AUDIT_DIR, DOCS_DIR, write_csv  # type: ignore
    from scripts.v3_audit.trades import (  # type: ignore
        append_section, df_to_md, load_settled_trades, profit_factor,
    )
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.v3_audit.db import AUDIT_DIR, DOCS_DIR, write_csv  # type: ignore  # noqa: E402
    from scripts.v3_audit.trades import (  # type: ignore  # noqa: E402
        append_section, df_to_md, load_settled_trades, profit_factor,
    )


DOW_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def by_day_of_week(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict] = []
    for dow, g in df.groupby("dow_ct"):
        n = len(g)
        net = float(g["pnl_dollars"].sum())
        wins = int((g["pnl_dollars"] > 0).sum())
        gw = float(g["pnl_pos"].sum())
        gl = float(g["pnl_neg"].sum())
        rows.append({
            "dow_idx": int(dow),
            "dow_name": DOW_NAMES[int(dow)] if 0 <= int(dow) < 7 else "?",
            "n_trades": n,
            "net_pnl": net,
            "win_rate": wins / n if n else 0.0,
            "profit_factor": profit_factor(gw, gl),
            "avg_pnl": net / n if n else 0.0,
        })
    return pd.DataFrame(rows).sort_values("dow_idx").reset_index(drop=True)


def by_dow_and_variant(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict] = []
    for (sid, dow), g in df.groupby(["strategy_id", "dow_ct"]):
        n = len(g)
        net = float(g["pnl_dollars"].sum())
        wins = int((g["pnl_dollars"] > 0).sum())
        rows.append({
            "strategy_id": sid,
            "dow_idx": int(dow),
            "dow_name": DOW_NAMES[int(dow)] if 0 <= int(dow) < 7 else "?",
            "n_trades": n,
            "net_pnl": net,
            "win_rate": wins / n,
        })
    return pd.DataFrame(rows)


def render_md(fleet_dow: pd.DataFrame, per_variant: pd.DataFrame,
              df: pd.DataFrame) -> str:
    if fleet_dow.empty:
        return "## §8 Calendar effects\n\n(no settled trades)\n"

    disp = fleet_dow.copy()
    disp["net_pnl"] = disp["net_pnl"].map(lambda v: f"${v:+,.2f}")
    disp["win_rate"] = disp["win_rate"].map(lambda v: f"{v*100:.1f}%")
    disp["profit_factor"] = disp["profit_factor"].map(
        lambda v: f"{v:.2f}" if v != float("inf") else "inf"
    )
    disp["avg_pnl"] = disp["avg_pnl"].map(lambda v: f"${v:+,.2f}")
    disp = disp.drop(columns=["dow_idx"]).rename(columns={
        "dow_name": "day", "n_trades": "n", "net_pnl": "net P&L",
        "win_rate": "WR", "profit_factor": "PF", "avg_pnl": "avg/trade",
    })

    # Days covered
    days = sorted(df["date_ct"].unique())
    days_span = f"{days[0]} → {days[-1]}" if days else "(none)"
    n_days = len(days)

    return f"""## §8 Calendar effects

[`docs/v3_audit/calendar_effects.csv`](docs/v3_audit/calendar_effects.csv)
· [`docs/v3_audit/calendar_effects_per_variant.csv`](docs/v3_audit/calendar_effects_per_variant.csv)

Trading days covered: {days_span} ({n_days} distinct calendar days).
With ~7 days of data this section reports patterns but each day-of-week
bucket has only 1-2 instances — **everything in this table is
suggestive, not significant**. Patterns identified here become real
findings only after several weeks more of paper data.

### Fleet, by day of week (CT)

{df_to_md(disp)}

### Notes

- Each day-of-week has 1-2 instances — the table cannot distinguish
  "Tuesday is bad" from "the one Tuesday in the window happened to
  contain the anchor-cluster failure."
- Week-of-month analysis requires multiple weeks of data; skipped here.
- The 2026-05-07 anchor cluster (worst day at -\$1,439.70 per §5) was a
  Thursday. If Thursdays show up systematically bad in later weeks, the
  pattern is real. Right now: insufficient sample.
- **Recommended**: re-run §8 after 4+ weeks of paper data accumulates.
  The CSV provides the per-variant × day-of-week base table for that.
"""


def main() -> int:
    df = load_settled_trades()
    fleet_dow = by_day_of_week(df)
    per_var = by_dow_and_variant(df)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    write_csv("calendar_effects.csv", fleet_dow.to_dict(orient="records"),
              list(fleet_dow.columns))
    write_csv("calendar_effects_per_variant.csv",
              per_var.to_dict(orient="records"), list(per_var.columns))
    md = render_md(fleet_dow, per_var, df)
    append_section(DOCS_DIR / "v3_audit.md", md)
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
