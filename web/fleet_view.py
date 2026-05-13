"""New-fleet dashboard.

Renders the home page for the Part-2 fleet: IGNITION / SESSION / REGIME /
BOUNDARY running through the classic conductor in SHADOW state.

Read-only HTML view. Five panels, all on one scroll:

  1. Header — heartbeat per strategy, current bar, live-trade pill.
  2. Live position — single position (conductor arbitrates), which
     strategy owns it, entry, age, unrealised P&L.
  3. Per-strategy cards — state, score, n, net P&L, WR, PF, Sharpe.
  4. Promotion-gate dashboard — distance to PILOT (≥0.55) and LIVE (≥0.65).
  5. Hour-of-day P&L heatmap — bucketed by entry hour CT.
  6. Bars-held distribution — per-strategy histogram with 2-bar
     benchmark (audit §3).
  7. Recent trades — compact table of latest dry_run_close events.

Reads from Supabase tables: `strategies`, `strategy_perf_snapshot`,
`broker_events`, `runtime_heartbeats`. Honest framing — no `HALT` or
`/200` labels (the audit revealed both were misleading).

Mounted at `/` by web/app.py.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
UTC = ZoneInfo("UTC")

# Strategies on the new fleet (Part-2). Order is display order in the UI.
FLEET = ["boundary", "overnight_drift", "gap_fill"]

# PerfTracker thresholds from src/acme/perf/scoring.py
PILOT_THRESHOLD = 0.55
LIVE_THRESHOLD = 0.65


# Time-of-day buckets for the UI filter. Keys map to a set of hours
# (CT). 'all' is the default and means no filter. Order matters — used
# as the chip render order at the top of the page.
TIME_BUCKETS: dict[str, dict] = {
    "all":            {"label": "All hours",         "hours": None},
    "audit_winners":  {"label": "Audit winners",     "hours": {3, 4, 8, 9, 17}},
    "audit_losers":   {"label": "Audit losers",      "hours": {11, 12, 13, 14, 15}},
    "europe":         {"label": "Europe 03–05",      "hours": {3, 4}},
    "rth_am":         {"label": "RTH AM 08–09",      "hours": {8, 9}},
    "rth_lunch":      {"label": "RTH lunch 10–12",   "hours": {10, 11, 12}},
    "rth_pm":         {"label": "RTH PM 13–15",      "hours": {13, 14, 15}},
    "overnight":      {"label": "Overnight 18–07",
                        "hours": {18, 19, 20, 21, 22, 23, 0, 1, 2, 5, 6, 7}},
}


def _bucket_hours(bucket: str | None) -> set[int] | None:
    """Returns the hour set for a bucket, or None for 'all' / unknown."""
    if not bucket or bucket == "all":
        return None
    b = TIME_BUCKETS.get(bucket)
    if not b:
        return None
    return b["hours"]


def _filter_closes_by_bucket(
    closes: list[dict[str, Any]], bucket: str | None
) -> list[dict[str, Any]]:
    hours = _bucket_hours(bucket)
    if hours is None:
        return closes
    out = []
    for c in closes:
        ts = c.get("occurred_at")
        if not ts:
            continue
        try:
            d = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(CT)
        except Exception:
            continue
        if d.hour in hours:
            out.append(c)
    return out

# Heartbeat staleness — same conventions as the v3 view.
HB_LIVE_MAX_S = 300        # 5 min (new fleet's 2-min bar + buffer)
HB_OFFLINE_MIN_S = 900     # 15 min

_STATE_BG = {
    "LIVE": "#16a34a",
    "PILOT": "#0891b2",
    "SHADOW": "#3b82f6",
    "BENCH": "#a16207",
    "RETIRED": "#7f1d1d",
}


# ─────────────────────────── small helpers ──────────────────────────


def _money(n: float | None) -> str:
    if n is None:
        return "—"
    sign = "−" if n < 0 else ""
    n = abs(n)
    if n >= 1000:
        return f"{sign}${n:,.0f}"
    return f"{sign}${n:.2f}"


def _pct(n: float | None) -> str:
    if n is None:
        return "—"
    return f"{n*100:.0f}%"


def _ago(ts_iso: str | None) -> str:
    if not ts_iso:
        return "?"
    try:
        d = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    except Exception:
        return "?"
    s = int((datetime.now(UTC) - d).total_seconds())
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _ct_str(ts_iso: str | None) -> str:
    if not ts_iso:
        return "—"
    try:
        d = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        return d.astimezone(CT).strftime("%H:%M:%S CT")
    except Exception:
        return "—"


# ─────────────────────────── data fetchers ──────────────────────────


def _fetch_heartbeats(sb) -> dict[str, dict[str, Any]]:
    """Heartbeats for the 4 fleet strategies only."""
    try:
        res = sb.table("runtime_heartbeats").select("*").in_("service", FLEET).execute()
    except Exception:
        return {}
    return {h["service"]: h for h in (res.data or [])}


def _fetch_perf_snapshots(sb) -> dict[str, dict[str, Any]]:
    """Latest perf snapshot per fleet strategy."""
    out: dict[str, dict[str, Any]] = {}
    for name in FLEET:
        try:
            res = (
                sb.table("strategy_perf_snapshot")
                .select("*")
                .eq("strategy", name)
                .order("id", desc=True)
                .limit(1)
                .execute()
            )
            data = (res.data or [None])[0]
            if data:
                out[name] = data
        except Exception:
            continue
    return out


def _fetch_strategies(sb) -> dict[str, dict[str, Any]]:
    """The `strategies` table rows (state / score / tier / notes)."""
    try:
        res = sb.table("strategies").select("*").in_("name", FLEET).execute()
    except Exception:
        return {}
    return {s["name"]: s for s in (res.data or [])}


def _fetch_recent_closes(sb, *, days: int = 7, limit: int = 1000) -> list[dict[str, Any]]:
    """`dry_run_close` events for the fleet in the last `days` days."""
    floor = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    try:
        res = (
            sb.table("broker_events").select("*")
            .eq("kind", "dry_run_close").in_("strategy", FLEET)
            .gte("occurred_at", floor)
            .order("id", desc=True).limit(limit).execute()
        )
    except Exception:
        return []
    return res.data or []


def _fetch_kill_switch_state(sb) -> dict[str, Any]:
    """Latest kill-switch event from operator_events. Returns
    {'active': bool, 'ts': iso or None, 'by': str or None}."""
    try:
        res = (
            sb.table("operator_events").select("*")
            .in_("kind", ["kill_switch_activated", "kill_switch_cleared"])
            .order("occurred_at", desc=True).limit(1).execute()
        )
        rows = res.data or []
    except Exception:
        return {"active": False, "ts": None, "by": None}
    if not rows:
        return {"active": False, "ts": None, "by": None}
    row = rows[0]
    return {
        "active": row.get("kind") == "kill_switch_activated",
        "ts": row.get("occurred_at"),
        "by": (row.get("raw") or {}).get("by") if isinstance(row.get("raw"), dict) else None,
    }


def _today_session_closes(closes: list[dict]) -> list[dict]:
    """Closes that exited during the current CT trading-day session.
    Trade-date rolls at 17:00 CT (Globex open), matching trading_date_ct."""
    now_ct = datetime.now(UTC).astimezone(CT)
    # Find the start of the current trading day.
    if now_ct.time() >= time(17, 0):
        td_start = datetime.combine(now_ct.date(), time(17, 0), tzinfo=CT)
    else:
        td_start = datetime.combine(
            now_ct.date() - timedelta(days=1), time(17, 0), tzinfo=CT
        )
    out = []
    for c in closes:
        ts = c.get("occurred_at")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(CT)
        except Exception:
            continue
        if dt >= td_start:
            out.append(c)
    return out


# ─────────────────────────── Topstep MLL tracker ────────────────────


_TOPSTEP_DLL = 1_000.0
_TOPSTEP_MLL = 2_000.0


def _mll_color(distance: float, limit: float) -> str:
    """Distance-to-limit → color. green=safe (>50% away), yellow=mid,
    red=within 20% of limit."""
    if distance >= 0.5 * limit:
        return "#15803d"  # green
    if distance >= 0.2 * limit:
        return "#d97706"  # yellow
    return "#b91c1c"      # red


def _render_mll_tracker(closes: list[dict]) -> str:
    """Topstep DLL+MLL header. Session P&L is sum of today's CT-session
    realized closes. DLL distance = $1000 − today's drawdown. MLL
    distance uses today's session drawdown as a simple proxy for the
    trailing-MLL exposure (real trailing-MLL math needs cross-session
    peak tracking which lives in the runner)."""
    todays = _today_session_closes(closes)

    # Build per-close P&L stream (chronological) to compute the
    # session's intraday drawdown.
    pnls = []
    for c in sorted(todays, key=lambda r: r.get("occurred_at") or ""):
        raw = c.get("raw") or {}
        pnls.append(float(raw.get("net_pnl") or 0.0))
    session_pnl = sum(pnls)

    # Intraday drawdown: peak running sum − current running sum
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        running += p
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)

    # DLL exposure tracks intraday loss vs $1K
    intraday_loss = max(0.0, -session_pnl) if session_pnl < 0 else 0.0
    dll_distance = max(0.0, _TOPSTEP_DLL - intraday_loss)
    mll_distance = max(0.0, _TOPSTEP_MLL - max_dd)

    pnl_color = "#15803d" if session_pnl > 0 else "#b91c1c" if session_pnl < 0 else "#475569"
    dll_color = _mll_color(dll_distance, _TOPSTEP_DLL)
    mll_color = _mll_color(mll_distance, _TOPSTEP_MLL)

    return f"""
  <section class="mll-tracker">
    <div class="mll-card">
      <div class="mll-label dim mini">SESSION P&amp;L</div>
      <div class="mll-num mono" style="color:{pnl_color};">${session_pnl:+,.2f}</div>
      <div class="mll-sub dim mini">{len(pnls)} closed today</div>
    </div>
    <div class="mll-card">
      <div class="mll-label dim mini">DLL DISTANCE ($1,000 limit)</div>
      <div class="mll-num mono" style="color:{dll_color};">${dll_distance:,.0f}</div>
      <div class="mll-sub dim mini">intraday loss ${intraday_loss:,.2f}</div>
    </div>
    <div class="mll-card">
      <div class="mll-label dim mini">TRAILING MLL ($2,000 limit)</div>
      <div class="mll-num mono" style="color:{mll_color};">${mll_distance:,.0f}</div>
      <div class="mll-sub dim mini">peak-to-trough today ${max_dd:,.2f}</div>
    </div>
  </section>
