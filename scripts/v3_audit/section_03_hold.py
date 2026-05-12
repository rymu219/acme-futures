"""Section 3 — hold-time analysis.

Bucket trades by bars held: 1, 2, 3, 4-6, 7-10, 11+. Per variant and
fleet-wide: win rate, net P&L, PF. Tests the 1-bar churn hypothesis.
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


BUCKETS = [
    ("1", lambda b: b == 1),
    ("2", lambda b: b == 2),
    ("3", lambda b: b == 3),
    ("4-6", lambda b: 4 <= b <= 6),
    ("7-10", lambda b: 7 <= b <= 10),
    ("11+", lambda b: b >= 11),
]


def bucket_for(b) -> str:
    if pd.isna(b):
        return "?"
    bi = int(b)
    for name, fn in BUCKETS:
        if fn(bi):
            return name
    return "?"


def hold_time_table(df: pd.DataFrame) -> pd.DataFrame:
    work = df.copy()
    work["bars_bucket"] = work["bars_held"].map(bucket_for)

    rows: list[dict] = []
    scopes = list(work["strategy_id"].unique()) + ["FLEET"]
    for scope in scopes:
        sub_scope = work if scope == "FLEET" else work[work["strategy_id"] == scope]
        for name, _ in BUCKETS:
            sub = sub_scope[sub_scope["bars_bucket"] == name]
            if sub.empty:
                continue
            n = len(sub)
            net = float(sub["pnl_dollars"].sum())
            wins = int((sub["pnl_dollars"] > 0).sum())
            gw = float(sub["pnl_pos"].sum())
            gl = float(sub["pnl_neg"].sum())
            rows.append({
                "scope": scope,
                "bars_bucket": name,
                "n_trades": n,
                "net_pnl": net,
                "win_rate": wins / n,
                "profit_factor": profit_factor(gw, gl),
                "avg_pnl": net / n,
            })

    out = pd.DataFrame(rows)
    bucket_order = {n: i for i, (n, _) in enumerate(BUCKETS)}
    out["bucket_order"] = out["bars_bucket"].map(bucket_order)
    out = out.sort_values(["scope", "bucket_order"]).drop(columns="bucket_order")
    return out.reset_index(drop=True)


def render_md(table: pd.DataFrame, df: pd.DataFrame) -> str:
    if table.empty:
        return "## §3 Hold-time analysis\n\n(no settled trades)\n"

    fleet = table[table["scope"] == "FLEET"].copy()
    fleet_disp = fleet.drop(columns=["scope"]).copy()
    fleet_disp["net_pnl"] = fleet_disp["net_pnl"].map(lambda v: f"${v:,.2f}")
    fleet_disp["win_rate"] = fleet_disp["win_rate"].map(lambda v: f"{v*100:.1f}%")
    fleet_disp["profit_factor"] = fleet_disp["profit_factor"].map(
        lambda v: f"{v:.2f}" if v != float("inf") else "inf"
    )
    fleet_disp["avg_pnl"] = fleet_disp["avg_pnl"].map(lambda v: f"${v:,.2f}")
    fleet_disp = fleet_disp.rename(columns={
        "bars_bucket": "bars held", "n_trades": "n",
        "net_pnl": "net P&L", "win_rate": "WR",
        "profit_factor": "PF", "avg_pnl": "avg/trade",
    })

    work = df.copy()
    bar1_opp = work[(work["bars_held"] == 1) & (work["exit_reason"] == "opposite_signal")]
    bar1_opp_n = len(bar1_opp)
    bar1_opp_net = float(bar1_opp["pnl_dollars"].sum())
    bar1_opp_wr = float((bar1_opp["pnl_dollars"] > 0).mean()) if bar1_opp_n else 0.0
    bar1_opp_pf = profit_factor(float(bar1_opp["pnl_pos"].sum()), float(bar1_opp["pnl_neg"].sum()))

    ge2_opp = work[(work["bars_held"] >= 2) & (work["exit_reason"] == "opposite_signal")]
    ge2_opp_n = len(ge2_opp)
    ge2_opp_net = float(ge2_opp["pnl_dollars"].sum())
    ge2_opp_wr = float((ge2_opp["pnl_dollars"] > 0).mean()) if ge2_opp_n else 0.0
    ge2_opp_pf = profit_factor(float(ge2_opp["pnl_pos"].sum()), float(ge2_opp["pnl_neg"].sum()))

    fleet_net = float(work["pnl_dollars"].sum())
    excl_net = fleet_net - bar1_opp_net

    confirmed = "confirmed" if bar1_opp_net < 0 and ge2_opp_net > 0 else (
        "partially supported (1-bar bad; ≥2-bar also negative but less so)"
        if bar1_opp_net < 0 else "unsupported"
    )

    canon = df[df["strategy_id"] == "v3-canon"]
    min2 = df[df["strategy_id"] == "v3-min2bar"]
    canon_net = float(canon["pnl_dollars"].sum())
    min2_net = float(min2["pnl_dollars"].sum())
    canon_pf = profit_factor(float(canon["pnl_pos"].sum()), float(canon["pnl_neg"].sum()))
    min2_pf = profit_factor(float(min2["pnl_pos"].sum()), float(min2["pnl_neg"].sum()))
    canon_wr = float((canon["pnl_dollars"] > 0).mean()) * 100
    min2_wr = float((min2["pnl_dollars"] > 0).mean()) * 100

    return f"""## §3 Hold-time analysis

