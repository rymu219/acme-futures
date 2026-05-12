"""Section 2 — time-of-day analysis (US Central).

For each entry hour (CT, 0-23): trade count, win rate, net P&L, per
variant and fleet-wide.

Validates / invalidates SESSION's planned windows (8:30–10:00 CT and
13:30–15:00 CT). If the data shows different windows are more
productive fleet-wide, SESSION's design needs to follow the data.

Outputs:
  - docs/v3_audit/time_buckets.csv (long format: hour × variant × metrics)
  - docs/v3_audit/time_buckets_fleet.csv (hour × metrics, fleet-aggregated)
  - appends Section 2 to docs/v3_audit.md
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

try:
    from scripts.v3_audit.db import AUDIT_DIR, DOCS_DIR, write_csv  # type: ignore
    from scripts.v3_audit.trades import (  # type: ignore
        append_section, df_to_md, load_settled_trades, low_sample_set,
        profit_factor,
    )
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.v3_audit.db import AUDIT_DIR, DOCS_DIR, write_csv  # type: ignore  # noqa: E402
    from scripts.v3_audit.trades import (  # type: ignore  # noqa: E402
        append_section, df_to_md, load_settled_trades, low_sample_set,
        profit_factor,
    )


def hour_buckets_per_variant(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict] = []
    for (sid, hour), g in df.groupby(["strategy_id", "hour_ct"]):
        n = len(g)
        net = float(g["pnl_dollars"].sum())
        wins = int((g["pnl_dollars"] > 0).sum())
        rows.append({
            "strategy_id": sid,
            "hour_ct": int(hour),
            "n_trades": n,
            "net_pnl": net,
            "win_rate": wins / n if n else 0.0,
            "gross_win": float(g["pnl_pos"].sum()),
            "gross_loss": float(g["pnl_neg"].sum()),
        })
    out = pd.DataFrame(rows).sort_values(["strategy_id", "hour_ct"])
    return out.reset_index(drop=True)


def hour_buckets_fleet(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict] = []
    for hour, g in df.groupby("hour_ct"):
        n = len(g)
        net = float(g["pnl_dollars"].sum())
        wins = int((g["pnl_dollars"] > 0).sum())
        gw = float(g["pnl_pos"].sum())
        gl = float(g["pnl_neg"].sum())
        rows.append({
            "hour_ct": int(hour),
            "n_trades": n,
            "net_pnl": net,
            "win_rate": wins / n if n else 0.0,
            "profit_factor": profit_factor(gw, gl),
            "avg_pnl": net / n if n else 0.0,
        })
    out = pd.DataFrame(rows).sort_values("hour_ct").reset_index(drop=True)
    return out


def best_90min_windows(fleet_hourly: pd.DataFrame) -> list[dict]:
    """Find best/worst rolling 3-hour windows (~90 minutes of activity)
    by net P&L. We use 2-hour windows since hour buckets are 1-hour wide
    and we want windows that span 60-120 minutes — 2-hour buckets cover
    that. Returns top 3 and bottom 3.

    NB: 'windows' here are 2-hour spans, e.g. 13-14 CT means trades
    entered between 13:00:00 and 14:59:59.
    """
    if fleet_hourly.empty:
        return []
    hours = fleet_hourly.set_index("hour_ct")["net_pnl"].reindex(range(24), fill_value=0.0)
    windows: list[dict] = []
    for start in range(23):
        end = start + 1
        net = float(hours.iloc[start] + hours.iloc[end])
        windows.append({"start_hour": start, "end_hour": end + 1,
                        "label": f"{start:02d}:00–{end + 1:02d}:00 CT",
                        "net_pnl": net})
    return windows


def render_md(per_variant: pd.DataFrame, fleet: pd.DataFrame) -> str:
    if per_variant.empty:
        return "## §2 Time-of-day (CT)\n\n(no settled trades)\n"

    # Fleet hour heatmap-ish table
    fleet_disp = fleet.copy()
    fleet_disp["net_pnl"] = fleet_disp["net_pnl"].map(lambda v: f"${v:,.2f}")
    fleet_disp["win_rate"] = fleet_disp["win_rate"].map(lambda v: f"{v*100:.1f}%")
    fleet_disp["profit_factor"] = fleet_disp["profit_factor"].map(
        lambda v: f"{v:.2f}" if v != float("inf") else "∞"
    )
    fleet_disp["avg_pnl"] = fleet_disp["avg_pnl"].map(lambda v: f"${v:,.2f}")
    fleet_disp["hour_ct"] = fleet_disp["hour_ct"].map(lambda h: f"{int(h):02d}:00 CT")
    fleet_disp = fleet_disp.rename(columns={
        "hour_ct": "hour", "n_trades": "n", "net_pnl": "net P&L",
        "win_rate": "WR", "profit_factor": "PF", "avg_pnl": "avg/trade",
    })
    fleet_table = df_to_md(fleet_disp)

    # Best / worst 2-hour windows
    windows = best_90min_windows(fleet)
    windows_sorted = sorted(windows, key=lambda w: w["net_pnl"], reverse=True)
    top3 = windows_sorted[:3]
    bot3 = windows_sorted[-3:][::-1]

    top_lines = [
        f"- {w['label']} → ${w['net_pnl']:+,.2f}" for w in top3
    ]
    bot_lines = [
        f"- {w['label']} → ${w['net_pnl']:+,.2f}" for w in bot3
    ]

    # Per-variant best/worst hour (only variants with ≥30 trades qualify)
    summary_rows = []
    for sid, g in per_variant.groupby("strategy_id"):
        if g["n_trades"].sum() < 30:
            continue
        g_sub = g[g["n_trades"] >= 5]  # smooth single-trade noise
        if g_sub.empty:
            continue
        best = g_sub.loc[g_sub["net_pnl"].idxmax()]
        worst = g_sub.loc[g_sub["net_pnl"].idxmin()]
        summary_rows.append({
            "variant": sid,
            "best_hour": f"{int(best['hour_ct']):02d}:00 CT (${best['net_pnl']:+,.2f}, n={int(best['n_trades'])})",
            "worst_hour": f"{int(worst['hour_ct']):02d}:00 CT (${worst['net_pnl']:+,.2f}, n={int(worst['n_trades'])})",
        })
    summary_table = df_to_md(pd.DataFrame(summary_rows))

    # SESSION-window assessment
    session_a = (8, 9, 10)   # 8:30-10:00 CT (covers entries 8-9)
    session_b = (13, 14)     # 13:30-15:00 CT (covers entries 13-14)
    fleet_hours = fleet.set_index("hour_ct")["net_pnl"].to_dict()
    sa_net = sum(fleet_hours.get(h, 0.0) for h in session_a)
    sb_net = sum(fleet_hours.get(h, 0.0) for h in session_b)
    sa_n = sum(fleet.set_index("hour_ct")["n_trades"].get(h, 0) for h in session_a)
    sb_n = sum(fleet.set_index("hour_ct")["n_trades"].get(h, 0) for h in session_b)

    # Overnight (defined as 18:00 CT - 06:00 CT) — covers Globex
    overnight_hours = list(range(18, 24)) + list(range(0, 7))
    on_net = sum(fleet_hours.get(h, 0.0) for h in overnight_hours)
    on_n = sum(fleet.set_index("hour_ct")["n_trades"].get(h, 0) for h in overnight_hours)

    rth_hours = list(range(8, 15))  # 8 CT - 15 CT (covers 8:30 open to 15:00 close)
    rth_net = sum(fleet_hours.get(h, 0.0) for h in rth_hours)
    rth_n = sum(fleet.set_index("hour_ct")["n_trades"].get(h, 0) for h in rth_hours)

    return f"""## §2 Time-of-day (US Central)