"""


# ─────────────────────────── kill switch ─────────────────────────────


def _render_kill_switch(ks: dict[str, Any], token: str | None) -> str:
    """Big red EMERGENCY FLAT button + green RESUME button. Two-click
    safety via JS confirm(). Active/Inactive state from operator_events."""
    token_q = f"&token={token}" if token else ""
    active = bool(ks.get("active"))
    state_label = "KILL SWITCH ACTIVE" if active else "KILL SWITCH INACTIVE"
    state_color = "#b91c1c" if active else "#15803d"
    state_bg = "#fee2e2" if active else "#dcfce7"

    ts_line = ""
    if ks.get("ts"):
        ts_line = f"<span class='dim mini'>since {_ago(ks['ts'])}</span>"

    if active:
        # Show resume button only
        action_html = f"""
        <a href="/kill-switch?action=clear{token_q}"
           class="ks-btn ks-btn-resume"
           onclick="return confirm('Clear the kill switch and resume trading?');">
          RESUME TRADING
        </a>
        """
    else:
        # Show emergency-flat button only
        action_html = f"""
        <a href="/kill-switch?action=activate{token_q}"
           class="ks-btn ks-btn-flat"
           onclick="return confirm('EMERGENCY FLAT — force-close ALL positions immediately. Are you sure?');">
          EMERGENCY FLAT — ALL POSITIONS
        </a>
        """

    return f"""
  <section class="kill-switch-row">
    <div class="ks-status" style="background:{state_bg};color:{state_color};">
      <span class="ks-status-label">{state_label}</span>
      {ts_line}
    </div>
    <div class="ks-action">{action_html}</div>
  </section>