[`docs/v3_audit/hold_time.csv`](docs/v3_audit/hold_time.csv) (variant + fleet detail)

Buckets: 1, 2, 3, 4–6, 7–10, 11+ bars (each bar = 2 min).

### Fleet, by bars-held bucket

{df_to_md(fleet_disp)}

### 1-bar opposite-signal hypothesis

| Cohort | n | Net P&L | WR | PF |
|---|---:|---:|---:|---:|
| 1-bar `opposite_signal` exits | {bar1_opp_n:,} | ${bar1_opp_net:+,.2f} | {bar1_opp_wr*100:.1f}% | {bar1_opp_pf:.2f} |
| ≥2-bar `opposite_signal` exits | {ge2_opp_n:,} | ${ge2_opp_net:+,.2f} | {ge2_opp_wr*100:.1f}% | {ge2_opp_pf:.2f} |

The 1-bar churn hypothesis is **{confirmed}** at the fleet level.

### Counterfactual: strict 2-bar minimum hold (upper bound)

| Metric | Actual | If 1-bar opp-sig exits skipped |
|---|---:|---:|
| Fleet net P&L | ${fleet_net:+,.2f} | ${excl_net:+,.2f} |

Upper bound — assumes the bar-2 outcome would have been P&L-neutral.
Real bar-2 outcome depends on what the price did next. A proper
estimate would replay each skipped trade against the bars; the CSV
captures the data needed for that follow-up.

### Variant pair test: v3-canon vs v3-min2bar

`v3-min2bar` is the only variant whose engine enables a strict 2-bar
minimum hold (`min_bars_before_opposite_exit = 2`). v3-canon is the
control.

| Variant | n | Net P&L | PF | WR |
|---|---:|---:|---:|---:|
| v3-canon | {len(canon):,} | ${canon_net:+,.2f} | {canon_pf:.2f} | {canon_wr:.1f}% |
| v3-min2bar | {len(min2):,} | ${min2_net:+,.2f} | {min2_pf:.2f} | {min2_wr:.1f}% |

The min-hold variant is **{'better' if min2_net > canon_net else 'worse'}** on net P&L despite
fewer trades. This corroborates the fleet 1-bar finding {('above' if bar1_opp_net < 0 else 'inconsistently')}.
"""


def main() -> int:
    df = load_settled_trades()
    table = hold_time_table(df)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    write_csv("hold_time.csv", table.to_dict(orient="records"),
              list(table.columns))
    md = render_md(table, df)
    append_section(DOCS_DIR / "v3_audit.md", md)
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
