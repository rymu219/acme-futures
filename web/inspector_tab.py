"""Strategy Inspector — read ~/.acme/telemetry.sqlite, surface disqualifying
conditions per strategy.

Goal: not optimization, but "brilliant at not being bad". For each strategy,
bucket every fire by each universal market-context feature and identify
buckets with negative expectancy — those are the conditions where the
strategy has no edge or worse, so we can simply not fire there.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

from acme.strategies.anti import AntiStrategy
from acme.strategies.bb_mr import BollingerMeanReversionStrategy
from acme.strategies.donchian import DonchianBreakoutStrategy
from acme.strategies.ema_cross import EmaCrossStrategy
from acme.strategies.orb import OpeningRangeBreakoutStrategy
from acme.strategies.supertrend import SupertrendStrategy
from acme.strategies.turtle_soup import TurtleSoupStrategy
from acme.strategies.turtles_system2 import TurtlesSystem2Strategy

DB_PATH = Path.home() / ".acme" / "telemetry.sqlite"

STRATEGY_CLASSES = {
    "ema_cross":       EmaCrossStrategy,
    "anti":            AntiStrategy,
    "orb":             OpeningRangeBreakoutStrategy,
    "donchian":        DonchianBreakoutStrategy,
    "bb_mr":           BollingerMeanReversionStrategy,
    "turtle_soup":     TurtleSoupStrategy,
    "supertrend":      SupertrendStrategy,
    "turtles_system2": TurtlesSystem2Strategy,
}

CONTEXT_FEATURES = [
    "ctx_volume_ratio_20",
    "ctx_momentum_5",
    "ctx_range_vs_atr",
    "ctx_close_position_in_bar",
    "ctx_close_vs_ema9",
    "ctx_close_vs_ema21",
    "ctx_close_vs_ema50",
]


def _load_fires(strategy: str, source: str | None, run_id: str | None) -> pd.DataFrame:
    if not DB_PATH.exists():
        return pd.DataFrame()
    conn = sqlite3.connect(str(DB_PATH))
    try:
        query = """
            SELECT
                e.id, e.bar_t, e.timeframe, e.strategy, e.source, e.run_id,
                e.bar_c, e.sig_side, e.sig_size, e.sig_reason,
                e.ctx_volume_ratio_20, e.ctx_momentum_5, e.ctx_range_vs_atr,
                e.ctx_close_position_in_bar,
                e.ctx_close_vs_ema9, e.ctx_close_vs_ema21, e.ctx_close_vs_ema50,
                o.exit_t, o.exit_price, o.net_pnl, o.outcome
            FROM bar_events e
            LEFT JOIN trade_outcomes o ON o.bar_event_id = e.id
            WHERE e.strategy = ? AND e.fired = 1
        """
        params: list[object] = [strategy]
        if source and source != "all":
            query += " AND e.source = ?"
            params.append(source)
        if run_id and run_id != "all":
            query += " AND e.run_id = ?"
            params.append(run_id)
        query += " ORDER BY e.bar_t"
        df = pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()
    return df


def _load_run_ids(source: str | None) -> list[str]:
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(str(DB_PATH))
    try:
        query = "SELECT DISTINCT run_id FROM bar_events"
        params: list[object] = []
        if source and source != "all":
            query += " WHERE source = ?"
            params.append(source)
        query += " ORDER BY run_id DESC LIMIT 30"
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def _bucket_winrate(df: pd.DataFrame, feature: str, n_bins: int = 6) -> pd.DataFrame:
    """Bucket fires by `feature` and compute n / win_rate / avg_pnl per bin."""
    valid = df.dropna(subset=[feature, "net_pnl"])
    if len(valid) < 5:
        return pd.DataFrame()
    try:
        valid = valid.copy()
        valid["bucket"] = pd.qcut(valid[feature], q=min(n_bins, len(valid)),
                                  duplicates="drop")
    except ValueError:
        return pd.DataFrame()
    grouped = valid.groupby("bucket", observed=True).agg(
        n=("net_pnl", "size"),
        win_rate=("net_pnl", lambda s: (s > 0).mean()),
        avg_pnl=("net_pnl", "mean"),
        total_pnl=("net_pnl", "sum"),
    ).reset_index()
    grouped["bucket"] = grouped["bucket"].astype(str)
    return grouped


def _winrate_color(rate: float) -> str:
    if rate < 0.40:
        return "#c0392b"   # red — disqualifying zone
    if rate < 0.55:
        return "#d4ac0d"   # yellow
    return "#27ae60"       # green


def render() -> None:
    st.subheader("Strategy Inspector")
    st.caption(
        "Bucket each fire by universal market-context features. "
        "**Red bins are disqualifying zones** — conditions where the strategy "
        "has no edge or worse. The goal isn't optimization; it's stripping bars "
        "where the strategy bleeds."
    )

    if not DB_PATH.exists():
        st.warning(
            f"No telemetry database at `{DB_PATH}`. Run a backtest with "
            "`uv run python -m acme.backtest.run --telemetry-mode full` "
            "to populate it."
        )
        return

    c1, c2, c3 = st.columns([1, 1, 2])
    with c1:
        strategy = st.selectbox("Strategy", list(STRATEGY_CLASSES.keys()), key="insp_strat")
    with c2:
        source = st.selectbox("Source", ["all", "backtest", "live"], key="insp_source")
    with c3:
        run_ids = _load_run_ids(source)
        run_id = st.selectbox("Run", ["all"] + run_ids, key="insp_run")

    df = _load_fires(strategy, source, run_id)

    if df.empty:
        st.info("No fires recorded for this strategy / source / run.")
        return

    closed = df.dropna(subset=["net_pnl"])
    n_fires = len(df)
    n_closed = len(closed)
    win_rate = (closed["net_pnl"] > 0).mean() if n_closed else 0.0
    avg_pnl = closed["net_pnl"].mean() if n_closed else 0.0
    total_pnl = closed["net_pnl"].sum() if n_closed else 0.0
    pf_num = closed.loc[closed["net_pnl"] > 0, "net_pnl"].sum() if n_closed else 0.0
    pf_den = -closed.loc[closed["net_pnl"] < 0, "net_pnl"].sum() if n_closed else 0.0
    pf = pf_num / pf_den if pf_den > 0 else float("nan")

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Fires", f"{n_fires}")
    m2.metric("Closed", f"{n_closed}")
    m3.metric("Win rate", f"{win_rate*100:.1f}%")
    m4.metric("Avg P&L", f"${avg_pnl:,.2f}" if n_closed else "—")
    m5.metric("Profit factor", f"{pf:.2f}" if pf_den > 0 else "—")
    st.caption(f"Total realized P&L on closed fires: **${total_pnl:,.2f}**")

    if n_closed < 10:
        st.warning(f"Only {n_closed} closed trades — need ≥ 10 for reliable bucketing.")

    st.markdown("---")
    st.markdown("### Disqualifying-zone analysis")
    st.caption(
        "Each chart buckets fires by a market-context feature. "
        "Bars are colored by win rate: **red < 40% (don't fire here)**, "
        "yellow 40–55%, green > 55%. Bar height = avg P&L per fire in that bucket."
    )

    if n_closed >= 10:
        feature_cols = st.columns(2)
        for i, feature in enumerate(CONTEXT_FEATURES):
            buckets = _bucket_winrate(closed, feature)
            with feature_cols[i % 2]:
                st.markdown(f"**{feature.replace('ctx_', '')}**")
                if buckets.empty:
                    st.caption("(insufficient data)")
                    continue
                buckets["color"] = buckets["win_rate"].apply(_winrate_color)
                # Use altair-style bar chart via streamlit's built-in
                chart_df = buckets.set_index("bucket")[["avg_pnl"]]
                st.bar_chart(chart_df, height=200)
                # Annotated table
                disp = buckets[["bucket", "n", "win_rate", "avg_pnl"]].copy()
                disp["win_rate"] = (disp["win_rate"] * 100).round(1).astype(str) + "%"
                disp["avg_pnl"] = disp["avg_pnl"].round(2).map("${:,.2f}".format)
                st.dataframe(disp, hide_index=True, use_container_width=True)

    st.markdown("---")
    st.markdown(f"### Fires ({n_fires} total)")
    table_cols = ["bar_t", "sig_side", "sig_size", "outcome", "net_pnl"] + CONTEXT_FEATURES
    table_df = df[table_cols].copy()
    for col in CONTEXT_FEATURES:
        table_df[col] = table_df[col].round(3)
    st.dataframe(table_df, use_container_width=True, height=300)

    st.markdown("---")
    st.markdown(f"### Tunable parameters — `{strategy}`")
    st.caption("Read-only manifest. The future sweep tab reads from this same source.")
    cls = STRATEGY_CLASSES[strategy]
    params = cls.tunable_params()
    params_rows = [
        {
            "name": p.name,
            "type": p.type.__name__,
            "default": p.default,
            "min": p.min_value,
            "max": p.max_value,
            "step": p.step,
            "description": p.description,
        }
        for p in params
    ]
    st.dataframe(pd.DataFrame(params_rows), hide_index=True, use_container_width=True)