"""


# ─────────────────────────── header ─────────────────────────────────


def _heartbeat_status(hb: dict | None) -> tuple[str, str, str]:
    """Returns (label, color, sub-text). label ∈ {LIVE, STALE, OFFLINE, UNKNOWN}."""
    if not hb:
        return ("UNKNOWN", "#64748b", "no heartbeat")
    ts = hb.get("ts")
    if not ts:
        return ("UNKNOWN", "#64748b", "no ts")
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return ("UNKNOWN", "#64748b", "bad ts")
    age_s = int((datetime.now(UTC) - d).total_seconds())
    if age_s < HB_LIVE_MAX_S:
        return ("LIVE", "#16a34a", f"{age_s}s ago")
    if age_s < HB_OFFLINE_MIN_S:
        return ("STALE", "#f59e0b", _ago(ts))
    return ("OFFLINE", "#dc2626", _ago(ts))


def _render_header(heartbeats: dict[str, dict]) -> str:
    """Top status bar — one pill per strategy + last-bar across fleet."""
    pills = []
    last_bar_ts: str | None = None
    for name in FLEET:
        hb = heartbeats.get(name)
        label, color, sub = _heartbeat_status(hb)
        pos_state = (hb or {}).get("position_state") or "flat"
        if pos_state != "flat":
            pos_chip = (
                f"<span class='pill mini' style='background:#1e3a8a;color:white;"
                f"margin-left:6px;'>{pos_state.upper()}</span>"
            )
        else:
            pos_chip = ""
        pills.append(
            f"<div class='hb-pill' style='border-color:{color};'>"
            f"<div class='hb-name'>{name}</div>"
            f"<div class='hb-status' style='color:{color};'>"
            f"{label}{pos_chip}</div>"
            f"<div class='hb-sub dim'>{sub}</div>"
            f"</div>"
        )
        lb = (hb or {}).get("last_bar_ts")
        if lb and (last_bar_ts is None or lb > last_bar_ts):
            last_bar_ts = lb

    bar_age = _ago(last_bar_ts) if last_bar_ts else "—"
    bar_ct = _ct_str(last_bar_ts) if last_bar_ts else "—"
    return f"""
  <header class="topbar">
    <div class="brand">
      <span class="title">Acme Futures · New Fleet</span>
      <span class="dim sub">SHADOW · classic conductor · single position</span>
    </div>
    <div class="last-bar">
      <span class="dim">last bar</span>
      <span class="mono">{bar_ct}</span>
      <span class="dim mini">({bar_age})</span>
    </div>
  </header>
  <section class="hb-row">
    {''.join(pills)}
  </section>
"""


# ─────────────────────────── live position ──────────────────────────


def _render_position_panel(heartbeats: dict[str, dict]) -> str:
    """Single-position view: which strategy currently holds, side, age.

    The classic conductor arbitrates one position across the fleet, so
    at most one strategy should report non-flat at any time. If multiple
    do, surface it as a warning (state desync).
    """
    in_pos = [(name, hb) for name, hb in heartbeats.items()
              if (hb.get("position_state") or "flat") != "flat"]

    if not in_pos:
        return """
  <section class="card">
    <div class="card-title">Live Position</div>
    <div class="empty">no position held — fleet is flat</div>
  </section>
