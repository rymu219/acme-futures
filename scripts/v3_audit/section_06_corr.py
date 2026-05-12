"""Section 6 — inter-variant correlation and cluster outcomes.

Pairwise correlation of daily P&L per variant (flag pairs > 0.7).
Cluster-event outcome analysis using §0.6's cluster_event_log.csv.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd

try:
    from scripts.v3_audit.db import AUDIT_DIR, DOCS_DIR, write_csv  # type: ignore
    from scripts.v3_audit.trades import (  # type: ignore
        append_section, daily_pnl_per_variant, df_to_md, load_settled_trades,
    )
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.v3_audit.db import AUDIT_DIR, DOCS_DIR, write_csv  # type: ignore  # noqa: E402
    from scripts.v3_audit.trades import (  # type: ignore  # noqa: E402
        append_section, daily_pnl_per_variant, df_to_md, load_settled_trades,
    )


HIGH_CORR_THRESHOLD = 0.7


def correlation_analysis(df: pd.DataFrame) -> tuple[pd.DataFrame, list[tuple[str, str, float]]]:
    daily = daily_pnl_per_variant(df)
    if daily.empty:
        return pd.DataFrame(), []
    corr = daily.corr()
    # Find high-corr pairs (off-diagonal, unique)
    pairs: list[tuple[str, str, float]] = []
    cols = list(corr.columns)
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            r = float(corr.loc[a, b])
            if pd.notna(r) and r >= HIGH_CORR_THRESHOLD:
                pairs.append((a, b, r))
    pairs.sort(key=lambda p: -p[2])
    return corr, pairs


def cluster_outcomes(df: pd.DataFrame) -> pd.DataFrame:
    """Read §0.6's cluster_event_log.csv. For each cluster event, look
    up the cluster's outcome — the sum of pnl_dollars across the
    variants that participated, for trades whose entry_ts falls inside
    the cluster window.
    """
    cluster_csv = AUDIT_DIR / "cluster_event_log.csv"
    if not cluster_csv.exists():
        return pd.DataFrame()

    clusters: list[dict] = []
    with cluster_csv.open() as f:
        for row in csv.DictReader(f):
            clusters.append(row)

    # Index trades by (strategy_id, entry_ts) for fast lookup
    # We match a cluster's variant list against entries whose entry_ts
    # falls inside the cluster's 5-minute window.
    work = df.copy()
    work["entry_ts_utc"] = work["entry_ts"]  # already UTC tz-aware

    out: list[dict] = []
    for c in clusters:
        anchor_str = c.get("anchor_ts_ct")  # e.g. "2026-05-11 18:22:01 CT"
        if not anchor_str:
            continue
        # Parse CT datetime then convert to UTC for matching
        try:
            anchor_local = pd.Timestamp(anchor_str.replace(" CT", ""))
            anchor = anchor_local.tz_localize("America/Chicago").tz_convert("UTC")
        except Exception:
            continue
        variants = c.get("variant_list", "").split(",") if c.get("variant_list") else []
        if not variants:
            continue
        window_end = anchor + pd.Timedelta(minutes=5)
        match = work[
            (work["strategy_id"].isin(variants))
            & (work["entry_ts_utc"] >= anchor)
            & (work["entry_ts_utc"] <= window_end)
        ]
        n_match = len(match)
        cluster_pnl = float(match["pnl_dollars"].sum()) if n_match else 0.0
        wins = int((match["pnl_dollars"] > 0).sum())
        out.append({
            "anchor_ts_ct": c.get("anchor_ts_ct"),
            "direction": c.get("direction"),
            "n_variants_declared": int(c.get("n_variants", 0) or 0),
            "n_settled_matched": n_match,
            "cluster_net_pnl": cluster_pnl,
            "win_rate": (wins / n_match) if n_match else 0.0,
        })
    return pd.DataFrame(out)


def render_md(corr: pd.DataFrame, pairs: list[tuple[str, str, float]],
              cluster_df: pd.DataFrame) -> str:
    # Correlation matrix as a compact rounded display
    if corr.empty:
        corr_section = "(no daily data)"
    else:
        # Show every variant pair; the matrix is 16x16 — fits OK
        disp = corr.round(2)
        # Add variant labels
        corr_md = "| variant | " + " | ".join(disp.columns) + " |\n"
        corr_md += "|---|" + "|".join(["---"] * len(disp.columns)) + "|\n"
        for sid in disp.index:
            row = [f"{disp.loc[sid, c]:+.2f}" for c in disp.columns]
            corr_md += f"| {sid} | " + " | ".join(row) + " |\n"
        corr_section = corr_md

    pair_lines = [f"- `{a}` ↔ `{b}` → r = {r:+.2f}" for a, b, r in pairs]

    # Cluster outcome rollups
    if cluster_df.empty:
        cluster_summary = "(no cluster events in log)"
        bucket_summary = ""
    else:
        df = cluster_df.copy()
        df["bucket"] = df["n_variants_declared"].apply(
            lambda n: "3-5" if n <= 5 else "6-10" if n <= 10 else "11+"
        )
        bucket = df.groupby("bucket").agg(
            n_clusters=("anchor_ts_ct", "count"),
            total_pnl=("cluster_net_pnl", "sum"),
            avg_pnl_per_cluster=("cluster_net_pnl", "mean"),
            avg_n_matched=("n_settled_matched", "mean"),
            mean_win_rate=("win_rate", "mean"),
        ).reset_index()
        # Render
        b_disp = bucket.copy()
        b_disp["total_pnl"] = b_disp["total_pnl"].map(lambda v: f"${v:+,.2f}")
        b_disp["avg_pnl_per_cluster"] = b_disp["avg_pnl_per_cluster"].map(lambda v: f"${v:+,.2f}")
        b_disp["avg_n_matched"] = b_disp["avg_n_matched"].map(lambda v: f"{v:.1f}")
        b_disp["mean_win_rate"] = b_disp["mean_win_rate"].map(lambda v: f"{v*100:.1f}%")
        b_disp = b_disp.rename(columns={
            "bucket": "cluster size",
            "n_clusters": "n clusters",
            "total_pnl": "total cluster P&L",
            "avg_pnl_per_cluster": "avg / cluster",
            "avg_n_matched": "avg matched trades",
            "mean_win_rate": "mean WR",
        })
        bucket_summary = df_to_md(b_disp)
        cluster_summary = bucket_summary

    return f"""## §6 Inter-variant correlation and cluster outcomes

