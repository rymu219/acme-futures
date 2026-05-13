"""Section 4 — direction and instrument.

MES-only, so instrument is fixed. For each variant, split P&L by long vs
short. Surfaces variants with meaningful directional skew.

Specifically calls out v4-loose-shorts (asymmetric pctile) and
v4-trend-flip (the only shorting variant in recent activity).
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
        profit_factor,
    )
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.v3_audit.db import AUDIT_DIR, DOCS_DIR, write_csv  # type: ignore  # noqa: E402
    from scripts.v3_audit.trades import (  # type: ignore  # noqa: E402
        append_section,
        df_to_md,
        load_settled_trades,
        profit_factor,
    )


def direction_split(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict] = []
    for sid, g in df.groupby("strategy_id"):
        for direction in ("long", "short"):
            sub = g[g["direction"] == direction]
            n = len(sub)
            net = float(sub["pnl_dollars"].sum())
            wins = int((sub["pnl_dollars"] > 0).sum())
            gw = float(sub["pnl_pos"].sum())
            gl = float(sub["pnl_neg"].sum())
            rows.append({
                "strategy_id": sid,
                "direction": direction,
                "n_trades": n,
                "net_pnl": net,
                "win_rate": (wins / n) if n else 0.0,
                "profit_factor": profit_factor(gw, gl) if n else 0.0,
                "avg_pnl": (net / n) if n else 0.0,
            })
    return pd.DataFrame(rows)


def render_md(splits: pd.DataFrame, df: pd.DataFrame) -> str:
    if splits.empty:
        return "## §4 Direction and instrument\n\n(no settled trades)\n"

    # Per-variant skew metric: long_net - short_net
    pivot = splits.pivot(index="strategy_id", columns="direction",
                         values=["n_trades", "net_pnl"]).fillna(0)
    pivot.columns = [f"{a}_{b}" for a, b in pivot.columns]
    pivot = pivot.reset_index()
    pivot["total_n"] = pivot["n_trades_long"] + pivot["n_trades_short"]
    pivot["pct_short"] = pivot.apply(
        lambda r: r["n_trades_short"] / r["total_n"] * 100 if r["total_n"] else 0,
        axis=1,
    )
    pivot["long_net"] = pivot["net_pnl_long"]
    pivot["short_net"] = pivot["net_pnl_short"]
    pivot = pivot.sort_values("pct_short", ascending=False)

    disp = pivot[[
        "strategy_id", "n_trades_long", "long_net",
        "n_trades_short", "short_net", "pct_short",
    ]].copy()
    disp["n_trades_long"] = disp["n_trades_long"].astype(int)
    disp["n_trades_short"] = disp["n_trades_short"].astype(int)
    disp["long_net"] = disp["long_net"].map(lambda v: f"${v:+,.2f}")
    disp["short_net"] = disp["short_net"].map(lambda v: f"${v:+,.2f}")
    disp["pct_short"] = disp["pct_short"].map(lambda v: f"{v:.1f}%")
    disp = disp.rename(columns={
        "strategy_id": "variant",
        "n_trades_long": "n longs",
        "long_net": "long net",
        "n_trades_short": "n shorts",
        "short_net": "short net",
        "pct_short": "% short",
    })

    fleet_long = df[df["direction"] == "long"]
    fleet_short = df[df["direction"] == "short"]
    fleet_long_net = float(fleet_long["pnl_dollars"].sum())
    fleet_short_net = float(fleet_short["pnl_dollars"].sum())
    fleet_long_pf = profit_factor(float(fleet_long["pnl_pos"].sum()),
                                   float(fleet_long["pnl_neg"].sum()))
    fleet_short_pf = profit_factor(float(fleet_short["pnl_pos"].sum()),
                                    float(fleet_short["pnl_neg"].sum()))
    fleet_long_n = len(fleet_long)
    fleet_short_n = len(fleet_short)
    fleet_pct_short = fleet_short_n / (fleet_long_n + fleet_short_n) * 100

    # v4-loose-shorts deep-dive
    ls = splits[splits["strategy_id"] == "v4-loose-shorts"]
    ls_long_pf = float(ls[ls["direction"] == "long"]["profit_factor"].iloc[0]) if not ls.empty else 0
    ls_short_pf = float(ls[ls["direction"] == "short"]["profit_factor"].iloc[0]) if not ls.empty else 0
    ls_long_n = int(ls[ls["direction"] == "long"]["n_trades"].iloc[0]) if not ls.empty else 0
    ls_short_n = int(ls[ls["direction"] == "short"]["n_trades"].iloc[0]) if not ls.empty else 0
    ls_long_net = float(ls[ls["direction"] == "long"]["net_pnl"].iloc[0]) if not ls.empty else 0
    ls_short_net = float(ls[ls["direction"] == "short"]["net_pnl"].iloc[0]) if not ls.empty else 0

    # v4-trend-flip
    tf = splits[splits["strategy_id"] == "v4-trend-flip"]
    tf_long_n = int(tf[tf["direction"] == "long"]["n_trades"].iloc[0]) if not tf.empty else 0
    tf_short_n = int(tf[tf["direction"] == "short"]["n_trades"].iloc[0]) if not tf.empty else 0
    tf_long_net = float(tf[tf["direction"] == "long"]["net_pnl"].iloc[0]) if not tf.empty else 0
    tf_short_net = float(tf[tf["direction"] == "short"]["net_pnl"].iloc[0]) if not tf.empty else 0

    return f"""## §4 Direction and instrument