"""

    if len(in_pos) > 1:
        names = ", ".join(n for n, _ in in_pos)
        warn = (f"<div class='warn'>⚠ {len(in_pos)} strategies report "
                f"non-flat — possible state desync: {names}</div>")
    else:
        warn = ""

    rows = []
    for name, hb in in_pos:
        state = hb.get("position_state", "?")
        extra = hb.get("extra") or {}
        contract = extra.get("contract_id") or "MES"
        rows.append(f"""
    <div class="pos">
      <span class="pos-name">{name}</span>
      <span class="pos-side"
            style="color:{'#16a34a' if state == 'long' else '#dc2626'};">
            {state.upper()}</span>
      <span class="pos-contract dim">{contract}</span>
      <span class="pos-age mini dim">{_ago(hb.get('ts'))}</span>
    </div>
""")
    return f"""
  <section class="card">
    <div class="card-title">Live Position</div>
    {warn}
    <div class="pos-list">{''.join(rows)}</div>
  </section>
"""


# ─────────────────────────── strategy cards ─────────────────────────


def _strategy_metrics(name: str, strategies: dict, snaps: dict) -> dict[str, Any]:
    s = strategies.get(name) or {}
    sn = snaps.get(name) or {}
    return {
        "name": name,
        "state": s.get("state") or "?",
        "tier": int(s.get("tier") or 2),
        "score": float(s.get("score") or 0),
        "n_trades": int(sn.get("n_trades") or 0),
        "net_pnl": float(sn.get("net_pnl") or 0),
        "win_rate": float(sn.get("win_rate") or 0),
        "pf": sn.get("profit_factor"),
        "sharpe": float(sn.get("sharpe") or 0),
        "dd": float(sn.get("max_drawdown") or 0),
    }


def _compute_metrics_from_closes(
    closes: list[dict], strategy: str,
) -> dict[str, Any]:
    """On-the-fly metrics for a strategy from a (possibly bucket-filtered)
    list of dry_run_close events. Used when the bucket selector is
    non-default; avoids the snapshot-vs-bucket mismatch.

    Returns the subset of metrics computable cheaply from closes:
    n_trades, net_pnl, win_rate, profit_factor. PF / Sharpe / MaxDD
    that require equity-curve walking are left to the snapshot path."""
    n = 0
    net = 0.0
    wins = 0
    gross_win = 0.0
    gross_loss = 0.0
    for c in closes:
        if c.get("strategy") != strategy:
            continue
        raw = c.get("raw") or {}
        pnl = float(raw.get("net_pnl") or 0)
        n += 1
        net += pnl
        if pnl > 0:
            gross_win += pnl
            wins += 1
        elif pnl < 0:
            gross_loss += -pnl
    if n == 0:
        return {"n_trades": 0, "net_pnl": 0.0, "win_rate": 0.0,
                "profit_factor": None}
    return {
        "n_trades": n,
        "net_pnl": net,
        "win_rate": wins / n,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (
            float("inf") if gross_win > 0 else None
        ),
    }


def _render_bucket_selector(current: str, token: str | None) -> str:
    """Chips at the top — clicking one re-renders the page filtered by
    that hour set."""
    token_q = f"&token={token}" if token else ""
    chips = []
    for key, b in TIME_BUCKETS.items():
        active = (key == current) or (current == "all" and key == "all")
        bg = "#0891b2" if active else "#f1f5f9"
        color = "white" if active else "var(--dim-1)"
        chips.append(
            f"<a class='bucket-chip' "
            f"style='background:{bg};color:{color};' "
            f"href='/?bucket={key}{token_q}'>{b['label']}</a>"
        )
    return f"""
  <section class="bucket-bar">
    <span class="dim mini">filter by hour bucket (CT):</span>
    {''.join(chips)}
  </section>
"""


def _render_strategy_cards(strategies: dict, snaps: dict,
                            closes_in_bucket: list[dict] | None = None,
                            bucket: str = "all") -> str:
    cards = []
    use_bucket = (bucket != "all" and closes_in_bucket is not None)
    for name in FLEET:
        m = _strategy_metrics(name, strategies, snaps)
        if use_bucket:
            # Replace the snapshot-derived metrics with bucket-filtered
            # ones. State / score stay from the strategies-row source of
            # truth — those are lifecycle-level, not bucket-level.
            bm = _compute_metrics_from_closes(closes_in_bucket, name)
            m["n_trades"] = bm["n_trades"]
            m["net_pnl"] = bm["net_pnl"]
            m["win_rate"] = bm["win_rate"]
            m["pf"] = bm["profit_factor"]
            # Sharpe / MaxDD can't be recomputed cheaply from closes;
            # leave the snapshot values in but they'll look stale for
            # bucket views — caller can ignore.
        state_bg = _STATE_BG.get(m["state"], "#334155")
        pnl_color = (
            "#16a34a" if m["net_pnl"] > 0
            else "#ef4444" if m["net_pnl"] < 0 else "#e2e8f0"
        )
        pf = f"{m['pf']:.2f}" if m["pf"] is not None else "—"
        cards.append(f"""
    <div class="strat-card">
      <div class="strat-head">
        <span class="strat-name">{name}</span>
        <span class="pill mini" style="background:{state_bg};color:white;">
          {m['state']}</span>
      </div>
      <div class="strat-pnl mono" style="color:{pnl_color};">
        {_money(m['net_pnl'])}
      </div>
      <div class="strat-stats dim mono">
        <span>n={m['n_trades']}</span>
        <span>WR {_pct(m['win_rate'])}</span>
        <span>PF {pf}</span>
        <span>SR {m['sharpe']:.2f}</span>
      </div>
      <div class="strat-dd mini dim mono">
        max DD {_money(m['dd'])}
      </div>
    </div>
