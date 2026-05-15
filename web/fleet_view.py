"""Ghost Dog Capital · Fleet dashboard.

Full visual overhaul of the Part-2 fleet view. Same architecture, same
Supabase fetchers, same render_overview() signature — new everything
else. Mounted at `/` by web/app.py.

Layout (top to bottom):
  1. Topbar           Ghost Dog logo + subtitle + last-bar age
  2. Ticker bar       Session P&L · MES · Position · Eval target ·
                      Remaining · Est. days · ARMED/KILL controls
  3. MLL tracker      Session P&L / DLL / Trailing MLL with progress bars
  4. Active trade     6-col panel, visible only when a position is open
  5. Strategy cards   Boundary / Overnight Drift / Gap Fill, P&L-tinted
  6. Hour-of-day grid 24-cell grid (two rows of 12), CT-hour P&L
  7. Recent closes    Most recent 20 trades + any open position
  8. Footer           Brand + auto-refresh indicator

Typography:
  Bebas Neue          large numbers, strategy names, brand
  Barlow Condensed    labels, badges, section headers
  IBM Plex Mono       data values, prices, P&L, timestamps

Data sources are identical to the prior view:
  runtime_heartbeats, strategy_perf_snapshot, strategies, broker_events,
  operator_events. No new queries.

Data limitations (carried over from the heartbeat schema):
  The Active Trade panel needs entry-price / current-price / unrealized /
  trail-stop fields that the conductor doesn't currently include in its
  heartbeat `extra` payload. Those cells show `—` until the heartbeat
  schema is enriched. Everything else (strategy P&L, hour buckets,
  recent closes, kill switch) is fully wired.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta
from math import ceil
from typing import Any
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
UTC = ZoneInfo("UTC")

# Fleet — display order matches the strategy-cards grid left-to-right.
FLEET = ["boundary", "overnight_drift", "gap_fill", "go_no_go_levels"]

# Per-strategy display metadata. `accent` drives the 3px top bar on each
# card and the tag color in the recent-closes table. `silk` selects a CSS
# class for the small "racing silk" badge in the card head.
STRATEGY_META: dict[str, dict[str, str]] = {
    "boundary": {
        "label":     "BOUNDARY",
        "accent":    "#C49A30",          # gold
        "silk":      "silk-gold-diag",
        "window":    "17–23 · 03–08 CT",
    },
    "overnight_drift": {
        "label":     "OVERNIGHT DRIFT",
        "accent":    "#4A8C3F",          # green
        "silk":      "silk-green",
        "window":    "17:00 → 08:30 CT",
    },
    "gap_fill": {
        "label":     "GAP FILL",
        "accent":    "#C4801A",          # amber
        "silk":      "silk-amber",
        "window":    "08:30 → 13:00 CT",
    },
    "go_no_go_levels": {
        "label":     "GO/NO-GO LEVELS",
        "accent":    "#5B7FB8",          # steel blue
        "silk":      "silk-blue",
        "window":    "08:00 → 12:00 CT",
    },
}

# Topstep 50K eval constants
_TOPSTEP_DLL  = 1_000.0
_TOPSTEP_MLL  = 2_000.0
EVAL_TARGET   = 3_000.0

# PerfTracker thresholds — only used for the PILOT/SHADOW badge on cards.
PILOT_THRESHOLD = 0.55
LIVE_THRESHOLD  = 0.65

# Heartbeat staleness (matches scripts/check_runner_heartbeat.py default).
HB_LIVE_MAX_S    = 300
HB_OFFLINE_MIN_S = 900


# ─────────────────────────── small helpers (UNCHANGED) ──────────────


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


# ─────────────────────────── data fetchers (UNCHANGED) ──────────────


def _fetch_heartbeats(sb) -> dict[str, dict[str, Any]]:
    """Heartbeats for the 3 fleet strategies only."""
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


def _fetch_eval_log_counts(sb) -> dict[str, int]:
    """Counts for the FLEET ACTIVITY panel: total evals today + near misses
    today. 'Today' = the current CT calendar date. Failures return zeros
    rather than tearing down the page render."""
    now_ct = datetime.now(UTC).astimezone(CT)
    midnight_ct = now_ct.replace(hour=0, minute=0, second=0, microsecond=0)
    floor_iso = midnight_ct.astimezone(UTC).isoformat()
    out = {"evals_today": 0, "near_misses_today": 0}
    try:
        res = (sb.table("eval_log").select("id", count="exact")
               .gte("bar_ts", floor_iso).limit(1).execute())
        out["evals_today"] = int(res.count or 0)
    except Exception:
        pass
    try:
        res = (sb.table("eval_log").select("id", count="exact")
               .eq("near_miss", True).gte("bar_ts", floor_iso).limit(1).execute())
        out["near_misses_today"] = int(res.count or 0)
    except Exception:
        pass
    return out


def _fetch_eval_log_activity(sb, *, limit: int = 12) -> list[dict[str, Any]]:
    """Most recent N eval_log rows for the activity feed.

    Returns rows sorted bar_ts desc. Empty list on failure or no rows.
    """
    try:
        res = (sb.table("eval_log")
               .select("bar_ts,strategy,outcome,near_miss,gate_failed,"
                       "signal_side,reason,gate_values")
               .order("bar_ts", desc=True).limit(limit).execute())
        return res.data or []
    except Exception:
        return []


def _fetch_charge_pct_by_strategy(sb) -> dict[str, float | None]:
    """Most-recent charge_pct per strategy.

    PostgREST doesn't support DISTINCT ON, so we issue one query per
    active fleet member (matches the pattern in `_fetch_perf_snapshots`).
    Returns name → float (0.0–1.0) or None if no row exists yet.
    """
    out: dict[str, float | None] = {}
    for name in FLEET:
        try:
            res = (sb.table("eval_log").select("gate_values")
                   .eq("strategy", name)
                   .order("bar_ts", desc=True).limit(1).execute())
            row = (res.data or [None])[0]
            if row is None:
                out[name] = None
                continue
            gv = row.get("gate_values") or {}
            raw = gv.get("charge_pct")
            out[name] = float(raw) if raw is not None else None
        except Exception:
            out[name] = None
    return out


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


# ─────────────────────────── derived helpers ────────────────────────


def _today_session_closes(closes: list[dict]) -> list[dict]:
    """Closes that exited during the current CT trading-day session.
    Trade-date rolls at 17:00 CT (Globex open), matching trading_date_ct."""
    now_ct = datetime.now(UTC).astimezone(CT)
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


def _session_pnl(closes: list[dict]) -> float:
    return sum(float((c.get("raw") or {}).get("net_pnl") or 0) for c in closes)


def _avg_daily_pnl(closes: list[dict]) -> float:
    """Avg P&L per unique CT date across the closes window. Used for the
    EST. DAYS ticker calculation. Returns 0 if no closes."""
    by_date: dict = defaultdict(float)
    for c in closes:
        ts = c.get("occurred_at")
        if not ts:
            continue
        try:
            d = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(CT).date()
        except Exception:
            continue
        raw = c.get("raw") or {}
        by_date[d] += float(raw.get("net_pnl") or 0)
    if not by_date:
        return 0.0
    return sum(by_date.values()) / len(by_date)


def _card_bg(net_pnl: float, all_nets: list[float]) -> str:
    """Background color for a strategy card based on its net P&L relative
    to the fleet's max. Per spec:
        red-tint        net <= 0
        light green     0 < intensity <= 0.33
        medium green    0.33 < intensity <= 0.66
        strong green    intensity > 0.66
    where intensity = this card's net / max(all_nets)."""
    if net_pnl <= 0:
        return "#2A1C1A"          # red tint
    positives = [n for n in all_nets if n > 0]
    max_net = max(positives) if positives else 1.0
    intensity = net_pnl / max_net if max_net > 0 else 0
    if intensity > 0.66:
        return "#1A2E1A"          # strong green
    if intensity > 0.33:
        return "#1E2A1C"          # medium green
    return "#222B1F"              # light green


