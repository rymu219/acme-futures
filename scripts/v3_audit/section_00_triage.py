"""Section 0 — operational triage.

Sub-steps:
  0.1 check-schema  — row count, distinct strategy_ids, distinct exit_reasons,
                      time range. Validates the DB shape before analysis.
  0.3 positions     — position-state table for every variant with a non-flat
                      heartbeat. Writes docs/v3_audit/position_state.csv.
  0.4 anchor        — characterize open trades near the anchor price (default
                      $7,374.50, configurable). Writes anchor_position.csv.
  0.5 bar1          — count occurrences of bar1_fast_fail per variant over the
                      last 7 days.
  0.6 clusters      — detect ≥3-variant same-direction clusters within a
                      5-minute window over the last 7 days. Writes
                      cluster_event_log.csv. Specifically highlights any
                      clusters at 19:44 / 19:54 / 20:00 / 20:06 CT today.

Default mode: paper. Default scope: last 7 days.

Usage:
    uv run python scripts/v3_audit/section_00_triage.py --check-schema
    uv run python scripts/v3_audit/section_00_triage.py --step 0.3
    uv run python scripts/v3_audit/section_00_triage.py --all
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any

from acme.runner import VARIANTS

# Robust imports whether invoked as a script or a module:
try:
    from scripts.v3_audit.db import (  # type: ignore
        AUDIT_DIR, CT, UTC, fetch_all_v3_trades, fetch_heartbeats,
        fetch_open_trades, fmt_ct, get_client, parse_iso, to_ct, write_csv,
    )
except ImportError:
    # Allow `uv run python scripts/v3_audit/section_00_triage.py` directly.
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.v3_audit.db import (  # type: ignore  # noqa: E402
        AUDIT_DIR, CT, UTC, fetch_all_v3_trades, fetch_heartbeats,
        fetch_open_trades, fmt_ct, get_client, parse_iso, to_ct, write_csv,
    )


VARIANT_BY_ID: dict[str, Any] = {v.strategy_id: v for v in VARIANTS}


def _variant_exit_rules(strategy_id: str) -> str:
    """Human-readable exit-rule summary for a variant, derived from
    src/acme/runner.py _VariantSpec flags."""
    v = VARIANT_BY_ID.get(strategy_id)
    if v is None:
        return "(unknown variant — no spec in src/acme/runner.py)"
    parts: list[str] = ["opposite_signal", "stop"]
    if v.enable_trailing_stop:
        parts.append(
            f"trailing_stop(be@{v.trail_be_lock_atr_mult}ATR, "
            f"trail={v.trail_atr_mult}ATR)"
        )
    if v.min_bars_before_opposite_exit > 0:
        parts.append(f"min_bars_before_opp={v.min_bars_before_opposite_exit}")
    if v.opposite_signal_armor_mfe_atr is not None:
        parts.append(f"armor(MFE≥{v.opposite_signal_armor_mfe_atr}ATR)")
    if v.enable_bar1_fast_fail:
        parts.append(f"bar1_fast_fail(MAE>MFE×{v.bar1_fast_fail_mae_mfe_ratio})")
    if v.enable_session_end_exit:
        parts.append("session_end")
    if v.enable_time_stop:
        parts.append("time_stop")
    return ", ".join(parts)


# ───────────────────────── 0.1 schema check ─────────────────────────────────


def check_schema() -> None:
    sb = get_client()
    print("\n=== 0.1 DB schema / shape ===\n")

    # Count rows by mode (with a paged head fetch to compute distincts).
    # supabase-py doesn't expose count without a separate call; we'll just
    # page the full table once for paper mode (that's the audit universe).
    rows = fetch_all_v3_trades(sb, mode="paper")
    print(f"ryan_spec_v3_trades (mode=paper): {len(rows):,} rows")

    if not rows:
        print("(no paper trades found — DB may be empty)")
        return

    bar_ts_vals = [r.get("bar_ts") for r in rows if r.get("bar_ts")]
    bar_ts_min = min(bar_ts_vals)
    bar_ts_max = max(bar_ts_vals)
    print(f"  time range: {fmt_ct(bar_ts_min)}  →  {fmt_ct(bar_ts_max)}")

    sids = Counter(r.get("strategy_id") or "(null)" for r in rows)
    print(f"\n  distinct strategy_ids ({len(sids)}):")
    for sid, n in sids.most_common():
        in_spec = "✓" if sid in VARIANT_BY_ID else "✗ NOT in runner.VARIANTS"
        print(f"    {sid:<22} {n:>6,} trades   {in_spec}")

    exits = Counter(r.get("exit_reason") or "(open)" for r in rows)
    print(f"\n  distinct exit_reasons ({len(exits)}):")
    for er, n in exits.most_common():
        print(f"    {er:<22} {n:>6,}")

    hbs = fetch_heartbeats(sb)
    print(f"\nruntime_heartbeats: {len(hbs)} services")
    states = Counter(h.get("position_state") or "(null)" for h in hbs)
    print(f"  position_state distribution: {dict(states)}")


# ───────────────────────── 0.3 position-state table ─────────────────────────


def position_state_table() -> None:
    sb = get_client()
    print("\n=== 0.3 position-state table ===\n")

    hbs = fetch_heartbeats(sb)
    hb_by_service = {h.get("service"): h for h in hbs}

    open_trades = fetch_open_trades(sb, mode="paper")
    open_by_sid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in open_trades:
        open_by_sid[t.get("strategy_id") or "(null)"].append(t)

    out_rows: list[dict[str, Any]] = []
    for sid, hb in sorted(hb_by_service.items()):
        state = (hb.get("position_state") or "").lower()
        if state in ("flat", "", None):
            continue
        opens = open_by_sid.get(sid, [])
        # Most-recent open trade for this variant
        opens_sorted = sorted(
            opens, key=lambda t: t.get("entry_ts") or "", reverse=True
        )
        latest = opens_sorted[0] if opens_sorted else {}
        out_rows.append({
            "strategy_id": sid,
            "heartbeat_state": state,
            "heartbeat_ts_ct": fmt_ct(hb.get("ts")),
            "heartbeat_age_min": _age_minutes(hb.get("ts")),
            "auth_ok": hb.get("auth_ok"),
            "consecutive_errors": hb.get("consecutive_errors"),
            "open_trade_count": len(opens),
            "latest_entry_ts_ct": fmt_ct(latest.get("entry_ts")) if latest else "",
            "latest_entry_price": latest.get("entry_price") if latest else None,
            "latest_direction": latest.get("direction") if latest else "",
            "stop_price": latest.get("stop_price") if latest else None,
            "atr_at_entry": latest.get("atr_at_entry") if latest else None,
            "cum_delta_at_entry": latest.get("cum_delta_at_entry") if latest else None,
            "variant_exit_rules": _variant_exit_rules(sid),
        })

    if not out_rows:
        print("(no variants currently in a non-flat position)")
        return

    # Print
    print(f"variants with non-flat heartbeat: {len(out_rows)}\n")
    for r in out_rows:
        print(
            f"  {r['strategy_id']:<20} {r['heartbeat_state']:<6}  "
            f"hb {r['heartbeat_age_min']}m ago  "
            f"opens={r['open_trade_count']}\n"
            f"    latest entry: {r['latest_entry_ts_ct']} @ "
            f"{r['latest_entry_price']} ({r['latest_direction']})  "
            f"stop={r['stop_price']}  ATR={r['atr_at_entry']}\n"
            f"    exit rules : {r['variant_exit_rules']}"
        )

    cols = [
        "strategy_id", "heartbeat_state", "heartbeat_ts_ct", "heartbeat_age_min",
        "auth_ok", "consecutive_errors", "open_trade_count",
        "latest_entry_ts_ct", "latest_entry_price", "latest_direction",
        "stop_price", "atr_at_entry", "cum_delta_at_entry",
        "variant_exit_rules",
    ]
    path = write_csv("position_state.csv", out_rows, cols)
    print(f"\n→ wrote {path.relative_to(path.parents[2])}")


# ───────────────────────── 0.4 anchor LONG analysis ─────────────────────────


def anchor_analysis(anchor_price: float, tolerance: float) -> None:
    sb = get_client()
    print(f"\n=== 0.4 anchor LONG @ ${anchor_price:.2f} (±${tolerance}) ===\n")

    open_trades = fetch_open_trades(sb, mode="paper")
    near: list[dict[str, Any]] = []
    for t in open_trades:
        ep = t.get("entry_price")
        if ep is None:
            continue
        try:
            ep = float(ep)
        except (TypeError, ValueError):
            continue
        if abs(ep - anchor_price) <= tolerance:
            near.append(t)

    if not near:
        print(f"(no open trades within ±${tolerance:.2f} of ${anchor_price:.2f})")
        return

    print(f"{len(near)} open trades within tolerance:\n")
    out_rows: list[dict[str, Any]] = []
    for t in sorted(near, key=lambda r: r.get("entry_ts") or ""):
        sid = t.get("strategy_id") or "(null)"
        row = {
            "strategy_id": sid,
            "entry_ts_ct": fmt_ct(t.get("entry_ts")),
            "entry_price": t.get("entry_price"),
            "direction": t.get("direction"),
            "stop_price": t.get("stop_price"),
            "atr_at_entry": t.get("atr_at_entry"),
            "cum_delta_at_entry": t.get("cum_delta_at_entry"),
            "bar_ts_ct": fmt_ct(t.get("bar_ts")),
            "trade_id": t.get("id"),
            "variant_exit_rules": _variant_exit_rules(sid),
            "regime_classifier": (
                getattr(VARIANT_BY_ID.get(sid), "regime_classifier_name", None)
                or "(none)"
            ),
            "entry_atr_ceiling": (
                getattr(VARIANT_BY_ID.get(sid), "entry_atr_ceiling", None)
            ),
        }
        out_rows.append(row)
        print(
            f"  {sid:<20} {row['entry_ts_ct']} @ {row['entry_price']} "
            f"({row['direction']:<5}) "
            f"stop={row['stop_price']} ATR={row['atr_at_entry']}\n"
            f"    cum_delta@entry={row['cum_delta_at_entry']}  "
            f"regime={row['regime_classifier']}  "
            f"atr_ceiling={row['entry_atr_ceiling']}\n"
            f"    exit rules: {row['variant_exit_rules']}"
        )

    # Group by (entry_ts, direction) to see if multiple variants entered on
    # the same bar — this is the cluster-failure signature in raw form.
    by_minute: dict[str, list[str]] = defaultdict(list)
    for t in near:
        m = (fmt_ct(t.get("entry_ts")) or "")[:16]  # truncate to minute
        by_minute[m].append(t.get("strategy_id") or "(null)")
    multi = {k: v for k, v in by_minute.items() if len(v) > 1}
    if multi:
        print(f"\n  multi-variant minutes ({len(multi)}):")
        for m, sids in sorted(multi.items()):
            print(f"    {m}: {len(sids):>2} variants → {', '.join(sorted(sids))}")

    cols = [
        "strategy_id", "entry_ts_ct", "entry_price", "direction",
        "stop_price", "atr_at_entry", "cum_delta_at_entry", "bar_ts_ct",
        "trade_id", "variant_exit_rules", "regime_classifier",
        "entry_atr_ceiling",
    ]
    path = write_csv("anchor_position.csv", out_rows, cols)
    print(f"\n→ wrote {path.relative_to(path.parents[2])}")


# ───────────────────────── 0.5 bar1_fast_fail count ─────────────────────────


def bar1_fast_fail_count(days: int = 7) -> None:
    sb = get_client()
    print(f"\n=== 0.5 bar1_fast_fail occurrences (last {days}d) ===\n")
    rows = fetch_all_v3_trades(sb, mode="paper", since_days=days)
    if not rows:
        print(f"(no paper trades in the last {days} days)")
        return

    fast_fails: Counter[str] = Counter()
    totals_per_sid: Counter[str] = Counter()
    for r in rows:
        sid = r.get("strategy_id") or "(null)"
        totals_per_sid[sid] += 1
        if r.get("exit_reason") == "bar1_fast_fail":
            fast_fails[sid] += 1

    enabled = {
        sid for sid, v in VARIANT_BY_ID.items()
        if getattr(v, "enable_bar1_fast_fail", False)
    }
    print(f"variants with enable_bar1_fast_fail=True: {sorted(enabled) or '(none)'}\n")

    out_rows: list[dict[str, Any]] = []
    print(f"  {'strategy_id':<22}{'enabled':<9}{'trades':>8}{'bar1_fast_fail':>18}{'%':>8}")
    for sid in sorted(totals_per_sid):
        n = totals_per_sid[sid]
        ff = fast_fails.get(sid, 0)
        pct = (ff / n * 100) if n else 0.0
        print(
            f"  {sid:<22}{('yes' if sid in enabled else 'no'):<9}"
            f"{n:>8,}{ff:>18,}{pct:>7.1f}%"
        )
        out_rows.append({
            "strategy_id": sid,
            "bar1_fast_fail_enabled": sid in enabled,
            "trades_7d": n,
            "bar1_fast_fail_exits_7d": ff,
            "pct_of_trades": round(pct, 2),
        })

    path = write_csv(
        "bar1_fast_fail.csv", out_rows,
        ["strategy_id", "bar1_fast_fail_enabled", "trades_7d",
         "bar1_fast_fail_exits_7d", "pct_of_trades"],
    )
    print(f"\n→ wrote {path.relative_to(path.parents[2])}")


# ───────────────────────── 0.6 cluster events ───────────────────────────────


def cluster_events(days: int = 7, window_min: int = 5, min_variants: int = 3,
                   target_day: str = "2026-05-11") -> None:
    sb = get_client()
    print(
        f"\n=== 0.6 cluster events (≥{min_variants} variants, same direction, "
        f"within {window_min}min) — last {days}d ===\n"
    )
    rows = fetch_all_v3_trades(sb, mode="paper", since_days=days)
    if not rows:
        print(f"(no paper trades in the last {days} days)")
        return

    # Build sorted (ts, direction, strategy_id) tuples for entries only.
    entries: list[tuple[datetime, str, str, int | None]] = []
    for r in rows:
        ets = parse_iso(r.get("entry_ts") or r.get("bar_ts"))
        if ets is None:
            continue
        direction = r.get("direction") or ""
        if direction not in ("long", "short"):
            continue
        sid = r.get("strategy_id") or "(null)"
        entries.append((ets, direction, sid, r.get("id")))
    entries.sort()

    # Non-overlapping sliding windows: each entry is consumed by at most one
    # cluster. Start at the earliest unconsumed entry; sweep forward
    # window_min minutes; collect same-direction entries from distinct
    # strategy_ids; consume all entries that participated. A new cluster
    # starts at the next unconsumed entry.
    cluster_log: list[dict[str, Any]] = []
    win = timedelta(minutes=window_min)
    i = 0
    while i < len(entries):
        t0, d0, s0, _ = entries[i]
        group_sids: dict[str, datetime] = {s0: t0}
        consumed_idx: list[int] = [i]
        j = i + 1
        while j < len(entries) and entries[j][0] - t0 <= win:
            t1, d1, s1, _ = entries[j]
            if d1 == d0 and s1 not in group_sids:
                group_sids[s1] = t1
                consumed_idx.append(j)
            j += 1
        if len(group_sids) >= min_variants:
            cluster_log.append({
                "anchor_ts_ct": t0.astimezone(CT).strftime("%Y-%m-%d %H:%M:%S CT"),
                "direction": d0,
                "n_variants": len(group_sids),
                "variant_list": ",".join(sorted(group_sids)),
                "min_entry_ts_ct": min(group_sids.values()).astimezone(CT).strftime("%H:%M:%S"),
                "max_entry_ts_ct": max(group_sids.values()).astimezone(CT).strftime("%H:%M:%S"),
            })
            # Consume all participating entries
            i = consumed_idx[-1] + 1
        else:
            i += 1

    print(f"total cluster events found: {len(cluster_log)}")

    size_buckets: Counter[str] = Counter()
    for ev in cluster_log:
        n = ev["n_variants"]
        b = "3-5" if n <= 5 else ("6-10" if n <= 10 else "11+")
        size_buckets[b] += 1
    print(f"  by size bucket: {dict(size_buckets)}")

    # Highlight the four target times on target_day
    target_minutes = {"19:44", "19:54", "20:00", "20:06"}
    today_hits = [
        ev for ev in cluster_log
        if ev["anchor_ts_ct"].startswith(target_day)
        and ev["anchor_ts_ct"][11:16] in target_minutes
    ]
    print(f"\ntarget-day events at 19:44/19:54/20:00/20:06 CT on {target_day}:")
    if not today_hits:
        # Widen: any cluster on the target day around those minutes
        same_day = [
            ev for ev in cluster_log if ev["anchor_ts_ct"].startswith(target_day)
        ]
        print(f"  (no exact-minute matches; {len(same_day)} clusters on {target_day})")
        for ev in same_day[:20]:
            print(
                f"  {ev['anchor_ts_ct']}  {ev['direction']:<5} "
                f"{ev['n_variants']:>2} variants: {ev['variant_list']}"
            )
    else:
        for ev in today_hits:
            print(
                f"  {ev['anchor_ts_ct']}  {ev['direction']:<5} "
                f"{ev['n_variants']:>2} variants: {ev['variant_list']}"
            )

    cols = [
        "anchor_ts_ct", "direction", "n_variants", "variant_list",
        "min_entry_ts_ct", "max_entry_ts_ct",
    ]
    path = write_csv("cluster_event_log.csv", cluster_log, cols)
    print(f"\n→ wrote {path.relative_to(path.parents[2])}")


# ───────────────────────── utility ─────────────────────────────────────────


def _age_minutes(ts: str | None) -> int | None:
    d = parse_iso(ts)
    if d is None:
        return None
    return int((datetime.now(UTC) - d).total_seconds() // 60)


# ───────────────────────── CLI ──────────────────────────────────────────────


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--check-schema", action="store_true",
                   help="Run 0.1 only (DB shape).")
    p.add_argument("--step", default=None,
                   choices=("0.1", "0.3", "0.4", "0.5", "0.6"),
                   help="Run one step in isolation.")
    p.add_argument("--all", action="store_true",
                   help="Run every step in order.")
    p.add_argument("--anchor-price", type=float, default=7374.50,
                   help="Anchor price for 0.4 (default $7,374.50).")
    p.add_argument("--anchor-tolerance", type=float, default=0.50,
                   help="±dollars around anchor price (default $0.50).")
    p.add_argument("--days", type=int, default=7,
                   help="Lookback window in days for 0.5 / 0.6 (default 7).")
    args = p.parse_args()

    if args.check_schema or args.step == "0.1":
        check_schema()
        if not args.all:
            return 0
    if args.step == "0.3" or args.all:
        position_state_table()
        if args.step == "0.3":
            return 0
    if args.step == "0.4" or args.all:
        anchor_analysis(args.anchor_price, args.anchor_tolerance)
        if args.step == "0.4":
            return 0
    if args.step == "0.5" or args.all:
        bar1_fast_fail_count(args.days)
        if args.step == "0.5":
            return 0
    if args.step == "0.6" or args.all:
        cluster_events(days=args.days)
        if args.step == "0.6":
            return 0

    if not (args.check_schema or args.step or args.all):
        p.print_help()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