""")
    label = TIME_BUCKETS.get(bucket, {}).get("label", "All hours")
    title_suffix = (
        f"<span class='dim mini'> · filtered to {label}</span>"
        if use_bucket else "<span class='dim mini'> · latest snapshot</span>"
    )
    return f"""
  <section class="card">
    <div class="card-title">Strategy Performance{title_suffix}</div>
    <div class="strat-grid">{''.join(cards)}</div>
  </section>
"""


# ─────────────────────────── promotion gate ─────────────────────────


def _render_promotion_gate(strategies: dict) -> str:
    rows = []
    for name in FLEET:
        s = strategies.get(name) or {}
        score = float(s.get("score") or 0)
        state = s.get("state") or "?"
        # Distance bar: 0 → 1 progress toward PILOT, then 1 → 2 toward LIVE.
        if score < PILOT_THRESHOLD:
            pct = (score / PILOT_THRESHOLD) * 50
            tgt = f"to PILOT (≥{PILOT_THRESHOLD:.2f}): +{(PILOT_THRESHOLD-score):.3f}"
            color = "#3b82f6"
        elif score < LIVE_THRESHOLD:
            pct = 50 + ((score - PILOT_THRESHOLD) /
                        (LIVE_THRESHOLD - PILOT_THRESHOLD)) * 50
            tgt = f"to LIVE (≥{LIVE_THRESHOLD:.2f}): +{(LIVE_THRESHOLD-score):.3f}"
            color = "#0891b2"
        else:
            pct = 100
            tgt = "LIVE-eligible"
            color = "#16a34a"
        pct = max(0, min(100, pct))
        rows.append(f"""
    <div class="gate-row">
      <span class="gate-name">{name}</span>
      <span class="gate-state pill mini"
            style="background:{_STATE_BG.get(state,'#334155')};color:white;">
            {state}</span>
      <div class="gate-bar"><div class="gate-fill"
           style="width:{pct:.0f}%;background:{color};"></div></div>
      <span class="gate-score mono">{score:.3f}</span>
      <span class="gate-target dim mini">{tgt}</span>
    </div>
""")
    return f"""
  <section class="card">
    <div class="card-title">Promotion Gate
      <span class="dim mini">PILOT ≥ {PILOT_THRESHOLD} · LIVE ≥ {LIVE_THRESHOLD}</span>
    </div>
    <div class="gate-list">{''.join(rows)}</div>
  </section>
"""


# ─────────────────────────── hour-of-day heatmap ────────────────────


def _bucket_closes_by_hour(closes: list[dict]) -> dict[int, dict[str, float]]:
    """Returns {hour_ct: {n, net_pnl}} aggregated across the fleet."""
    out: dict[int, dict[str, float]] = defaultdict(
        lambda: {"n": 0.0, "net_pnl": 0.0}
    )
    for c in closes:
        ts = c.get("occurred_at")
        if not ts:
            continue
        try:
            d = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(CT)
        except Exception:
            continue
        raw = c.get("raw") or {}
        pnl = float(raw.get("net_pnl") or 0)
        out[d.hour]["n"] += 1
        out[d.hour]["net_pnl"] += pnl
    return dict(out)


def _render_hour_heatmap(closes: list[dict], selected_bucket: str = "all") -> str:
    """Always shows all 24 hours. The selected bucket gets a stronger
    border treatment so you can see which hours your filter covers."""
    buckets = _bucket_closes_by_hour(closes)
    selected_hours = _bucket_hours(selected_bucket) or set()
    if not buckets:
        return """
  <section class="card">
    <div class="card-title">Hour-of-day P&amp;L
      <span class="dim mini">audit §2 reference: 03–05 CT, 08–09 CT win</span>
    </div>
    <div class="empty">no closed trades yet</div>
  </section>
