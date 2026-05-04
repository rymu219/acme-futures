"""Streamlit Regime tab — current regime + strategy×regime expectancy matrix.

Reads from Supabase (market_regimes, regime_strategy_performance, coverage_gaps).
Reuses existing strategy classes for the habitat_match column. No write paths.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import streamlit as st

from acme.db import Db
from acme.regime.habitat import REGIME_TO_FIT_KEY

REGIME_EMOJI = {
    "trending": "↗ Trending",
    "ranging": "↔ Ranging",
    "compressing": "⊟ Compressing",
    "chaotic": "⚠ Chaotic",
    "ambiguous": "? Ambiguous",
}

REGIME_COLOR = {
    "trending": "#27ae60",
    "ranging": "#2980b9",
    "compressing": "#f1c40f",
    "chaotic": "#c0392b",
    "ambiguous": "#7f8c8d",
}


@st.cache_data(ttl=30)
def _fetch_latest_regime() -> dict | None:
    db = Db()
    return db.latest_regime()


@st.cache_data(ttl=30)
def _fetch_recent_regimes(n: int = 200) -> pd.DataFrame:
    db = Db()
    try:
        res = (
            db.client.table("market_regimes")
            .select("ts, regime, confidence, adx, atr_ratio, hurst, regime_direction")
            .order("ts", desc=True)
            .limit(n)
            .execute()
        )
    except Exception as e:
        st.error(f"Failed to fetch market_regimes: {e}")
        return pd.DataFrame()
    return pd.DataFrame(res.data or [])


@st.cache_data(ttl=60)
def _fetch_regime_perf() -> pd.DataFrame:
    db = Db()
    try:
        res = (
            db.client.table("regime_strategy_performance")
            .select("*")
            .execute()
        )
    except Exception as e:
        st.error(f"Failed to fetch regime_strategy_performance: {e}")
        return pd.DataFrame()
    return pd.DataFrame(res.data or [])


@st.cache_data(ttl=120)
def _fetch_coverage_gaps() -> pd.DataFrame:
    db = Db()
    try:
        res = (
            db.client.table("coverage_gaps")
            .select("ts, regime, duration_bars, adx, atr_ratio, hurst")
            .order("ts", desc=True)
            .limit(200)
            .execute()
        )
    except Exception as e:
        st.error(f"Failed to fetch coverage_gaps: {e}")
        return pd.DataFrame()
    return pd.DataFrame(res.data or [])


def _color_expectancy(val: float | None) -> str:
    if val is None or pd.isna(val):
        return "color: #7f8c8d"   # gray for missing
    if val > 0:
        return "color: #27ae60; font-weight: 600"
    if val < 0:
        return "color: #c0392b"
    return "color: #95a5a6"


def render() -> None:
    st.subheader("Regime Engine")
    st.caption(
        "What is the river offering, and do we have a bucket? "
        "Bots are gated by their declared habitat — a strategy with `regime_fit[trending] ≥ 0.7` "
        "fires only in TRENDING; one with `regime_fit[ranging] ≥ 0.7` only in RANGING. "
        "CHAOTIC and COMPRESSING silence everything by design."
    )

    # ---------- Current regime card ----------
    st.markdown("### Current state")
    latest = _fetch_latest_regime()
    if latest is None:
        st.warning("No market_regimes rows yet. Run the backfill: "
                   "`uv run python -m acme.regime.backfill`")
    else:
        ts = latest.get("ts")
        regime = latest.get("regime", "ambiguous")
        conf = float(latest.get("confidence") or 0.0)
        direction = latest.get("regime_direction") or "neutral"
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.markdown(
            f"### <span style='color:{REGIME_COLOR.get(regime, '#888')}'>{REGIME_EMOJI.get(regime, regime)}</span>",
            unsafe_allow_html=True,
        )
        c2.metric("Direction", direction.replace("_", " ").title())
        c3.metric("Confidence", f"{conf:.2f}")
        adx_val = latest.get("adx")
        c4.metric("ADX", f"{float(adx_val):.1f}" if adx_val is not None else "—")
        hurst_val = latest.get("hurst")
        c5.metric("Hurst", f"{float(hurst_val):.2f}" if hurst_val is not None else "—")
        if ts:
            try:
                ts_parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                age_min = (datetime.now(UTC) - ts_parsed).total_seconds() / 60
                st.caption(f"Snapshot at `{ts}` ({age_min:.0f} min ago).")
            except Exception:
                st.caption(f"Snapshot at `{ts}`.")

    st.markdown("---")

    # ---------- Strategy × regime matrix ----------
    st.markdown("### Strategy × regime expectancy matrix")
    st.caption(
        "Expectancy per strategy per regime, computed from retroactively tagged trades. "
        "Habitat cells (regime in strategy's declared `regime_fit ≥ 0.7`) are highlighted. "
        "**Goal**: positive expectancy in habitat, ≈zero or negative outside."
    )
    perf = _fetch_regime_perf()
    if perf.empty:
        st.info("No regime_strategy_performance yet. Run analytics after backfill: "
                "`uv run python -m acme.regime.analytics`")
    else:
        regimes_in_data = sorted(perf["regime"].unique())
        strategies_in_data = sorted(perf["strategy"].unique())
        # Pivot expectancy
        expect_pivot = perf.pivot_table(
            index="strategy", columns="regime", values="expectancy", aggfunc="first",
        ).reindex(index=strategies_in_data, columns=regimes_in_data)
        count_pivot = perf.pivot_table(
            index="strategy", columns="regime", values="trade_count", aggfunc="first",
        ).reindex(index=strategies_in_data, columns=regimes_in_data)
        habitat_pivot = perf.pivot_table(
            index="strategy", columns="regime", values="habitat_match", aggfunc="first",
        ).reindex(index=strategies_in_data, columns=regimes_in_data)

        # Render expectancy with color
        styled = expect_pivot.style.applymap(_color_expectancy).format(
            "{:+.2f}", na_rep="—"
        )
        st.markdown("**Expectancy ($/trade)**")
        st.dataframe(styled, use_container_width=True)

        st.markdown("**Trade count**")
        st.dataframe(
            count_pivot.fillna(0).astype(int),
            use_container_width=True,
        )

        st.markdown("**Habitat match (★ = regime ∈ strategy's declared habitat)**")
        habitat_display = habitat_pivot.fillna(False).map(lambda v: "★" if v else "·")
        st.dataframe(habitat_display, use_container_width=True)

        # Quick summary: how many strategies are positive in their habitat?
        in_habitat = perf[perf["habitat_match"]]
        if not in_habitat.empty:
            positive_in_habitat = in_habitat[in_habitat["expectancy"] > 0]
            st.info(
                f"**{len(positive_in_habitat)} of {len(in_habitat)} habitat cells are positive** "
                f"({100 * len(positive_in_habitat) / len(in_habitat):.0f}%). "
                f"That's the habitat hypothesis playing out."
            )

    st.markdown("---")

    # ---------- Recent regime history ----------
    st.markdown("### Recent regime history (last 200 snapshots)")
    history = _fetch_recent_regimes(200)
    if not history.empty:
        history = history.sort_values("ts").reset_index(drop=True)
        history["ts"] = pd.to_datetime(history["ts"])
        regime_counts = history["regime"].value_counts()
        bars_total = int(regime_counts.sum())
        cols = st.columns(len(regime_counts))
        for col, (regime, cnt) in zip(cols, regime_counts.items(), strict=False):
            col.metric(REGIME_EMOJI.get(regime, regime),
                       f"{cnt}", f"{100 * cnt / bars_total:.0f}%")
        # Confidence-over-time chart
        chart_df = history[["ts", "confidence"]].set_index("ts")
        st.line_chart(chart_df, height=200)

    # ---------- Coverage gaps ----------
    st.markdown("---")
    st.markdown("### Coverage gaps (recent)")
    st.caption(
        "Periods where the active regime had no eligible strategies. "
        "These are the conditions our fleet currently can't capture."
    )
    gaps = _fetch_coverage_gaps()
    if gaps.empty:
        st.caption("No coverage_gaps rows yet (run the analytics).")
    else:
        st.dataframe(gaps, hide_index=True, use_container_width=True)

    # ---------- Habitat declaration sanity panel ----------
    st.markdown("---")
    with st.expander("Habitat declarations (what each strategy claims)", expanded=False):
        from acme.strategies.anti import AntiStrategy
        from acme.strategies.bb_mr import BollingerMeanReversionStrategy
        from acme.strategies.donchian import DonchianBreakoutStrategy
        from acme.strategies.ema_cross import EmaCrossStrategy
        from acme.strategies.orb import OpeningRangeBreakoutStrategy
        from acme.strategies.supertrend import SupertrendStrategy
        from acme.strategies.turtle_soup import TurtleSoupStrategy
        from acme.strategies.turtles_system2 import TurtlesSystem2Strategy
        rows = []
        for cls in [
            EmaCrossStrategy, AntiStrategy, OpeningRangeBreakoutStrategy,
            DonchianBreakoutStrategy, BollingerMeanReversionStrategy,
            TurtleSoupStrategy, SupertrendStrategy, TurtlesSystem2Strategy,
        ]:
            rf = cls.metadata.regime_fit
            rows.append({
                "strategy": cls.name,
                "trending": rf.get("trending", 0.0),
                "ranging": rf.get("ranging", 0.0),
                "volatile": rf.get("volatile", 0.0),
                "quiet": rf.get("quiet", 0.0),
                "habitat (>=0.7)": ", ".join(
                    fit_key for fit_key, weight in rf.items()
                    if weight >= 0.7 and fit_key in REGIME_TO_FIT_KEY
                ) or "(none)",
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