def _mll_band_color(used_frac: float) -> str:
    """Color for an MLL/DLL progress band. Per spec: green < 30%, gold
    30-70%, red > 70%."""
    if used_frac < 0.30:
        return "var(--green2)"
    if used_frac < 0.70:
        return "var(--gold2)"
    return "var(--red2)"


def _heartbeat_status(hb: dict | None) -> tuple[str, str, str]:
    """(label, color, sub-text). label ∈ {LIVE, STALE, OFFLINE, UNKNOWN}."""
    if not hb:
        return ("UNKNOWN", "var(--text3)", "no heartbeat")
    ts = hb.get("ts")
    if not ts:
        return ("UNKNOWN", "var(--text3)", "no ts")
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return ("UNKNOWN", "var(--text3)", "bad ts")
    age_s = int((datetime.now(UTC) - d).total_seconds())
    if age_s < HB_LIVE_MAX_S:
        return ("LIVE", "var(--green2)", f"{age_s}s ago")
    if age_s < HB_OFFLINE_MIN_S:
        return ("STALE", "var(--amber)", _ago(ts))
    return ("OFFLINE", "var(--red2)", _ago(ts))


def _strategy_metrics(name: str, strategies: dict, snaps: dict) -> dict[str, Any]:
    s = strategies.get(name) or {}
    sn = snaps.get(name) or {}
    return {
        "name":     name,
        "state":    s.get("state") or "?",
        "tier":     int(s.get("tier") or 2),
        "score":    float(s.get("score") or 0),
        "n_trades": int(sn.get("n_trades") or 0),
        "net_pnl":  float(sn.get("net_pnl") or 0),
        "win_rate": float(sn.get("win_rate") or 0),
        "pf":       sn.get("profit_factor"),
        "sharpe":   float(sn.get("sharpe") or 0),
        "dd":       float(sn.get("max_drawdown") or 0),
        "avg_pnl":  float(sn.get("avg_pnl") or 0) if sn.get("avg_pnl") is not None else (
            (float(sn.get("net_pnl") or 0) / int(sn["n_trades"]))
            if sn.get("n_trades") else 0.0
        ),
    }


# ─────────────────────────── section renderers ──────────────────────


def _render_topbar(heartbeats: dict) -> str:
    """Topbar: logo + subtitle on the left, last-bar age on the right.

    The logo is rendered as inline SVG (not <img src=>) so it inherits
    the Google Fonts loaded by the page. Inline SVG also dodges the
    binary-file-in-git problem — the real PNG-with-wolf-illustration
    can be saved to web/static/ghost_dog_logo.png and swapped in by
    replacing this inline <svg>...</svg> block with an <img> tag.
    """
    last_bar_ts: str | None = None
    for hb in heartbeats.values():
        lb = hb.get("last_bar_ts")
        if lb and (last_bar_ts is None or lb > last_bar_ts):
            last_bar_ts = lb
    last_bar = _ago(last_bar_ts) if last_bar_ts else "—"
    # Primary logo: PNG file at web/static/ghost_dog_logo.png.
    # Fallback: inline-SVG wordmark, shown via onerror if the PNG is
    # missing (so the page never breaks during a deploy ordering issue).
    fallback_svg = (
        '<svg class=\\\'logo\\\' viewBox=\\\'0 0 200 60\\\' '
        'preserveAspectRatio=\\\'xMidYMid meet\\\'>'
        '<text x=\\\'100\\\' y=\\\'40\\\' text-anchor=\\\'middle\\\' class=\\\'logo-name\\\'>'
        'GHOST DOG</text>'
        '<text x=\\\'100\\\' y=\\\'55\\\' text-anchor=\\\'middle\\\' class=\\\'logo-tag\\\'>'
        'CAPITAL</text></svg>'
    )
    logo_html = (
        '<img src="/static/ghost_dog_logo.png" alt="Ghost Dog Capital" '
        'class="logo" '
        f'onerror="this.outerHTML=\'{fallback_svg}\'">'
    )
    return f"""
  <header class="topbar">
    <div class="brand">
      {logo_html}
      <span class="brand-divider"></span>
      <span class="brand-sub">$50K EVAL · CLASSIC CONDUCTOR · SINGLE POSITION</span>
    </div>
    <div class="last-bar">
      <span class="lb-label">LAST BAR</span>
      <span class="lb-value">{last_bar}</span>
    </div>
  </header>
"""