"""
    # Determine min/max for color scaling
    pnls = [v["net_pnl"] for v in buckets.values()]
    max_abs = max((abs(p) for p in pnls), default=1.0) or 1.0

    cells = []
    for h in range(24):
        v = buckets.get(h)
        if v is None or v["n"] == 0:
            cells.append(
                f"<div class='hh-cell empty' title='{h:02d}:00 CT — no trades'>"
                f"<div class='hh-hour'>{h:02d}</div>"
                f"<div class='hh-pnl dim'>—</div>"
                f"</div>"
            )
            continue
        intensity = min(1.0, abs(v["net_pnl"]) / max_abs)
        if v["net_pnl"] > 0:
            bg = f"rgba(22,163,74,{0.10 + 0.60*intensity:.2f})"
        elif v["net_pnl"] < 0:
            bg = f"rgba(220,38,38,{0.10 + 0.60*intensity:.2f})"
        else:
            bg = "transparent"
        # Audit-winning hours get a green border accent; selected bucket
        # gets a yellow halo (rendered as a wider border via box-shadow).
        win_hours = {3, 4, 8, 9, 17}
        loss_hours = {11, 12, 13, 14, 15}
        border = "#16a34a" if h in win_hours else (
            "#dc2626" if h in loss_hours else "transparent")
        halo = ("box-shadow: 0 0 0 2px #fbbf24 inset;"
                if h in selected_hours else "")
        n_cell = int(v["n"])
        pnl_cell = v["net_pnl"]
        title = f"{h:02d}:00 CT — n={n_cell} net=${pnl_cell:.2f}"
        cells.append(
            f"<div class='hh-cell' style='background:{bg};border-color:{border};{halo}' "
            f"title='{title}'>"
            f"<div class='hh-hour'>{h:02d}</div>"
            f"<div class='hh-pnl mono'>{_money(pnl_cell)}</div>"
            f"<div class='hh-n dim mini'>n={n_cell}</div>"
            f"</div>"
        )
    return f"""
  <section class="card">
    <div class="card-title">Hour-of-day P&amp;L (CT)
      <span class="dim mini">green border = audit-winning hours · red = audit-losing</span>
    </div>
    <div class="hh-grid">{''.join(cells)}</div>
  </section>
"""


# ─────────────────────────── bars-held distribution ─────────────────


_BARS_BUCKETS = [
    ("1", lambda b: b == 1),
    ("2", lambda b: b == 2),
    ("3", lambda b: b == 3),
    ("4-6", lambda b: 4 <= b <= 6),
    ("7-10", lambda b: 7 <= b <= 10),
    ("11+", lambda b: b >= 11),
]


def _bars_held_for(close_row: dict) -> int | None:
    raw = close_row.get("raw") or {}
    mins = raw.get("bars_held_minutes")
    if mins is None:
        return None
    # 2-min bars
    return max(1, int(round(int(mins) / 2)))


def _render_bars_held(closes: list[dict]) -> str:
    """Histogram of bars_held per strategy. The audit's load-bearing
    finding: 1-bar exits destroy P&L (PF 0.18); ≥2-bar holds win
    (PF 6.14). Watch IGNITION/REGIME closely to see whether the min-2
    rule is actually keeping bar-1 exits out of the data."""
    by_strat: dict[str, dict[str, int]] = defaultdict(
        lambda: {b[0]: 0 for b in _BARS_BUCKETS}
    )
    totals: dict[str, int] = defaultdict(int)
    no_bars_held = 0
    for c in closes:
        name = c.get("strategy") or "?"
        if name not in FLEET:
            continue
        bh = _bars_held_for(c)
        if bh is None:
            no_bars_held += 1
            continue
        for bucket_name, fn in _BARS_BUCKETS:
            if fn(bh):
                by_strat[name][bucket_name] += 1
                totals[name] += 1
                break

    if not totals:
        return """
  <section class="card">
    <div class="card-title">Bars-held Distribution
      <span class="dim mini">audit §3: keep 1-bar bucket near zero</span>
    </div>
    <div class="empty">waiting for closed trades…</div>
  </section>
"""

    cols = []
    for name in FLEET:
        if totals.get(name, 0) == 0:
            cols.append(f"""
    <div class="bh-col empty">
      <div class="bh-name">{name}</div>
      <div class="dim mini">no trades</div>
    </div>
""")
            continue
        bars = []
        for bucket_name, _ in _BARS_BUCKETS:
            count = by_strat[name][bucket_name]
            pct = count / totals[name] * 100
            # Bar-1 is highlighted red as the audit's anti-pattern
            color = "#ef4444" if bucket_name == "1" else "#3b82f6"
            label = f"{bucket_name}: {count}" if count else ""
            bars.append(
                f"<div class='bh-bar'>"
                f"<div class='bh-fill' style='height:{pct:.0f}%;background:{color};'>"
                f"</div>"
                f"<div class='bh-label dim mini'>{label}</div>"
                f"</div>"
            )
        bar1_pct = by_strat[name]["1"] / totals[name] * 100
        warn = ("⚠ " if bar1_pct > 20 else "") + f"{bar1_pct:.0f}% bar-1"
        warn_color = "#ef4444" if bar1_pct > 20 else "#94a3b8"
        cols.append(f"""
    <div class="bh-col">
      <div class="bh-name">{name}</div>
      <div class="bh-bars">{''.join(bars)}</div>
      <div class="bh-warn mini" style="color:{warn_color};">{warn}</div>
      <div class="dim mini">n={totals[name]}</div>
    </div>
""")
    note = ""
    if no_bars_held:
        note = (f'<div class="dim mini" style="margin-top:8px;">'
                f'{no_bars_held} older closes lack bars_held_minutes — '
                f'new field added 2026-05-11; pre-existing rows will not '
                f'show here.</div>')
    return f"""
  <section class="card">
    <div class="card-title">Bars-held Distribution
      <span class="dim mini">audit §3: keep bar-1 exits below 20%</span>
    </div>
    <div class="bh-grid">{''.join(cols)}</div>
    {note}
  </section>
