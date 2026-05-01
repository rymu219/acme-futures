"""Streamlit-based backtest calculator.

Local-only UI for iterating on strategy parameters and date ranges. Runs the
existing `acme.backtest` engine but exposes every config knob as a form input
so you can see how parameter changes shift the result.

Launch:
    cd "/Users/ryanmurphy/Desktop/Acme Futures"
    uv run streamlit run web/backtest_ui.py
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta

import pandas as pd
import streamlit as st

from acme.backtest.bar_replay import evaluate_stage_0, run_backtest
from acme.backtest.data import iter_bars
from acme.contracts import MES
from acme.risk import TOPSTEP_50K
from acme.strategies.anti import AntiConfig, AntiStrategy
from acme.strategies.bb_mr import BBMRConfig, BollingerMeanReversionStrategy
from acme.strategies.donchian import DonchianBreakoutStrategy, DonchianConfig
from acme.strategies.ema_cross import EmaCrossConfig, EmaCrossStrategy
from acme.strategies.orb import OpeningRangeBreakoutStrategy, ORBConfig

st.set_page_config(
    page_title="Acme Backtest Calculator",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------- per-strategy form renderers ----------

def _ema_cross_form():
    st.markdown("**9/21 EMA crossover** — trend following, 1-min bars")
    c1, c2 = st.columns(2)
    with c1:
        fast = st.number_input("Fast EMA period", min_value=2, max_value=200, value=9, step=1)
        stop_ticks = st.number_input("Stop (ticks)", min_value=1, max_value=200, value=8, step=1)
    with c2:
        slow = st.number_input("Slow EMA period", min_value=3, max_value=400, value=21, step=1)
        target_ticks = st.number_input("Target (ticks)", min_value=1, max_value=400, value=16, step=1)
    risk = st.number_input("Risk per trade ($)", min_value=1.0, max_value=2000.0, value=25.0, step=5.0)
    return EmaCrossStrategy(
        config=EmaCrossConfig(
            fast=int(fast), slow=int(slow),
            stop_ticks=int(stop_ticks), target_ticks=int(target_ticks),
            risk_dollars_per_trade=float(risk),
        ),
        contract=MES,
    )


def _anti_form():
    st.markdown("**Raschke's Anti** — stochastic-based pullback in trend, 5-min bars")
    c1, c2, c3 = st.columns(3)
    with c1:
        trend_ema = st.number_input("Trend EMA period", min_value=5, max_value=200, value=20, step=1)
        fast_k = st.number_input("Fast stoch %K period", min_value=2, max_value=50, value=5, step=1)
    with c2:
        slow_k = st.number_input("Slow stoch %K period", min_value=2, max_value=50, value=14, step=1)
        ob = st.number_input("Stoch overbought", min_value=50.0, max_value=99.0, value=75.0, step=5.0)
    with c3:
        os_ = st.number_input("Stoch oversold", min_value=1.0, max_value=50.0, value=25.0, step=5.0)
        target_r = st.number_input("Target R-multiple", min_value=0.5, max_value=10.0, value=1.5, step=0.5)
    risk = st.number_input("Risk per trade ($)", min_value=1.0, max_value=2000.0, value=25.0, step=5.0, key="anti_risk")
    return AntiStrategy(
        config=AntiConfig(
            trend_ema_period=int(trend_ema),
            fast_k_period=int(fast_k),
            slow_k_period=int(slow_k),
            stoch_overbought=float(ob),
            stoch_oversold=float(os_),
            target_r_multiple=float(target_r),
            risk_dollars_per_trade=float(risk),
        ),
        contract=MES,
    )


def _orb_form():
    st.markdown("**Opening Range Breakout** — 15-min OR + volume-confirmed breakout, 5-min bars")
    c1, c2 = st.columns(2)
    with c1:
        or_min = st.number_input("OR window (min)", min_value=5, max_value=60, value=15, step=5)
        vol_mult = st.number_input("Volume multiple", min_value=1.0, max_value=5.0, value=1.2, step=0.1)
    with c2:
        target_mult = st.number_input("Target × OR width", min_value=0.5, max_value=5.0, value=1.0, step=0.5)
        risk = st.number_input("Risk per trade ($)", min_value=1.0, max_value=2000.0, value=25.0, step=5.0, key="orb_risk")
    st.caption("Heads up: with risk_per_trade=$25 and typical MES OR widths of 5–15 points "
               "(stops of $25–$75/contract), this strategy may not size up. Try $100+ risk to see real behavior.")
    return OpeningRangeBreakoutStrategy(
        config=ORBConfig(
            or_minutes=int(or_min),
            volume_multiple=float(vol_mult),
            target_or_width_multiple=float(target_mult),
            risk_dollars_per_trade=float(risk),
        ),
        contract=MES,
    )


def _donchian_form():
    st.markdown("**Donchian breakout** — 20-bar high/low, first-of-day only, 5-min bars")
    c1, c2 = st.columns(2)
    with c1:
        lookback = st.number_input("Lookback bars", min_value=5, max_value=100, value=20, step=1)
        atr_stop = st.number_input("ATR stop multiple", min_value=0.5, max_value=10.0, value=2.0, step=0.5)
    with c2:
        atr_target = st.number_input("ATR target multiple", min_value=0.5, max_value=10.0, value=2.0, step=0.5)
        risk = st.number_input("Risk per trade ($)", min_value=1.0, max_value=2000.0, value=25.0, step=5.0, key="don_risk")
    return DonchianBreakoutStrategy(
        config=DonchianConfig(
            lookback=int(lookback),
            atr_stop_multiple=float(atr_stop),
            atr_target_multiple=float(atr_target),
            risk_dollars_per_trade=float(risk),
        ),
        contract=MES,
    )


def _bb_mr_form():
    st.markdown("**Bollinger mean-reversion** — BB(20,2σ) + RSI(2) extremes + ADX<20, 5-min bars, mid-day only")
    c1, c2, c3 = st.columns(3)
    with c1:
        bb_p = st.number_input("BB period", min_value=5, max_value=100, value=20, step=1)
        bb_std = st.number_input("BB std multiple", min_value=1.0, max_value=4.0, value=2.0, step=0.25)
    with c2:
        rsi_long = st.number_input("RSI(2) long threshold", min_value=1.0, max_value=30.0, value=5.0, step=1.0)
        rsi_short = st.number_input("RSI(2) short threshold", min_value=70.0, max_value=99.0, value=95.0, step=1.0)
    with c3:
        adx_max = st.number_input("Max ADX (range gate)", min_value=10.0, max_value=40.0, value=20.0, step=1.0)
        atr_mult = st.number_input("Stop ATR multiple", min_value=0.5, max_value=5.0, value=1.5, step=0.25)
    daily_cap = st.number_input("Daily trade cap", min_value=1, max_value=20, value=2, step=1)
    risk = st.number_input("Risk per trade ($)", min_value=1.0, max_value=2000.0, value=25.0, step=5.0, key="bbmr_risk")
    return BollingerMeanReversionStrategy(
        config=BBMRConfig(
            bb_period=int(bb_p), bb_std=float(bb_std),
            rsi_long_threshold=float(rsi_long),
            rsi_short_threshold=float(rsi_short),
            adx_max_for_range=float(adx_max),
            stop_atr_multiple=float(atr_mult),
            daily_trade_cap=int(daily_cap),
            risk_dollars_per_trade=float(risk),
        ),
        contract=MES,
    )


STRATEGY_FORMS = {
    "ema_cross": _ema_cross_form,
    "anti":      _anti_form,
    "orb":       _orb_form,
    "donchian":  _donchian_form,
    "bb_mr":     _bb_mr_form,
}


# ---------- sidebar form ----------

st.sidebar.title("⚙️ Backtest Setup")

strategy_name = st.sidebar.selectbox("Strategy", list(STRATEGY_FORMS.keys()))

st.sidebar.markdown("---")
st.sidebar.markdown("**Date range**")

# Default to last 6 months (more responsive to current regime than 2-year window)
default_end = date(2026, 4, 30)
default_start = default_end - timedelta(days=180)

start_date = st.sidebar.date_input(
    "Start", value=default_start, min_value=date(2024, 4, 1), max_value=default_end,
)
end_date = st.sidebar.date_input(
    "End", value=default_end, min_value=date(2024, 4, 1), max_value=default_end,
)

# Quick-pick presets
preset = st.sidebar.radio(
    "Quick presets",
    ["custom", "last 30d", "last 90d", "last 6mo", "last 12mo", "full 2yr"],
    index=0,
    horizontal=True,
)
if preset != "custom":
    days_map = {"last 30d": 30, "last 90d": 90, "last 6mo": 180, "last 12mo": 365, "full 2yr": 760}
    start_date = default_end - timedelta(days=days_map[preset])
    end_date = default_end

st.sidebar.markdown("---")

starting_balance = st.sidebar.number_input(
    "Starting balance ($)", min_value=1000, max_value=1_000_000, value=50_000, step=1000,
)
enforce_time = st.sidebar.checkbox(
    "Enforce time-bucket gate", value=True,
    help="Restrict entry signals to the strategy's metadata.time_buckets (RTH for most). Recommended ON.",
)

st.sidebar.markdown("---")
st.sidebar.markdown("**Slippage (per side, ticks)**")
slip_entry = st.sidebar.number_input("Entry", min_value=0, max_value=10, value=1, step=1)
slip_stop = st.sidebar.number_input("Stop", min_value=0, max_value=10, value=2, step=1)
slip_target = st.sidebar.number_input("Target", min_value=0, max_value=10, value=1, step=1)


# ---------- main panel ----------

st.title("Acme Futures · Backtest Calculator")
st.caption(
    f"Strategy: **{strategy_name}**  |  "
    f"Window: **{start_date} → {end_date}** ({(end_date - start_date).days} days)"
)

with st.expander("Strategy parameters", expanded=True):
    strategy = STRATEGY_FORMS[strategy_name]()

run = st.button("▶ Run Backtest", type="primary", use_container_width=True)


# ---------- run + render ----------

def _money(n: float) -> str:
    sign = "-" if n < 0 else ""
    return f"{sign}${abs(n):,.2f}"


if run:
    start_dt = datetime.combine(start_date, time(0, 0), tzinfo=UTC)
    end_dt = datetime.combine(end_date, time(23, 59), tzinfo=UTC)

    with st.spinner(f"Running {strategy_name} on {(end_date - start_date).days} days of MES…"):
        bars_iter = iter_bars(start=start_dt, end=end_dt)
        report = run_backtest(
            strategy, bars_iter,
            profile=TOPSTEP_50K,
            starting_balance=float(starting_balance),
            slip_entry_ticks=int(slip_entry),
            slip_stop_ticks=int(slip_stop),
            slip_target_ticks=int(slip_target),
            enforce_time_buckets=enforce_time,
        )

    st.session_state["last_report"] = report
    st.session_state["last_starting_balance"] = float(starting_balance)


# Render whatever's in session state (so reruns don't blank the screen)
if "last_report" in st.session_state:
    report = st.session_state["last_report"]
    sb = st.session_state["last_starting_balance"]
    m = report.metrics

    # Top metrics row
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Trades", m.n_trades)
    c2.metric("Net P&L", _money(m.net_pnl),
              delta=_money(m.net_pnl) if m.n_trades else None,
              delta_color="normal" if m.net_pnl >= 0 else "inverse")
    c3.metric("Win rate", f"{m.win_rate*100:.1f}%")
    c4.metric("Profit factor", f"{m.profit_factor:.2f}" if m.profit_factor else "—")
    c5.metric("Sharpe", f"{m.sharpe:.2f}")

    # Stage 0 verdict
    s0 = evaluate_stage_0(report, sb)
    if s0["verdict"] == "PASS":
        st.success("### Stage 0: **PASS**  ✓")
    else:
        st.error("### Stage 0: **FAIL**")
    gate_cols = st.columns(4)
    for (gate, info), col in zip(s0["gates"].items(), gate_cols, strict=False):
        mark = "✅" if info["pass"] else "❌"
        col.write(f"{mark}  **{gate}**  \n{info['actual']}")

    st.markdown("---")

    # Equity curve
    if report.equity_curve:
        st.subheader("Equity curve")
        eq_df = pd.DataFrame(report.equity_curve, columns=["time", "equity_$"])
        eq_df["time"] = pd.to_datetime(eq_df["time"])
        eq_df = eq_df.set_index("time")
        st.line_chart(eq_df, height=300)
    else:
        st.info("No closed trades — no equity curve to plot.")

    # Per-trade details
    if report.trades:
        st.subheader(f"Trade history ({len(report.trades)} trades)")
        rows = []
        for t in report.trades:
            d = asdict(t)
            d["entry_t"] = t.entry_t.strftime("%Y-%m-%d %H:%M")
            d["exit_t"] = t.exit_t.strftime("%Y-%m-%d %H:%M")
            d["entry_price"] = round(d["entry_price"], 2)
            d["exit_price"] = round(d["exit_price"], 2)
            d["net_pnl"] = round(d["net_pnl"], 2)
            d["gross_pnl"] = round(d["gross_pnl"], 2)
            d["fees"] = round(d["fees"], 2)
            rows.append(d)
        trades_df = pd.DataFrame(rows)
        # Reorder for readability
        cols = ["entry_t", "exit_t", "side", "size", "outcome",
                "entry_price", "exit_price", "gross_pnl", "fees", "net_pnl", "bars_held"]
        trades_df = trades_df[cols]
        st.dataframe(trades_df, use_container_width=True, height=400)

        # Outcome breakdown
        st.markdown("**Outcome breakdown**")
        oc1, oc2 = st.columns(2)
        targets = [t for t in report.trades if t.outcome == "target"]
        stops = [t for t in report.trades if t.outcome == "stop"]
        with oc1:
            st.write(f"🎯 **Targets**: {len(targets)} trades, "
                     f"avg = {_money(sum(t.net_pnl for t in targets)/max(len(targets),1))}, "
                     f"total = {_money(sum(t.net_pnl for t in targets))}")
        with oc2:
            st.write(f"🛑 **Stops**: {len(stops)} trades, "
                     f"avg = {_money(sum(t.net_pnl for t in stops)/max(len(stops),1))}, "
                     f"total = {_money(sum(t.net_pnl for t in stops))}")

else:
    st.info("Configure the strategy on the left and click **Run Backtest** above.")