[`docs/v3_audit/direction_split.csv`](docs/v3_audit/direction_split.csv)

Instrument is MES across all variants. Direction (long/short) is the
only axis to split.

### Fleet

| Direction | n | Net P&L | PF |
|---|---:|---:|---:|
| long | {fleet_long_n:,} | ${fleet_long_net:+,.2f} | {fleet_long_pf:.2f} |
| short | {fleet_short_n:,} | ${fleet_short_net:+,.2f} | {fleet_short_pf:.2f} |
| **total** | **{fleet_long_n + fleet_short_n:,}** | **${fleet_long_net + fleet_short_net:+,.2f}** | |
| % short | | {fleet_pct_short:.1f}% | |

The fleet is **{fleet_pct_short:.0f}% short**. The 2026-05-07 post-mortem
noted the static cum-delta filter was structurally long-biased (1,063
longs vs 1 short over 4 days); the v4 asymmetric and regime variants
have moved the fleet partly off that bias.

### Per-variant skew (sorted by % short)

{df_to_md(disp)}

### v4-loose-shorts deep-dive

The "loose shorts" variant uses asymmetric percentile gating (top 12%
short, bottom 5% long) specifically to fix the long-bias problem.

| Direction | n | Net P&L | PF |
|---|---:|---:|---:|
| long | {ls_long_n:,} | ${ls_long_net:+,.2f} | {ls_long_pf:.2f} |
| short | {ls_short_n:,} | ${ls_short_net:+,.2f} | {ls_short_pf:.2f} |

**The asymmetric gate did not fire any shorts in this window.** The top-12%
threshold required cum_delta to spike positive enough to qualify a
short, and over the 7d window that didn't happen on MES. The 2026-05-07
long-bias problem hasn't been fixed by `v4-loose-shorts`; the asymmetric
gate just sits there.

### v4-trend-flip (the only spicy inverter)

Inverts counter-trend entries into following entries via the EMA(20)
trend classifier. Its short trades are the trend-down classification
flipping a LONG signal into a SHORT entry.

| Direction | n | Net P&L |
|---|---:|---:|
| long | {tf_long_n:,} | ${tf_long_net:+,.2f} |
| short | {tf_short_n:,} | ${tf_short_net:+,.2f} |

The inverter is taking real shorts, but as §1 showed v4-trend-flip lost
money overall in this window. The inversion direction *is* aligned with
the regime call; the *exit policy* (which it inherits from the v3 base)
still suffers the same 1-bar churn problem identified in §3.
"""


def main() -> int:
    df = load_settled_trades()
    splits = direction_split(df)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    write_csv("direction_split.csv", splits.to_dict(orient="records"),
              list(splits.columns))
    md = render_md(splits, df)
    append_section(DOCS_DIR / "v3_audit.md", md)
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