"""


# ─────────────────────────── recent trades ──────────────────────────


def _render_recent_trades(closes: list[dict], limit: int = 20) -> str:
    if not closes:
        return """
  <section class="card">
    <div class="card-title">Recent Closes</div>
    <div class="empty">waiting for first dry_run_close…</div>
  </section>
"""
    rows = []
    for c in closes[:limit]:
        raw = c.get("raw") or {}
        net = float(raw.get("net_pnl") or 0)
        pnl_color = "#16a34a" if net > 0 else "#ef4444" if net < 0 else "#94a3b8"
        outcome = raw.get("outcome") or "?"
        bh_m = raw.get("bars_held_minutes")
        bh_str = f"{int(bh_m)//2}b" if bh_m else "—"
        rows.append(f"""
      <tr>
        <td class="mono dim">{_ct_str(c.get('occurred_at'))}</td>
        <td><strong>{c.get('strategy') or '?'}</strong></td>
        <td class="mono">{raw.get('entry_price') or '—'} → {raw.get('exit_price') or '—'}</td>
        <td>{outcome}</td>
        <td class="mono dim">{bh_str}</td>
        <td class="mono" style="color:{pnl_color};text-align:right;">
          {_money(net)}</td>
      </tr>
""")
    return f"""
  <section class="card">
    <div class="card-title">Recent Closes
      <span class="dim mini">most recent {limit}</span>
    </div>
    <table class="recent">
      <thead><tr>
        <th>time</th><th>strategy</th><th>price</th>
        <th>outcome</th><th>held</th><th style="text-align:right;">net P&amp;L</th>
      </tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
  </section>
"""


# ─────────────────────────── CSS + page shell ───────────────────────


_CSS = """
:root {
  --bg: #ffffff;
  --card: #f8fafc;
  --border: #e2e8f0;
  --text: #0f172a;
  --dim-1: #475569;
  --dim-2: #94a3b8;
  --pos: #15803d;
  --neg: #b91c1c;
  --warn: #d97706;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 16px; max-width: 1280px; margin-left: auto;
  margin-right: auto; background: var(--bg); color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  font-size: 14px;
}
.dim { color: var(--dim-1); }
.mini { font-size: 11px; }
.mono { font-family: 'SF Mono', Menlo, monospace; }
.empty { padding: 18px; text-align: center; color: var(--dim-2); }
.warn { color: #f59e0b; padding: 6px 0; font-size: 12px; }

.topbar {
  display: flex; justify-content: space-between; align-items: baseline;
  padding-bottom: 12px; margin-bottom: 12px;
  border-bottom: 1px solid var(--border);
}
.brand .title { font-weight: 600; font-size: 16px; }
.brand .sub { margin-left: 10px; font-size: 11px; }
.last-bar { font-size: 12px; }
.last-bar .mono { margin: 0 6px; }

.hb-row {
  display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap;
}
.hb-pill {
  flex: 1; min-width: 140px; padding: 8px 12px;
  background: var(--card); border: 2px solid var(--border); border-radius: 6px;
}
.hb-name { font-weight: 600; font-size: 13px; }
.hb-status { font-weight: 700; font-size: 12px; margin-top: 2px; }
.hb-sub { margin-top: 2px; }

.card {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 8px; padding: 14px; margin-bottom: 14px;
}
.card-title { font-weight: 600; margin-bottom: 10px; font-size: 13px; }
.card-title .dim { font-weight: 400; margin-left: 6px; }

.pill { display: inline-block; padding: 2px 8px; border-radius: 999px;
        font-size: 11px; font-weight: 600; }

.pos-list { display: flex; flex-direction: column; gap: 6px; }
.pos { display: flex; gap: 12px; align-items: baseline; padding: 6px 0; }
.pos-name { font-weight: 600; }
.pos-side { font-weight: 700; font-size: 13px; }
.pos-contract { font-size: 11px; }

.strat-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 10px;
}
.strat-card {
  background: #ffffff; border: 1px solid var(--border);
  border-radius: 6px; padding: 10px 12px;
}
.strat-head { display: flex; justify-content: space-between; align-items: center;
              margin-bottom: 6px; }
.strat-name { font-weight: 600; }
.strat-pnl { font-size: 18px; font-weight: 700; margin-bottom: 4px; }
.strat-stats { display: flex; gap: 10px; font-size: 11px; }

.gate-list { display: flex; flex-direction: column; gap: 8px; }
.gate-row {
  display: grid; grid-template-columns: 90px 70px 1fr 60px 1fr;
  gap: 10px; align-items: center;
}
.gate-bar { background: #f1f5f9; border-radius: 999px;
            height: 6px; overflow: hidden; }
.gate-fill { height: 100%; border-radius: 999px; transition: width 0.3s; }
.gate-score { text-align: right; font-weight: 700; }
.gate-target { text-align: left; }

.hh-grid {
  display: grid; grid-template-columns: repeat(12, 1fr);
  gap: 4px;
}
.hh-cell {
  border: 1px solid transparent; border-radius: 4px; padding: 6px 4px;
  text-align: center; font-size: 11px; min-height: 56px;
  display: flex; flex-direction: column; justify-content: center;
}
.hh-cell.empty { opacity: 0.4; }
.hh-hour { font-weight: 700; margin-bottom: 2px; }
.hh-pnl { font-size: 10px; }
.hh-n { font-size: 9px; margin-top: 2px; }