def _render_ticker_bar(
    closes_all: list[dict], heartbeats: dict, ks: dict, token: str | None,
) -> str:
    """Ticker bar: session metrics + kill-switch controls."""
    today = _today_session_closes(closes_all)
    session_pnl = _session_pnl(today)
    pnl_color = ("var(--green2)" if session_pnl > 0
                 else "var(--red2)" if session_pnl < 0
                 else "var(--text2)")

    # MES price — not currently in the heartbeat schema. Show "—" until
    # the conductor includes it in extras. (Same applies to entry/current
    # in the active trade panel below.)
    mes_price = "—"

    # Single-position summary across the fleet.
    in_pos = [(n, hb) for n, hb in heartbeats.items()
              if (hb.get("position_state") or "flat") != "flat"]
    if not in_pos:
        position = "FLAT"
        pos_color = "var(--text3)"
    else:
        name, hb = in_pos[0]
        side = (hb.get("position_state") or "?").upper()
        position = f"{side} MES"
        pos_color = "var(--green2)" if side == "LONG" else "var(--red2)"

    remaining = EVAL_TARGET - session_pnl
    avg = _avg_daily_pnl(closes_all)
    if remaining <= 0:
        days_est = "DONE"
    elif avg > 0:
        days_est = str(ceil(remaining / avg))
    else:
        days_est = "—"

    token_q = f"&token={token}" if token else ""
    if ks.get("active"):
        ks_status = '<span class="ticker-badge ks-active">● KILL ACTIVE</span>'
        ks_button = (
            f'<a href="/kill-switch?action=clear{token_q}" '
            f'class="ticker-btn ks-resume" '
            f'onclick="return confirm(\'Clear the kill switch and resume trading?\');">'
            f'RESUME</a>'
        )
    else:
        ks_status = '<span class="ticker-badge ks-armed">● ARMED</span>'
        ks_button = (
            f'<a href="/kill-switch?action=activate{token_q}" '
            f'class="ticker-btn ks-flat" '
            f'onclick="return confirm(\'EMERGENCY FLAT — force-close ALL positions immediately. '
            f'Are you sure?\');">'
            f'⬛ EMERGENCY FLAT</a>'
        )

    items = [
        ("SESSION P&amp;L", f'<span style="color:{pnl_color}">{_money(session_pnl)}</span>'),
        ("MES",             mes_price),
        ("POSITION",        f'<span style="color:{pos_color}">{position}</span>'),
        ("EVAL TARGET",     _money(EVAL_TARGET)),
        ("REMAINING",       _money(remaining)),
        ("EST. DAYS",       days_est),
    ]
    items_html = "".join(
        f'<div class="ticker-item">'
        f'<span class="ticker-label">{label}</span>'
        f'<span class="ticker-value">{value}</span>'
        f'</div>'
        for label, value in items
    )
    return f"""
  <section class="ticker">
    <div class="ticker-items">{items_html}</div>
    <div class="ticker-actions">{ks_status}{ks_button}</div>
  </section>
"""


def _render_mll_tracker(closes_all: list[dict]) -> str:
    """3-column MLL/DLL tracker with progress bars. Session-only math —
    DLL distance is intraday-loss vs $1K, MLL distance is intraday peak-
    to-trough drawdown vs $2K."""
    todays = _today_session_closes(closes_all)
    pnls: list[float] = []
    for c in sorted(todays, key=lambda r: r.get("occurred_at") or ""):
        raw = c.get("raw") or {}
        pnls.append(float(raw.get("net_pnl") or 0.0))
    session_pnl = sum(pnls)

    running = peak = max_dd = 0.0
    for p in pnls:
        running += p
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)

    intraday_loss = max(0.0, -session_pnl) if session_pnl < 0 else 0.0
    dll_distance  = max(0.0, _TOPSTEP_DLL - intraday_loss)
    mll_distance  = max(0.0, _TOPSTEP_MLL - max_dd)

    pnl_color = ("var(--green2)" if session_pnl > 0
                 else "var(--red2)" if session_pnl < 0
                 else "var(--text2)")
    dll_used_frac = intraday_loss / _TOPSTEP_DLL if _TOPSTEP_DLL else 0
    mll_used_frac = max_dd / _TOPSTEP_MLL if _TOPSTEP_MLL else 0
    dll_color = _mll_band_color(dll_used_frac)
    mll_color = _mll_band_color(mll_used_frac)
    dll_fill_pct = min(100.0, dll_used_frac * 100)
    mll_fill_pct = min(100.0, mll_used_frac * 100)
    pnl_fill_pct = min(100.0, abs(session_pnl) / EVAL_TARGET * 100)

    return f"""
  <section class="mll-tracker">
    <div class="mll-card">
      <div class="mll-label">SESSION P&amp;L</div>
      <div class="mll-num" style="color:{pnl_color};">{_money(session_pnl)}</div>
      <div class="mll-sub">{len(pnls)} closed today</div>
      <div class="mll-bar"><div class="mll-fill"
           style="width:{pnl_fill_pct:.1f}%;background:{pnl_color};"></div></div>
    </div>
    <div class="mll-card">
      <div class="mll-label">DAILY LOSS LIMIT  ·  ${_TOPSTEP_DLL:,.0f}</div>
      <div class="mll-num" style="color:{dll_color};">{_money(dll_distance)}</div>
      <div class="mll-sub">intraday loss {_money(intraday_loss)}</div>
      <div class="mll-bar"><div class="mll-fill"
           style="width:{dll_fill_pct:.1f}%;background:{dll_color};"></div></div>
    </div>
    <div class="mll-card">
      <div class="mll-label">TRAILING MLL  ·  ${_TOPSTEP_MLL:,.0f}</div>
      <div class="mll-num" style="color:{mll_color};">{_money(mll_distance)}</div>
      <div class="mll-sub">peak-to-trough today {_money(max_dd)}</div>
      <div class="mll-bar"><div class="mll-fill"
           style="width:{mll_fill_pct:.1f}%;background:{mll_color};"></div></div>
    </div>
  </section>
"""