[`docs/v3_audit/correlation_matrix.csv`](docs/v3_audit/correlation_matrix.csv)
· [`docs/v3_audit/cluster_outcomes.csv`](docs/v3_audit/cluster_outcomes.csv)

### Pairwise daily-P&L correlation

Variants with `r ≥ {HIGH_CORR_THRESHOLD:.1f}` are functionally one strategy
for diversification purposes.

**Pairs at or above r = {HIGH_CORR_THRESHOLD:.1f}** ({len(pairs)}):

{chr(10).join(pair_lines) if pair_lines else '(none)'}

### Full matrix

{corr_section}

### Cluster outcomes — what happens when ≥3 variants fire same direction

The §0.6 cluster log identified 495 non-overlapping cluster events.
This rollup matches each cluster to the trades settled inside its
5-minute window and aggregates P&L.

{cluster_summary}

The cluster-size column maps to the hypothesis: the larger the
cluster, the more correlated the bet, the more swing in either
direction.
"""


def main() -> int:
    df = load_settled_trades()
    corr, pairs = correlation_analysis(df)
    cluster_df = cluster_outcomes(df)

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)

    # Save correlation matrix
    if not corr.empty:
        rows = []
        for sid in corr.index:
            row = {"variant": sid}
            for c in corr.columns:
                row[c] = float(corr.loc[sid, c])
            rows.append(row)
        write_csv(
            "correlation_matrix.csv", rows, ["variant"] + list(corr.columns),
        )

    # Save cluster outcomes
    if not cluster_df.empty:
        write_csv(
            "cluster_outcomes.csv",
            cluster_df.to_dict(orient="records"),
            list(cluster_df.columns),
        )

    md = render_md(corr, pairs, cluster_df)
    append_section(DOCS_DIR / "v3_audit.md", md)
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