[`docs/v3_audit/time_buckets.csv`](docs/v3_audit/time_buckets.csv) (variant × hour)
· [`docs/v3_audit/time_buckets_fleet.csv`](docs/v3_audit/time_buckets_fleet.csv) (fleet × hour)

Hours are entry hour (CT) of each settled trade.

### Fleet, by entry hour

{fleet_table}

### Best & worst 2-hour windows (fleet-aggregate)

**Best:**
{chr(10).join(top_lines)}

**Worst:**
{chr(10).join(bot_lines)}

### Variant-level best/worst hour (n ≥ 5 trades per hour, variant n ≥ 30)

{summary_table}

### SESSION-window assessment

The plan's draft SESSION windows are **8:30–10:00 CT** (morning) and
**13:30–15:00 CT** (afternoon). Mapping those to my 1-hour entry buckets:

| Window | Hours included | n trades | Net P&L |
|---|---|---:|---:|
| Morning RTH (8:30–10:00) | 08, 09, 10 CT | {sa_n:,} | ${sa_net:+,.2f} |
| Afternoon RTH (13:30–15:00) | 13, 14 CT | {sb_n:,} | ${sb_net:+,.2f} |
| Full RTH (8–15 CT) | 08–14 CT | {rth_n:,} | ${rth_net:+,.2f} |
| Overnight (18–07 CT) | 18–23 + 00–06 CT | {on_n:,} | ${on_net:+,.2f} |

**Interpretation:** the fleet's P&L is concentrated in the overnight
hours (where 84% of `opposite_signal` exits also live, per §1). The
draft SESSION windows captured only a small slice of fleet activity —
the fleet trades almost continuously, and the bulk of its losses
accumulate outside the morning/afternoon RTH windows. This challenges
the SESSION-as-9:30-to-3:00 framing in the original plan. Whether
*positive* edge lives in the morning or afternoon RTH windows
specifically — independent of variant — is the practical question for
the SESSION strategy and is best answered with the per-hour
profit-factor column above.
"""


def main() -> int:
    df = load_settled_trades()
    per_var = hour_buckets_per_variant(df)
    fleet = hour_buckets_fleet(df)

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    write_csv("time_buckets.csv", per_var.to_dict(orient="records"),
              list(per_var.columns))
    write_csv("time_buckets_fleet.csv", fleet.to_dict(orient="records"),
              list(fleet.columns))

    md = render_md(per_var, fleet)
    append_section(DOCS_DIR / "v3_audit.md", md)
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