def _render_active_trade(heartbeats: dict) -> str:
    """6-column active-trade panel. Only renders when any keeper reports
    a non-flat position. Entry/current/unrealized/trail-stop are placeholders
    until the heartbeat schema is enriched — see module docstring."""
    in_pos = [(n, hb) for n, hb in heartbeats.items()
              if (hb.get("position_state") or "flat") != "flat"]
    if not in_pos:
        return """
  <section class="active-trade flat">
    <div class="at-label">ACTIVE TRADE</div>
    <div class="at-empty">NO ACTIVE POSITION</div>
  </section>
"""

    # If multiple strategies are non-flat (shouldn't happen on the classic
    # conductor — single position), show the most recent only and surface
    # a warning in the badge area.
    in_pos.sort(key=lambda x: x[1].get("ts") or "", reverse=True)
    name, hb = in_pos[0]
    side = (hb.get("position_state") or "?").upper()
    extras = hb.get("extra") or {}
    contract = extras.get("contract_id") or "MES"
    side_color = "var(--green2)" if side == "LONG" else "var(--red2)"
    meta = STRATEGY_META.get(name, {})
    label = meta.get("label", name.upper())

    warn = ""
    if len(in_pos) > 1:
        others = ", ".join(n for n, _ in in_pos[1:])
        warn = (f'<span class="at-warn">⚠ {len(in_pos)} non-flat: '
                f'{name} + {others}</span>')

    cells = [
        ("STRATEGY",   label,                                "value"),
        ("SIDE",       f'<span style="color:{side_color};">{side}</span>',  "value"),
        ("ENTRY",      "—",                                  "value muted"),
        ("CURRENT",    "—",                                  "value muted"),
        ("UNREALIZED", "—",                                  "value muted"),
        ("TRAIL STOP", '<span style="color:var(--gold2);">—</span>', "value"),
    ]
    cells_html = "".join(
        f'<div class="at-cell">'
        f'<span class="at-cell-label">{label}</span>'
        f'<span class="at-cell-{cls}">{val}</span>'
        f'</div>'
        for label, val, cls in cells
    )
    return f"""
  <section class="active-trade">
    <div class="at-head">
      <span class="at-label">ACTIVE TRADE  ·  {contract}</span>
      {warn}
    </div>
    <div class="at-grid">{cells_html}</div>
  </section>
"""


_BADGE_BG_BY_STATE: dict[str, str] = {
    "LIVE":          "var(--green)",
    "PILOT":         "var(--gold)",
    "SHADOW":        "var(--surface2)",
    "BENCH":         "var(--surface2)",
    "RETIRED":       "var(--surface2)",
    "BACKTEST":      "var(--surface2)",
    "REPLAY":        "var(--surface2)",
}


def _badge_for(state: str | None, score: float) -> tuple[str, str]:
    """Pick the (label, bg-color) for a strategy's lifecycle badge.

    Primary source is the strategies-table `state` column — PILOT shows
    PILOT, SHADOW shows SHADOW, LIVE shows LIVE, etc. Falls back to
    score-derived display only when state is null / unknown (legacy
    behaviour preserved as the fallback).
    """
    if state and state in _BADGE_BG_BY_STATE:
        return state, _BADGE_BG_BY_STATE[state]
    # Fallback: score-derived lifecycle estimate.
    if score >= LIVE_THRESHOLD:
        return "LIVE-ELIGIBLE", "var(--green)"
    if score >= PILOT_THRESHOLD:
        return "PILOT", "var(--gold)"
    return "SHADOW", "var(--surface2)"


def _charge_color(charge: float | None) -> str:
    """Color for the charge-pct meter. Red < 30%, gold 30–70%, green > 70%."""
    if charge is None:
        return "var(--text3)"
    if charge < 0.30:
        return "var(--red2)"
    if charge < 0.70:
        return "var(--gold2)"
    return "var(--green2)"


def _render_strategy_cards(
    heartbeats: dict, strategies: dict, snaps: dict,
    charge_by_strategy: dict[str, float | None] | None = None,
) -> str:
    """Strategy grid (one card per FLEET member). Background tint scales
    with each card's net P&L vs the fleet max (per _card_bg). Top 3px
    accent bar is per-strategy. Racing silk in the head is a small
    color-coded badge. A charge-pct meter sits between the stats rows
    and the foot — eval_log's last-bar charge for this strategy.

    `charge_by_strategy` is optional so callers (and tests) can pass an
    empty dict to skip the meter.
    """
    charge_by_strategy = charge_by_strategy or {}
    metrics = {name: _strategy_metrics(name, strategies, snaps) for name in FLEET}
    all_nets = [metrics[name]["net_pnl"] for name in FLEET]
    cards = []
    for name in FLEET:
        m = metrics[name]
        meta = STRATEGY_META[name]
        bg = _card_bg(m["net_pnl"], all_nets)

        hb = heartbeats.get(name) or {}
        pos_state = (hb.get("position_state") or "flat").lower()
        if pos_state != "flat":
            dot_color = "var(--green2)"
            status = f"LIVE · {pos_state.upper()} 1c"
        else:
            dot_color = "var(--text3)"
            status = "FLAT"

        # Lifecycle badge: derived from strategies-table state when present,
        # else score-derived (legacy fallback). PILOT in the DB now displays
        # as PILOT — the score-only path no longer overrides.
        db_state = (strategies.get(name) or {}).get("state")
        badge_label, badge_bg = _badge_for(db_state, m["score"])

        pnl_color = ("var(--green2)" if m["net_pnl"] > 0
                     else "var(--red2)" if m["net_pnl"] < 0
                     else "var(--text2)")
        pf_str = f"{m['pf']:.2f}" if m["pf"] is not None else "—"

        charge = charge_by_strategy.get(name)
        charge_pct_display = f"{charge*100:.0f}%" if charge is not None else "—"
        charge_fill_pct = min(100.0, max(0.0, (charge or 0) * 100))
        ch_color = _charge_color(charge)

        cards.append(f"""
    <div class="strat-card" style="background:{bg};border-top-color:{meta['accent']};">
      <div class="strat-head">
        <div class="strat-head-left">
          <div class="silk {meta['silk']}"></div>
          <span class="strat-name">{meta['label']}</span>
        </div>
        <span class="strat-badge"
              style="background:{badge_bg};color:var(--text);">{badge_label}</span>
      </div>
      <div class="strat-pnl" style="color:{pnl_color};">{_money(m['net_pnl'])}</div>
      <div class="strat-stats">
        <span class="ss-row">
          <span class="ss-k">n=</span><span class="ss-v">{m['n_trades']}</span>
          <span class="ss-k">WR=</span><span class="ss-v">{_pct(m['win_rate'])}</span>
          <span class="ss-k">PF=</span><span class="ss-v">{pf_str}</span>
        </span>
        <span class="ss-row">
          <span class="ss-k">max DD</span><span class="ss-v">{_money(m['dd'])}</span>
          <span class="ss-k">avg</span><span class="ss-v">{_money(m['avg_pnl'])}</span>
        </span>
      </div>
      <div class="strat-charge">
        <span class="ch-label">CHARGE</span>
        <div class="ch-bar"><div class="ch-fill"
             style="width:{charge_fill_pct:.1f}%;background:{ch_color};"></div></div>
        <span class="ch-pct" style="color:{ch_color};">{charge_pct_display}</span>
      </div>
      <div class="strat-foot">
        <span class="live-dot" style="background:{dot_color};"></span>
        <span class="strat-status">{status}</span>
        <span class="strat-window">{meta['window']}</span>
      </div>
    </div>
""")
    return f"""
  <section class="strat-grid">
    {''.join(cards)}
  </section>
"""


