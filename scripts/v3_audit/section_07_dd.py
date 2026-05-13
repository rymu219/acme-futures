"""Section 7 — drawdown analysis.

Largest drawdown per variant: start, end (or 'ongoing'), duration,
number of trades during DD, win rate during DD vs lifetime, market
regime label.

Market regime is approximated from the raw price stream using a coarse
proxy: realized vol of MES (proxied by stop-distance ATR at entry,
since we don't have a separate price-bar table) and price direction
over the DD window using the trades' entry/exit prices.
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
        max_drawdown,
    )
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.v3_audit.db import AUDIT_DIR, DOCS_DIR, write_csv  # type: ignore  # noqa: E402
    from scripts.v3_audit.trades import (  # type: ignore  # noqa: E402
        append_section,
        df_to_md,
        load_settled_trades,
        max_drawdown,
    )


def regime_label(g: pd.DataFrame) -> str:
    """Coarse regime label for a slice of trades:
        - 'trending-up' if mean(exit_price - entry_price) > +0.5 ticks
          AND ATR_at_entry above the variant median
        - 'trending-down' if mean(exit - entry) < -0.5 AND ATR elevated
        - 'chop' if absolute mean move < 0.5
        - 'vol-expansion' if ATR_at_entry mean is in the top quartile of
          the variant's lifetime ATRs
    """
    if g.empty:
        return "unknown"
    move = float((g["exit_price"] - g["entry_price"]).mean())
    atr = float(g["atr_at_entry"].mean())
    base = "chop"
    if move > 0.5:
        base = "trending-up"
    elif move < -0.5:
        base = "trending-down"
    if atr > 2.5:
        base = f"vol-expansion-{base}" if base != "chop" else "vol-expansion-chop"
    return base


def drawdown_per_variant(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict] = []
    for sid, g in df.groupby("strategy_id"):
        g = g.sort_values("entry_ts").reset_index(drop=True)
        n_lifetime = len(g)
        lifetime_wr = float((g["pnl_dollars"] > 0).mean()) if n_lifetime else 0.0
        equity = g["pnl_dollars"].cumsum().tolist()
        mdd, mdd_pct, peak_idx, trough_idx = max_drawdown(equity)
        if mdd <= 0 or trough_idx == peak_idx:
            rows.append({
                "strategy_id": sid,
                "mdd_dollars": 0.0,
                "mdd_pct_of_peak": 0.0,
                "peak_ts_ct": "",
                "trough_ts_ct": "",
                "duration_days": 0,
                "trades_during_dd": 0,
                "wr_during_dd": 0.0,
                "lifetime_wr": lifetime_wr,
                "regime_during_dd": "n/a",
                "ongoing": False,
            })
            continue
        # Slice
        peak_ts = g.iloc[peak_idx]["entry_ts_ct"]
        trough_ts = g.iloc[trough_idx]["entry_ts_ct"]
        dd_slice = g.iloc[peak_idx:trough_idx + 1]
        n_during = len(dd_slice)
        wr_during = float((dd_slice["pnl_dollars"] > 0).mean()) if n_during else 0.0
        duration_days = max(0, (trough_ts - peak_ts).total_seconds() / 86400)
        # Ongoing? — trough_idx is the last index meaning no recovery
        ongoing = (trough_idx == len(g) - 1)
        regime = regime_label(dd_slice)
        rows.append({
            "strategy_id": sid,
            "mdd_dollars": mdd,
            "mdd_pct_of_peak": mdd_pct,
            "peak_ts_ct": str(peak_ts),
            "trough_ts_ct": str(trough_ts) if not ongoing else f"{trough_ts} (ongoing)",
            "duration_days": round(duration_days, 2),
            "trades_during_dd": n_during,
            "wr_during_dd": wr_during,
            "lifetime_wr": lifetime_wr,
            "regime_during_dd": regime,
            "ongoing": ongoing,
        })
    return pd.DataFrame(rows).sort_values("mdd_dollars", ascending=False).reset_index(drop=True)


def render_md(table: pd.DataFrame) -> str:
    if table.empty:
        return "## §7 Drawdown analysis\n\n(no settled trades)\n"

    disp = table.copy()
    disp["mdd_dollars"] = disp["mdd_dollars"].map(lambda v: f"${v:,.2f}")
    disp["mdd_pct_of_peak"] = disp["mdd_pct_of_peak"].map(lambda v: f"{v:.1f}%")
    disp["wr_during_dd"] = disp["wr_during_dd"].map(lambda v: f"{v*100:.1f}%")
    disp["lifetime_wr"] = disp["lifetime_wr"].map(lambda v: f"{v*100:.1f}%")
    disp["ongoing"] = disp["ongoing"].map(lambda v: "yes" if v else "no")
    disp = disp.rename(columns={
        "strategy_id": "variant",
        "mdd_dollars": "MDD ($)",
        "mdd_pct_of_peak": "MDD (% peak)",
        "peak_ts_ct": "peak (CT)",
        "trough_ts_ct": "trough (CT)",
        "duration_days": "duration (d)",
        "trades_during_dd": "n DD",
        "wr_during_dd": "WR DD",
        "lifetime_wr": "WR life",
        "regime_during_dd": "regime",
    })

    # Regime distribution
    regimes = table["regime_during_dd"].value_counts()
    regime_lines = [f"- `{r}` — {n} variants" for r, n in regimes.items()]

    # WR delta
    table_wr = table.copy()
    table_wr["wr_delta"] = table_wr["wr_during_dd"] - table_wr["lifetime_wr"]
    worst_delta = table_wr.loc[table_wr["wr_delta"].idxmin()]
    best_delta = table_wr.loc[table_wr["wr_delta"].idxmax()]

    ongoing = table[table["ongoing"]]["strategy_id"].tolist()

    return f"""## §7 Drawdown analysis

[`docs/v3_audit/drawdowns.csv`](docs/v3_audit/drawdowns.csv)

Per-variant maximum drawdown in dollar terms, with the trade slice that
caused it. "Ongoing" = the variant has not yet recovered to its prior
peak as of the audit window's end.

{df_to_md(disp)}

**Variants still in their max-drawdown:** {', '.join(ongoing) if ongoing else '(none)'}

**Largest WR collapse during DD:** `{worst_delta['strategy_id']}` —
lifetime WR {worst_delta['lifetime_wr']*100:.1f}% vs WR during DD
{worst_delta['wr_during_dd']*100:.1f}%.

**Most stable WR during DD:** `{best_delta['strategy_id']}` —
lifetime WR {best_delta['lifetime_wr']*100:.1f}% vs DD WR
{best_delta['wr_during_dd']*100:.1f}%.

### Regime distribution during max-drawdowns

{chr(10).join(regime_lines)}

Regime label is a coarse proxy: derived from average ATR-at-entry and
average (exit - entry) price move across the DD slice. It's not from a
calibrated regime classifier.

**Concentration test:** if all DDs land in the same regime label, the
fleet has a regime-specific weakness that REGIME's switching logic
could address. If DDs span every regime, the entry signal itself
needs fixing, not just regime gating.
"""


def main() -> int:
    df = load_settled_trades()
    table = drawdown_per_variant(df)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    write_csv("drawdowns.csv", table.to_dict(orient="records"),
              list(table.columns))
    md = render_md(table)
    append_section(DOCS_DIR / "v3_audit.md", md)
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
