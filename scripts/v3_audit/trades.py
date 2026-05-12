"""Shared trade-data loader for Sections 1-9 of the v3 audit.

Single source of truth for the dataframe everyone analyzes:
- pulls all paper trades from ryan_spec_v3_trades
- excludes the 10 orphaned rows from 2026-05-07 22:08 CT (those have
  exit_ts IS NULL but their variants' heartbeats are flat — they're
  not real held positions, just data corruption from a SignalR drop)
- excludes other open trades from current analysis (settled-only)
- adds CT-localized datetime columns for hour/day-of-week analysis

Tag policy: variants with fewer than MIN_TRADES_FOR_INFERENCE settled
trades are still included in tables but tagged with `low_sample = True`
so callers can present the [low_sample] qualifier.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

import pandas as pd

try:
    from scripts.v3_audit.db import (  # type: ignore
        CT, MIN_TRADES_FOR_INFERENCE, UTC, fetch_all_v3_trades, get_client,
    )
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.v3_audit.db import (  # type: ignore  # noqa: E402
        CT, MIN_TRADES_FOR_INFERENCE, UTC, fetch_all_v3_trades, get_client,
    )


# MES contract spec for $/point conversion. The trade rows already store
# pnl_dollars so we don't multiply by this — kept here for documentation.
MES_DOLLARS_PER_POINT = 5.0


def load_settled_trades(*, mode: str = "paper") -> pd.DataFrame:
    """Return a DataFrame of every settled paper trade.

    Settled = exit_ts IS NOT NULL. The 10 orphaned 2026-05-07 22:08 rows
    are open (exit_ts NULL) so this filter excludes them naturally.

    Columns:
        id, strategy_id, mode, direction, bar_ts (UTC tz-aware),
        bar_ts_ct (CT tz-aware), entry_ts, exit_ts, entry_price, exit_price,
        stop_price, atr_at_entry, cum_delta_at_entry, exit_reason,
        pnl_dollars (float), bars_held (int), mfe_atr, mae_atr,
        slippage_ticks, hour_ct (0-23), date_ct (date), dow_ct (0-6).
    """
    sb = get_client()
    rows = fetch_all_v3_trades(sb, mode=mode)
    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # Settled-only
    df = df[df["exit_ts"].notna()].copy()
    # Force numerics in case Supabase returned strings
    for col in ("pnl_dollars", "entry_price", "exit_price", "stop_price",
                "atr_at_entry", "mfe_atr", "mae_atr"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    # Drop trades where pnl_dollars was never computed. As of 2026-05-11
    # this is exactly the 53 v3-canon `broker_error` rows — those are
    # runtime errors, not real trade outcomes, and including them
    # NaN-poisons every aggregation (cumsum, mean, ...).
    df = df[df["pnl_dollars"].notna()].copy()
    if "bars_held" in df.columns:
        df["bars_held"] = pd.to_numeric(df["bars_held"], errors="coerce").astype("Int64")
    if "cum_delta_at_entry" in df.columns:
        df["cum_delta_at_entry"] = pd.to_numeric(
            df["cum_delta_at_entry"], errors="coerce"
        ).astype("Int64")

    # Time columns
    df["bar_ts"] = pd.to_datetime(df["bar_ts"], utc=True)
    df["entry_ts"] = pd.to_datetime(df["entry_ts"], utc=True)
    df["exit_ts"] = pd.to_datetime(df["exit_ts"], utc=True)
    df["bar_ts_ct"] = df["bar_ts"].dt.tz_convert(CT)
    df["entry_ts_ct"] = df["entry_ts"].dt.tz_convert(CT)
    df["exit_ts_ct"] = df["exit_ts"].dt.tz_convert(CT)

    # Derived
    df["hour_ct"] = df["entry_ts_ct"].dt.hour.astype("Int64")
    df["date_ct"] = df["entry_ts_ct"].dt.date
    df["dow_ct"] = df["entry_ts_ct"].dt.dayofweek.astype("Int64")  # Mon=0
    df["pnl_pos"] = df["pnl_dollars"].clip(lower=0)
    df["pnl_neg"] = (-df["pnl_dollars"]).clip(lower=0)  # absolute losses
    df["is_win"] = df["pnl_dollars"] > 0

    df = df.sort_values("entry_ts").reset_index(drop=True)
    return df


def low_sample_set(df: pd.DataFrame, *, by: str = "strategy_id") -> set[str]:
    """Return strategy_ids whose settled trade count < threshold."""
    if df.empty:
        return set()
    counts = df.groupby(by).size()
    return set(counts[counts < MIN_TRADES_FOR_INFERENCE].index)


def tag(name: str, low_sample: bool) -> str:
    """Format a name with [low_sample] qualifier when applicable."""
    return f"{name} [low_sample]" if low_sample else name


def profit_factor(wins_dollars: float, losses_dollars_abs: float) -> float:
    if losses_dollars_abs <= 0:
        return float("inf") if wins_dollars > 0 else 0.0
    return wins_dollars / losses_dollars_abs


def max_drawdown(equity_curve: Iterable[float]) -> tuple[float, float, int, int]:
    """Return (mdd_dollars, mdd_pct_of_peak, peak_idx, trough_idx).

    Drawdown computed against the running peak of the equity curve. Equity
    curve should be cumulative P&L starting at 0.
    """
    curve = list(equity_curve)
    if not curve:
        return 0.0, 0.0, 0, 0
    peak = curve[0]
    peak_idx = 0
    mdd = 0.0
    mdd_peak_idx = 0
    mdd_trough_idx = 0
    for i, v in enumerate(curve):
        if v > peak:
            peak = v
            peak_idx = i
        dd = peak - v
        if dd > mdd:
            mdd = dd
            mdd_peak_idx = peak_idx
            mdd_trough_idx = i
    mdd_pct = (mdd / peak * 100.0) if peak > 0 else 0.0
    return mdd, mdd_pct, mdd_peak_idx, mdd_trough_idx


def sharpe_daily(daily_pnl: pd.Series) -> float:
    """Annualized Sharpe on daily P&L. 252 trading days, zero risk-free
    rate. Returns 0 when stdev is 0 or fewer than 2 samples."""
    if len(daily_pnl) < 2:
        return 0.0
    std = daily_pnl.std()
    if std == 0 or pd.isna(std):
        return 0.0
    return float(daily_pnl.mean() / std * (252 ** 0.5))


def daily_pnl_per_variant(df: pd.DataFrame) -> pd.DataFrame:
    """Pivot: rows = date_ct, cols = strategy_id, values = sum(pnl_dollars).
    Missing days are 0 (the variant didn't trade)."""
    if df.empty:
        return pd.DataFrame()
    daily = (
        df.groupby(["date_ct", "strategy_id"])["pnl_dollars"].sum()
        .unstack(fill_value=0.0)
    )
    return daily


def append_section(md_path, section_md: str) -> None:
    """Append a markdown chunk to docs/v3_audit.md, preceded by a divider."""
    with open(md_path, "a") as f:
        f.write("\n\n---\n\n")
        f.write(section_md.rstrip() + "\n")


# convenience for table rendering
def df_to_md(df: pd.DataFrame, *, floatfmt: str = ".2f",
             intcols: Iterable[str] = ()) -> str:
    """Render a DataFrame as a github-flavored markdown table.

    Uses pandas.DataFrame.to_markdown if tabulate is installed; falls back
    to a manual renderer otherwise.
    """
    try:
        return df.to_markdown(index=False, floatfmt=floatfmt)
    except (ImportError, ValueError):
        # Manual fallback (no tabulate dep)
        cols = list(df.columns)
        head = "| " + " | ".join(str(c) for c in cols) + " |"
        sep = "| " + " | ".join("---" for _ in cols) + " |"
        rows = []
        for _, r in df.iterrows():
            cells = []
            for c in cols:
                v = r[c]
                if pd.isna(v):
                    cells.append("")
                elif c in intcols:
                    cells.append(f"{int(v)}")
                elif isinstance(v, float):
                    cells.append(f"{v:{floatfmt[1:]}}" if floatfmt.startswith(".") else f"{v}")
                else:
                    cells.append(str(v))
            rows.append("| " + " | ".join(cells) + " |")
        return "\n".join([head, sep, *rows])