.bh-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 14px; }
.bh-col { text-align: center; }
.bh-col.empty { opacity: 0.5; }
.bh-name { font-weight: 600; margin-bottom: 6px; }
.bh-bars { display: flex; gap: 4px; height: 80px; align-items: flex-end;
           border-bottom: 1px solid var(--border); }
.bh-bar { flex: 1; display: flex; flex-direction: column; height: 100%;
          justify-content: flex-end; gap: 2px; }
.bh-fill { width: 100%; border-radius: 2px 2px 0 0; min-height: 1px; }
.bh-label { line-height: 1.1; height: 24px; }
.bh-warn { margin-top: 6px; font-weight: 600; }

.bucket-bar { display: flex; gap: 6px; flex-wrap: wrap; align-items: center;
              margin-bottom: 14px; }
.bucket-bar > span:first-child { margin-right: 4px; }
.bucket-chip {
  padding: 4px 10px; border-radius: 999px; font-size: 11px;
  text-decoration: none; font-weight: 600; border: 1px solid var(--border);
  transition: background 0.15s;
}
.bucket-chip:hover { background: #e2e8f0 !important; }

.recent { width: 100%; border-collapse: collapse; font-size: 12px; }
/* ── Kill switch ────────────────────────────────────────────────── */
.kill-switch-row {
  display: grid; grid-template-columns: 1fr 2fr;
  gap: 12px; margin-bottom: 16px; align-items: stretch;
}
.ks-status {
  padding: 12px 16px; border-radius: 8px; font-weight: 700;
  display: flex; flex-direction: column; gap: 4px;
}
.ks-status-label { font-size: 13px; letter-spacing: 0.4px; }
.ks-action { display: flex; align-items: stretch; }
.ks-btn {
  flex: 1; display: flex; align-items: center; justify-content: center;
  padding: 14px 20px; border-radius: 8px; text-decoration: none;
  font-weight: 700; font-size: 14px; letter-spacing: 0.6px;
  transition: opacity 0.15s, transform 0.05s;
}
.ks-btn:active { transform: scale(0.99); }
.ks-btn-flat {
  background: #b91c1c; color: white; border: 2px solid #991b1b;
}
.ks-btn-flat:hover { background: #991b1b; }
.ks-btn-resume {
  background: #15803d; color: white; border: 2px solid #166534;
}
.ks-btn-resume:hover { background: #166534; }

/* ── MLL tracker ────────────────────────────────────────────────── */
.mll-tracker {
  display: grid; grid-template-columns: repeat(3, 1fr);
  gap: 10px; margin-bottom: 16px;
}
.mll-card {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 8px; padding: 12px 14px;
}
.mll-label { letter-spacing: 0.5px; }
.mll-num {
  font-size: 26px; font-weight: 700; margin: 4px 0 2px;
}
.mll-sub { font-size: 11px; }

.recent th { text-align: left; padding: 4px 8px; color: var(--dim-1);
             border-bottom: 1px solid var(--border); font-weight: 600; }
.recent td { padding: 4px 8px; border-bottom: 1px solid #f1f5f9; }
"""


def render_overview(sb, *, token: str | None = None,
                    bucket: str = "all") -> str:
    """Main entry — renders the full HTML page.

    `bucket` is one of TIME_BUCKETS keys ('all' default). When set to a
    specific bucket, strategy cards / bars-held / recent trades are
    filtered to closes whose entry-hour CT falls in that bucket's hour
    set. The hour heatmap always shows all 24 hours (the discovery
    surface) but highlights the selected bucket with a yellow halo.
    """
    if bucket not in TIME_BUCKETS:
        bucket = "all"
    heartbeats = _fetch_heartbeats(sb)
    strategies = _fetch_strategies(sb)
    snaps = _fetch_perf_snapshots(sb)
    closes_all = _fetch_recent_closes(sb)
    closes_filtered = _filter_closes_by_bucket(closes_all, bucket)
    ks_state = _fetch_kill_switch_state(sb)

    body = (
        _render_kill_switch(ks_state, token)
        + _render_mll_tracker(closes_all)
        + _render_header(heartbeats)
        + _render_bucket_selector(bucket, token)
        + _render_position_panel(heartbeats)
        + _render_strategy_cards(strategies, snaps,
                                  closes_in_bucket=closes_filtered,
                                  bucket=bucket)
        + _render_promotion_gate(strategies)
        + _render_hour_heatmap(closes_all, selected_bucket=bucket)
        + _render_recent_trades(closes_filtered)
    )
    token_q = f"?token={token}" if token else ""
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta http-equiv="refresh" content="10" />
<title>Acme Futures · New Fleet</title>
<style>{_CSS}</style>
</head><body>
{body}
<footer class="dim mini" style="text-align:center;padding:12px;">
  v3 archive: <a href="/v3-archive{token_q}" style="color:#475569;">→ here</a>
  · legacy fleet: <a href="/fleet{token_q}" style="color:#475569;">→ here</a>
  · auto-refresh 10s
</footer>
</body></html>
"""