def _render_hour_grid(closes: list[dict]) -> str:
    """24-hour P&L grid laid out as two rows of 12. Cells colored by
    aggregate P&L sign; current CT hour gets the amber 'live' treatment."""
    buckets: dict[int, dict[str, float]] = defaultdict(
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
        buckets[d.hour]["n"] += 1
        buckets[d.hour]["net_pnl"] += float(raw.get("net_pnl") or 0)

    current_hour = datetime.now(UTC).astimezone(CT).hour

    def cell(h: int) -> str:
        v = buckets.get(h)
        label = f"{h:02d}"
        if h == current_hour:
            return (f'<div class="hh-cell hh-live" title="{h:02d}:00 CT — live">'
                    f'<span class="hh-hour">{label}</span>'
                    f'<span class="hh-val">live</span>'
                    f'</div>')
        if v is None or v["n"] == 0:
            return (f'<div class="hh-cell hh-empty" title="{h:02d}:00 CT — no trades">'
                    f'<span class="hh-hour">{label}</span>'
                    f'<span class="hh-val">—</span>'
                    f'</div>')
        pnl = v["net_pnl"]
        cls = "hh-pos" if pnl > 0 else "hh-neg" if pnl < 0 else "hh-empty"
        title = f'{h:02d}:00 CT — n={int(v["n"])} net={_money(pnl)}'
        return (f'<div class="hh-cell {cls}" title="{title}">'
                f'<span class="hh-hour">{label}</span>'
                f'<span class="hh-val">{_money(pnl)}</span>'
                f'</div>')

    row1 = "".join(cell(h) for h in range(0, 12))
    row2 = "".join(cell(h) for h in range(12, 24))
    return f"""
  <section class="hh-section">
    <div class="hh-head">HOUR-OF-DAY P&amp;L  ·  CT</div>
    <div class="hh-row">{row1}</div>
    <div class="hh-row">{row2}</div>
  </section>
"""


def _render_recent_closes(closes: list[dict], heartbeats: dict, limit: int = 20) -> str:
    """Recent closes table with an optional 'open' row at the top for
    any currently active position."""
    open_rows = []
    for name in FLEET:
        hb = heartbeats.get(name) or {}
        pos = (hb.get("position_state") or "flat").lower()
        if pos == "flat":
            continue
        meta = STRATEGY_META.get(name, {})
        accent = meta.get("accent", "var(--text2)")
        label = meta.get("label", name.upper())
        side_color = "var(--green2)" if pos == "long" else "var(--red2)"
        open_rows.append(f"""
        <tr class="rc-row rc-open">
          <td class="rc-time">{_ct_str(hb.get('ts'))}</td>
          <td><span class="rc-tag" style="border-color:{accent};color:{accent};">
            {label}</span></td>
          <td class="rc-price">— → —</td>
          <td><span class="rc-outcome rc-outcome-open">open</span></td>
          <td class="rc-held">—</td>
          <td class="rc-pnl" style="color:{side_color};">{pos.upper()}</td>
        </tr>
""")

    rows = []
    for c in closes[:limit]:
        raw = c.get("raw") or {}
        net = float(raw.get("net_pnl") or 0)
        pnl_color = ("var(--green2)" if net > 0
                     else "var(--red2)" if net < 0
                     else "var(--text2)")
        outcome = (raw.get("outcome") or "?").lower()
        if "target" in outcome or net > 0 and outcome != "force_close_session":
            oc_class = "rc-outcome-target"
        elif "stop" in outcome:
            oc_class = "rc-outcome-stop"
        else:
            oc_class = "rc-outcome-flat"
        bh_m = raw.get("bars_held_minutes")
        bh_str = f"{int(bh_m)//2}b" if bh_m else "—"
        strat = c.get("strategy") or "?"
        meta = STRATEGY_META.get(strat, {})
        accent = meta.get("accent", "var(--text2)")
        label = meta.get("label", strat.upper())
        entry = raw.get("entry_price")
        exit_p = raw.get("exit_price")
        price_str = (f'{entry} → {exit_p}'
                     if entry is not None and exit_p is not None else "—")
        rows.append(f"""
      <tr class="rc-row">
        <td class="rc-time">{_ct_str(c.get('occurred_at'))}</td>
        <td><span class="rc-tag" style="border-color:{accent};color:{accent};">
          {label}</span></td>
        <td class="rc-price">{price_str}</td>
        <td><span class="rc-outcome {oc_class}">{outcome}</span></td>
        <td class="rc-held">{bh_str}</td>
        <td class="rc-pnl" style="color:{pnl_color};">{_money(net)}</td>
      </tr>
""")

    if not rows and not open_rows:
        body = ('<tr><td colspan="6" class="rc-empty">'
                'waiting for first close…</td></tr>')
    else:
        body = "".join(open_rows) + "".join(rows)

    return f"""
  <section class="rc-section">
    <div class="rc-head">RECENT CLOSES  ·  most recent {limit}</div>
    <table class="rc-table">
      <thead>
        <tr>
          <th>TIME</th><th>STRATEGY</th><th>PRICE</th>
          <th>OUTCOME</th><th>HELD</th><th class="rc-pnl-col">NET P&amp;L</th>
        </tr>
      </thead>
      <tbody>{body}</tbody>
    </table>
  </section>
"""


_OUTCOME_CLASS: dict[str, str] = {
    "PASS":  "ev-pass",
    "NEAR":  "ev-near",
    "ENTRY": "ev-entry",
    "HOLD":  "ev-hold",
}


def _render_eval_activity(
    counts: dict[str, int], rows: list[dict[str, Any]],
) -> str:
    """FLEET ACTIVITY panel — today's eval counts + activity feed of
    the most recent eval_log rows (PASS, NEAR, ENTRY, HOLD).

    Surfaces the invisible work: every bar each strategy evaluates is
    represented here, not just the bars that fired trades. NEAR rows
    are highlighted (★) — those are the "invisible saves."
    """
    evals_today = counts.get("evals_today", 0)
    nears_today = counts.get("near_misses_today", 0)

    body_rows: list[str] = []
    for r in rows:
        outcome = (r.get("outcome") or "PASS").upper()
        oc_cls = _OUTCOME_CLASS.get(outcome, "ev-pass")
        near = bool(r.get("near_miss"))
        star = '<span class="ev-star">★</span>' if near else ""
        strat = r.get("strategy") or "?"
        meta = STRATEGY_META.get(strat, {})
        accent = meta.get("accent", "var(--text2)")
        label = meta.get("label", strat.upper())
        side = (r.get("signal_side") or "—").upper()
        reason = (r.get("reason") or "").replace("<", "&lt;").replace(">", "&gt;")
        body_rows.append(f"""
      <tr class="ev-row">
        <td class="ev-time">{_ct_str(r.get('bar_ts'))}</td>
        <td><span class="ev-tag" style="border-color:{accent};color:{accent};">
          {label}</span></td>
        <td><span class="ev-outcome {oc_cls}">{outcome}</span>{star}</td>
        <td class="ev-side">{side}</td>
        <td class="ev-reason">{reason}</td>
      </tr>
""")
    if not body_rows:
        body = ('<tr><td colspan="5" class="ev-empty">'
                'waiting for first eval_log row…</td></tr>')
    else:
        body = "".join(body_rows)

    return f"""
  <section class="ev-section">
    <div class="ev-head">
      <span class="ev-title">FLEET ACTIVITY</span>
      <span class="ev-counts">
        <span class="ev-counts-k">evals today</span>
        <span class="ev-counts-v">{evals_today:,}</span>
        <span class="ev-sep">·</span>
        <span class="ev-counts-k">near-misses today</span>
        <span class="ev-counts-v ev-near-count">★ {nears_today}</span>
      </span>
    </div>
    <table class="ev-table">
      <thead>
        <tr>
          <th>TIME</th><th>STRATEGY</th><th>OUTCOME</th><th>DIR</th><th>REASON</th>
        </tr>
      </thead>
      <tbody>{body}</tbody>
    </table>
  </section>
"""


def _render_footer() -> str:
    return """
  <footer class="footer">
    <span class="ft-left">Ghost Dog Capital · Fleet v4.0</span>
    <span class="ft-right">supabase live · auto-refresh 10s</span>
  </footer>
"""


# ─────────────────────────── CSS ────────────────────────────────────


_CSS = """
:root {
  --bg: #2A2825;
  --bg2: #232220;
  --bg3: #1E1D1B;
  --surface: #323028;
  --surface2: #3A3835;
  --border: #4A4740;
  --border2: #5A5750;
  --text: #E8E2D6;
  --text2: #B8B0A0;
  --text3: #7A7268;
  --green: #4A8C3F;
  --green2: #6BAE60;
  --greenl: rgba(74,140,63,0.15);
  --red: #8C3A2E;
  --red2: #B85A4A;
  --redl: rgba(140,58,46,0.15);
  --gold: #8C6A1A;
  --gold2: #C49A30;
  --amber: #C4801A;
  --amberl: rgba(196,128,26,0.15);
}

* { box-sizing: border-box; }

body {
  margin: 0;
  padding: 0;
  background: var(--bg);
  color: var(--text);
  font-family: 'Barlow Condensed', 'Inter', -apple-system, sans-serif;
  font-size: 13px;
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

.page {
  flex: 1;
  max-width: 1480px;
  margin: 0 auto;
  width: 100%;
  padding: 0 16px;
}

/* ── Topbar ─────────────────────────────────────────────────────── */
.topbar {
  background: var(--bg3);
  border-bottom: 3px solid var(--gold2);
  height: 56px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0 20px;
  margin: 0 -16px 0 -16px;
}
.brand { display: flex; align-items: center; gap: 14px; }
.brand .logo {
  height: 44px;
  width: auto;
  display: block;
}
/* Inline-SVG <text> elements — fonts inherited from page CSS. */
.logo-name {
  font-family: 'Bebas Neue', 'Anton', 'Impact', 'Arial Black', sans-serif;
  font-size: 32px;
  font-weight: 700;
  letter-spacing: 2.5px;
  fill: var(--text);
}
.logo-tag {
  font-family: 'Barlow Condensed', 'Arial Narrow', sans-serif;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 8px;
  fill: var(--text);
}
.brand-divider {
  width: 1px;
  height: 32px;
  background: var(--border2);
}
.brand-sub {
  font-family: 'Barlow Condensed', sans-serif;
  font-weight: 600;
  font-size: 11px;
  letter-spacing: 1.5px;
  color: var(--text2);
  text-transform: uppercase;
}
.last-bar {
  display: flex;
  align-items: center;
  gap: 10px;
}
.lb-label {
  font-family: 'Barlow Condensed', sans-serif;
  font-size: 10px;
  letter-spacing: 1.5px;
  color: var(--text3);
  text-transform: uppercase;
}
.lb-value {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 12px;
  color: var(--text);
}

/* ── Ticker bar ─────────────────────────────────────────────────── */
.ticker {
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
  height: 32px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0 20px;
  margin: 0 -16px 16px -16px;
}
.ticker-items { display: flex; gap: 24px; align-items: center; }
.ticker-item { display: flex; align-items: baseline; gap: 6px; }
.ticker-label {
  font-family: 'Barlow Condensed', sans-serif;
  font-size: 10px;
  letter-spacing: 1.2px;
  color: var(--text3);
  text-transform: uppercase;
}
.ticker-value {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 11px;
  color: var(--text);
  font-weight: 500;
}
.ticker-actions { display: flex; align-items: center; gap: 10px; }
.ticker-badge {
  font-family: 'Barlow Condensed', sans-serif;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 1px;
  padding: 3px 8px;
  border-radius: 3px;
}
.ks-armed  { background: rgba(107,174,96,0.12); color: var(--green2); }
.ks-active { background: rgba(184,90,74,0.18);  color: var(--red2); }
.ticker-btn {
  font-family: 'Barlow Condensed', sans-serif;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 1px;
  padding: 4px 10px;
  border-radius: 3px;
  text-decoration: none;
  border: 1px solid;
  transition: background 0.15s;
}
.ks-flat   { color: var(--red2); border-color: var(--red); }
.ks-flat:hover   { background: rgba(184,90,74,0.15); }
.ks-resume { color: var(--green2); border-color: var(--green); }
.ks-resume:hover { background: rgba(107,174,96,0.15); }

/* ── MLL tracker ────────────────────────────────────────────────── */
.mll-tracker {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 1px;
  background: var(--border);
  border: 1px solid var(--border);
  margin-bottom: 16px;
}
.mll-card {
  background: var(--surface);
  padding: 14px 18px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.mll-label {
  font-family: 'Barlow Condensed', sans-serif;
  font-size: 10px;
  letter-spacing: 1.2px;
  color: var(--text3);
  text-transform: uppercase;
}
.mll-num {
  font-family: 'Bebas Neue', sans-serif;
  font-size: 26px;
  letter-spacing: 1px;
  line-height: 1.1;
}
.mll-sub {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 10px;
  color: var(--text3);
}
.mll-bar {
  height: 2px;
  background: var(--bg3);
  margin-top: 4px;
}
.mll-fill {
  height: 100%;
  transition: width 0.3s ease;
}

/* ── Active trade panel ─────────────────────────────────────────── */
.active-trade {
  background: var(--bg2);
  border: 1px solid var(--border);
  padding: 14px 18px;
  margin-bottom: 16px;
}
.active-trade.flat .at-empty {
  font-family: 'Barlow Condensed', sans-serif;
  font-size: 12px;
  letter-spacing: 1.2px;
  color: var(--text3);
  text-transform: uppercase;
  margin-top: 6px;
}
.at-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 10px;
}
.at-label {
  font-family: 'Barlow Condensed', sans-serif;
  font-size: 11px;
  letter-spacing: 1.5px;
  color: var(--text2);
  font-weight: 700;
  text-transform: uppercase;
}
.at-warn {
  font-family: 'Barlow Condensed', sans-serif;
  font-size: 10px;
  color: var(--amber);
}
.at-grid {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 1px;
  background: var(--border);
}
.at-cell {
  background: var(--surface);
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.at-cell-label {
  font-family: 'Barlow Condensed', sans-serif;
  font-size: 10px;
  letter-spacing: 1.2px;
  color: var(--text3);
  text-transform: uppercase;
}
.at-cell-value {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 13px;
  color: var(--text);
}
.at-cell-value.muted { color: var(--text3); }

/* ── Strategy cards ─────────────────────────────────────────────── */
.strat-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
  margin-bottom: 16px;
}
.strat-card {
  border: 1px solid var(--border);
  border-top: 3px solid var(--gold2);
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 170px;
}
.strat-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.strat-head-left { display: flex; align-items: center; gap: 8px; }
.silk {
  width: 16px;
  height: 16px;
  border-radius: 2px;
  border: 1px solid var(--border2);
}
.silk-gold-diag {
  background:
    repeating-linear-gradient(45deg,
      var(--gold2) 0px, var(--gold2) 4px,
      var(--bg3) 4px, var(--bg3) 8px);
}
.silk-green { background: var(--green); }
.silk-amber { background: var(--amber); }
.silk-blue  { background: #5B7FB8; }
.strat-name {
  font-family: 'Bebas Neue', sans-serif;
  font-size: 18px;
  letter-spacing: 1.5px;
  color: var(--text);
}
.strat-badge {
  font-family: 'Barlow Condensed', sans-serif;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 1px;
  padding: 2px 8px;
  border-radius: 2px;
}
.strat-pnl {
  font-family: 'Bebas Neue', sans-serif;
  font-size: 30px;
  letter-spacing: 1px;
  line-height: 1;
  margin: 2px 0;
}
.strat-stats {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-family: 'IBM Plex Mono', monospace;
  font-size: 11px;
  color: var(--text2);
}
.ss-row { display: flex; gap: 10px; flex-wrap: wrap; }
.ss-k   { color: var(--text3); }
.ss-v   { color: var(--text); }
.strat-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: auto;
  padding-top: 6px;
  border-top: 1px solid var(--border);
  font-family: 'Barlow Condensed', sans-serif;
  font-size: 10px;
  letter-spacing: 1px;
  text-transform: uppercase;
}
.live-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}
.strat-status { color: var(--text2); font-weight: 600; }
.strat-window {
  margin-left: auto;
  color: var(--text3);
  font-family: 'IBM Plex Mono', monospace;
  font-size: 10px;
}

/* ── Hour-of-day grid ───────────────────────────────────────────── */
.hh-section {
  background: var(--bg2);
  border: 1px solid var(--border);
  padding: 14px 16px;
  margin-bottom: 16px;
}
.hh-head {
  font-family: 'Barlow Condensed', sans-serif;
  font-size: 11px;
  letter-spacing: 1.5px;
  color: var(--text2);
  font-weight: 700;
  text-transform: uppercase;
  margin-bottom: 10px;
}
.hh-row {
  display: grid;
  grid-template-columns: repeat(12, 1fr);
  gap: 4px;
  margin-bottom: 4px;
}
.hh-row:last-child { margin-bottom: 0; }
.hh-cell {
  border: 1px solid var(--border);
  border-radius: 3px;
  padding: 6px 4px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  min-height: 44px;
  justify-content: center;
}
.hh-hour {
  font-family: 'Barlow Condensed', sans-serif;
  font-size: 11px;
  letter-spacing: 0.8px;
  color: var(--text3);
  font-weight: 600;
}
.hh-val {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 10px;
}
.hh-empty .hh-val { color: var(--text3); }
.hh-pos {
  background: var(--greenl);
  border-color: var(--green);
}
.hh-pos .hh-val { color: var(--green2); }
.hh-neg {
  background: var(--redl);
  border-color: var(--red);
}
.hh-neg .hh-val { color: var(--red2); }
.hh-live {
  background: var(--amberl);
  border: 2px solid var(--amber);
}
.hh-live .hh-hour { color: var(--gold2); }
.hh-live .hh-val  { color: var(--gold2); font-weight: 600; }

/* ── Recent closes table ────────────────────────────────────────── */
.rc-section {
  background: var(--surface);
  border: 1px solid var(--border);
  padding: 14px 16px;
  margin-bottom: 16px;
}
.rc-head {
  font-family: 'Barlow Condensed', sans-serif;
  font-size: 11px;
  letter-spacing: 1.5px;
  color: var(--text2);
  font-weight: 700;
  text-transform: uppercase;
  margin-bottom: 10px;
}
.rc-table {
  width: 100%;
  border-collapse: collapse;
}
.rc-table th {
  font-family: 'Barlow Condensed', sans-serif;
  font-size: 10px;
  letter-spacing: 1.2px;
  color: var(--text3);
  font-weight: 700;
  text-transform: uppercase;
  text-align: left;
  padding: 6px 8px;
  border-bottom: 1px solid var(--border);
}
.rc-table th.rc-pnl-col { text-align: right; }
.rc-row td {
  padding: 6px 8px;
  border-bottom: 1px solid var(--bg2);
  font-family: 'IBM Plex Mono', monospace;
  font-size: 11px;
}
.rc-row.rc-open td { background: rgba(196,128,26,0.10); }
.rc-time  { color: var(--text2); }
.rc-price { color: var(--text); }
.rc-held  { color: var(--text3); }
.rc-pnl   { text-align: right; font-weight: 600; }
.rc-tag {
  font-family: 'Barlow Condensed', sans-serif;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 1px;
  padding: 1px 6px;
  border-radius: 2px;
  border: 1px solid;
  text-transform: uppercase;
}
.rc-outcome {
  font-family: 'Barlow Condensed', sans-serif;
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.8px;
  text-transform: uppercase;
}
.rc-outcome-target  { color: var(--green2); }
.rc-outcome-stop    { color: var(--red2); }
.rc-outcome-flat    { color: var(--text3); }
.rc-outcome-open    { color: var(--amber); }
.rc-empty {
  text-align: center;
  padding: 16px;
  color: var(--text3);
  font-family: 'Barlow Condensed', sans-serif;
  font-size: 12px;
  letter-spacing: 1.2px;
}

/* ── Strategy-card charge meter (eval_log per-strategy charge_pct) ── */
.strat-charge {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 6px 0 2px;
}
.ch-label {
  font-family: 'Barlow Condensed', sans-serif;
  font-size: 10px;
  letter-spacing: 1.2px;
  color: var(--text3);
  min-width: 50px;
}
.ch-bar {
  flex: 1;
  height: 6px;
  background: var(--bg3);
  border-radius: 2px;
  overflow: hidden;
}
.ch-fill {
  height: 100%;
  transition: width 0.3s ease;
}
.ch-pct {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 11px;
  font-weight: 600;
  min-width: 36px;
  text-align: right;
}

/* ── FLEET ACTIVITY panel (eval_log feed) ─────────────────────────── */
.ev-section {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 14px 16px;
  margin: 12px 0;
}
.ev-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 10px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--border);
}
.ev-title {
  font-family: 'Bebas Neue', sans-serif;
  font-size: 14px;
  letter-spacing: 2px;
  color: var(--text);
}
.ev-counts {
  font-family: 'Barlow Condensed', sans-serif;
  font-size: 11px;
  letter-spacing: 1px;
  color: var(--text2);
}
.ev-counts-k { color: var(--text3); text-transform: uppercase; margin-right: 4px; }
.ev-counts-v { color: var(--text); font-family: 'IBM Plex Mono', monospace; margin-right: 12px; }
.ev-sep      { color: var(--text3); margin: 0 4px; }
.ev-near-count { color: var(--gold2); }
.ev-table {
  width: 100%;
  border-collapse: collapse;
  font-family: 'IBM Plex Mono', monospace;
  font-size: 11px;
}
.ev-table th {
  text-align: left;
  font-family: 'Barlow Condensed', sans-serif;
  font-size: 10px;
  letter-spacing: 1.2px;
  color: var(--text3);
  padding: 4px 6px;
  border-bottom: 1px solid var(--border);
}
.ev-table td {
  padding: 5px 6px;
  border-bottom: 1px solid rgba(74,71,64,0.4);
  color: var(--text2);
  vertical-align: middle;
}
.ev-time { color: var(--text3); white-space: nowrap; }
.ev-tag {
  font-family: 'Barlow Condensed', sans-serif;
  font-size: 10px;
  letter-spacing: 1px;
  padding: 1px 6px;
  border: 1px solid;
  border-radius: 2px;
}
.ev-outcome {
  font-family: 'Barlow Condensed', sans-serif;
  font-size: 10px;
  letter-spacing: 1px;
  font-weight: 700;
  padding: 1px 6px;
  border-radius: 2px;
}
.ev-pass  { background: var(--surface2); color: var(--text2); }
.ev-near  { background: var(--gold);     color: var(--text);  }
.ev-entry { background: var(--green);    color: var(--text);  }
.ev-hold  { background: var(--bg3);      color: var(--text3); }
.ev-star  { color: var(--gold2); margin-left: 4px; font-size: 12px; }
.ev-side { text-align: center; color: var(--text3); }
.ev-reason { color: var(--text2); }
.ev-empty {
  color: var(--text3);
  font-style: italic;
  text-align: center;
  padding: 12px;
}

/* ── Footer ─────────────────────────────────────────────────────── */
.footer {
  background: var(--bg3);
  border-top: 1px solid var(--border);
  padding: 12px 20px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: auto;
}
.ft-left {
  font-family: 'Barlow Condensed', sans-serif;
  font-size: 11px;
  letter-spacing: 1.5px;
  color: var(--text2);
  text-transform: uppercase;
}
.ft-right {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 10px;
  color: var(--text3);
}
"""


# ─────────────────────────── entry point ────────────────────────────


def render_overview(sb, *, token: str | None = None,
                    bucket: str = "all") -> str:
    """Main entry — renders the full Ghost Dog Capital dashboard.

    Signature preserved for backwards compatibility with web/app.py.
    `bucket` is currently unused (the bucket selector was removed in the
    Ghost Dog redesign); kept on the signature so callers that pass it
    don't break.
    """
    _ = bucket   # explicitly unused

    heartbeats  = _fetch_heartbeats(sb)
    strategies  = _fetch_strategies(sb)
    snaps       = _fetch_perf_snapshots(sb)
    closes_all  = _fetch_recent_closes(sb)
    ks_state    = _fetch_kill_switch_state(sb)
    eval_counts = _fetch_eval_log_counts(sb)
    eval_rows   = _fetch_eval_log_activity(sb, limit=12)
    charge_by   = _fetch_charge_pct_by_strategy(sb)

    body = (
        _render_topbar(heartbeats)
        + '<div class="page">'
        + _render_ticker_bar(closes_all, heartbeats, ks_state, token)
        + _render_mll_tracker(closes_all)
        + _render_active_trade(heartbeats)
        + _render_strategy_cards(heartbeats, strategies, snaps, charge_by)
        + _render_eval_activity(eval_counts, eval_rows)
        + _render_hour_grid(closes_all)
        + _render_recent_closes(closes_all, heartbeats)
        + '</div>'
        + _render_footer()
    )

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta http-equiv="refresh" content="10" />
<title>Ghost Dog Capital · Fleet</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=IBM+Plex+Mono:wght@400;500;600&family=Barlow+Condensed:wght@400;600;700&display=swap">
<style>{_CSS}</style>
</head><body>
{body}
</body></html>
"""
