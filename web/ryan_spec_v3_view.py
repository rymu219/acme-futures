"""Ryan-Spec OOS-v3 watcher page.

Read-only HTML view rendered by the FastAPI watcher (web/app.py). Reads from
the `ryan_spec_v3_trades` Supabase table using the service-role key. Same
visual language as the main page: dark theme, KPI cards, status pills,
Recent table. Mobile-responsive, 5-second auto-refresh.

Mounted at /ryan-spec-v3?token=...
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
UTC = ZoneInfo("UTC")
MAX_ROWS = 40

# OOS-v3 reference numbers (used to color "is paper drifting from OOS")
OOS_REF = {
    "pf": 2.30,
    "wr": 27.0,
    "trades_per_day": 90.1,
    "exit_dist": {"opposite_signal": 0.528, "session_end": 0.425, "stop": 0.047},
}

EXIT_COLORS = {
    "stop":              "#ef4444",
    "stop_breakeven":    "#f97316",
    "opposite_signal":   "#06b6d4",
    "opposite_signal_loss": "#a78bfa",
    "session_end":       "#eab308",
    "time_stop":         "#94a3b8",
    "broker_error":      "#dc2626",
}

# Heartbeat staleness thresholds. The v3 runner writes a heartbeat row per
# variant on every closed 2-minute bar, so a fresh row is normally <2m old.
# > LIVE_MAX_S: amber "stale", > OFFLINE_MIN_S: red "offline".
HEARTBEAT_LIVE_MAX_S = 240    # 4 min — one missed bar is fine
HEARTBEAT_OFFLINE_MIN_S = 900  # 15 min — clearly dead

HEARTBEAT_COLORS = {
    "LIVE":    "#16a34a",
    "STALE":   "#f59e0b",
    "OFFLINE": "#dc2626",
    "UNKNOWN": "#64748b",
}


def _money(n: float | None) -> str:
    if n is None:
        return "—"
    sign = "-" if n < -0.005 else ""
    return f"{sign}${abs(n):,.2f}"


def _ago(ts_iso: str | None) -> str:
    if not ts_iso:
        return ""
    try:
        ts = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    except Exception:
        return ""
    delta = datetime.now(UTC) - ts
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s//60}m"
    if s < 86400:
        return f"{s//3600}h"
    return f"{s//86400}d"


def _ct_str(ts_iso: str | None) -> str:
    if not ts_iso:
        return ""
    try:
        ts = datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).astimezone(CT)
    except Exception:
        return ""
    return ts.strftime("%H:%M:%S")


def _topstep_trading_date(now_ct: datetime) -> date:
    return now_ct.date() if now_ct.hour < 17 else (now_ct.date() + timedelta(days=1))


def _fetch_trades(sb, *, mode: str, strategy_id: str, since_iso: str,
                  limit: int = 1000) -> list[dict]:
    try:
        res = (
            sb.table("ryan_spec_v3_trades")
            .select("*")
            .eq("mode", mode)
            .eq("strategy_id", strategy_id)
            .gte("bar_ts", since_iso)
            .order("bar_ts", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data or []
    except Exception:
        return []


def _fetch_open(sb, *, mode: str, strategy_id: str) -> dict | None:
    try:
        res = (
            sb.table("ryan_spec_v3_trades")
            .select("*")
            .eq("mode", mode)
            .eq("strategy_id", strategy_id)
            .is_("exit_ts", "null")
            .not_.is_("entry_ts", "null")
            .order("entry_ts", desc=True)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None
    except Exception:
        return None


def _fetch_heartbeats(sb) -> dict[str, dict]:
    """Read every runtime_heartbeats row, keyed by service name. The v3 runner
    writes one row per variant (service == strategy_id, e.g. 'v3-canon').
    Returns {} on read failure so a missing table never breaks the page."""
    try:
        res = sb.table("runtime_heartbeats").select("*").execute()
        rows = res.data or []
    except Exception:
        return {}
    return {r["service"]: r for r in rows if r.get("service")}


def _heartbeat_status(hb: dict | None, now_utc: datetime) -> dict:
    """Classify a heartbeat row as LIVE / STALE / OFFLINE / UNKNOWN.
    Returns {state, color, age_s, ago, auth_ok, errors}."""
    if not hb or not hb.get("ts"):
        return {"state": "UNKNOWN", "color": HEARTBEAT_COLORS["UNKNOWN"],
                "age_s": None, "ago": "—", "auth_ok": None, "errors": 0}
    try:
        ts = datetime.fromisoformat(hb["ts"].replace("Z", "+00:00"))
    except Exception:
        return {"state": "UNKNOWN", "color": HEARTBEAT_COLORS["UNKNOWN"],
                "age_s": None, "ago": "—", "auth_ok": None, "errors": 0}
    age_s = max(0, int((now_utc - ts).total_seconds()))
    if age_s < HEARTBEAT_LIVE_MAX_S:
        state = "LIVE"
    elif age_s < HEARTBEAT_OFFLINE_MIN_S:
        state = "STALE"
    else:
        state = "OFFLINE"
    # If auth has failed, treat as OFFLINE regardless of recency — the runner
    # may be writing heartbeats but unable to actually trade.
    if hb.get("auth_ok") is False:
        state = "OFFLINE"
    return {
        "state": state,
        "color": HEARTBEAT_COLORS[state],
        "age_s": age_s,
        "ago": _ago(hb["ts"]),
        "auth_ok": hb.get("auth_ok"),
        "errors": int(hb.get("consecutive_errors") or 0),
    }


def _fleet_heartbeat_pill(heartbeats: dict[str, dict], now_utc: datetime) -> str:
    """Top-of-page pill summarising the fleet's heartbeat state. Shows the
    worst variant's status so a single dead runner is impossible to miss."""
    statuses = [_heartbeat_status(heartbeats.get(sid), now_utc)
                for sid in KNOWN_VARIANTS]
    severity = {"LIVE": 0, "STALE": 1, "UNKNOWN": 2, "OFFLINE": 3}
    worst = max(statuses, key=lambda s: severity[s["state"]])
    n_live = sum(1 for s in statuses if s["state"] == "LIVE")
    n_total = len(KNOWN_VARIANTS)
    label = {
        "LIVE":    f"RUNNER LIVE · {n_live}/{n_total}",
        "STALE":   f"RUNNER STALE · {n_live}/{n_total} live",
        "OFFLINE": f"RUNNER OFFLINE · {n_live}/{n_total} live",
        "UNKNOWN": f"NO HEARTBEAT · {n_live}/{n_total} live",
    }[worst["state"]]
    # Hover/title shows the per-variant breakdown so the operator can see which
    # variant is the laggard without leaving the overview.
    title = " · ".join(
        f"{sid}={s['state']}({s['ago']})"
        for sid, s in zip(KNOWN_VARIANTS, statuses)
    )
    return (
        f'<span class="pill" title="{title}" '
        f'style="color:{worst["color"]};border-color:{worst["color"]}55;'
        f'background:{worst["color"]}11">'
        f'<span class="dot" style="background:{worst["color"]}"></span>'
        f'{label}</span>'
    )


def _fetch_strategy_summaries(sb, *, mode: str, since_iso: str) -> list[dict]:
    """For the multi-variant nav: pull today's P&L per strategy_id so the
    nav buttons can show each variant's running total at a glance."""
    try:
        res = (
            sb.table("ryan_spec_v3_trades")
            .select("strategy_id,pnl_dollars,exit_reason")
            .eq("mode", mode)
            .gte("bar_ts", since_iso)
            .limit(20_000)
            .execute()
        )
        rows = res.data or []
    except Exception:
        return []
    by_strat: dict[str, dict] = {}
    for r in rows:
        sid = r.get("strategy_id") or "?"
        bucket = by_strat.setdefault(sid, {"strategy_id": sid, "n": 0, "total": 0.0})
        if r.get("exit_reason"):
            bucket["n"] += 1
            bucket["total"] += r.get("pnl_dollars") or 0.0
    return sorted(by_strat.values(), key=lambda b: b["strategy_id"])


def _settled_metrics(rows: list[dict]) -> dict:
    settled = [r for r in rows if r.get("exit_reason")]
    n = len(settled)
    if n == 0:
        return {"n": 0, "wins": 0, "losses": 0, "wr": 0.0, "pf": None,
                "exp": 0.0, "total": 0.0, "exit_dist": {}}
    wins = sum(1 for r in settled if (r.get("pnl_dollars") or 0) > 0)
    losses = sum(1 for r in settled if (r.get("pnl_dollars") or 0) < 0)
    wr = wins / n * 100
    gw = sum(r["pnl_dollars"] for r in settled if (r.get("pnl_dollars") or 0) > 0)
    gl = abs(sum(r["pnl_dollars"] for r in settled if (r.get("pnl_dollars") or 0) < 0))
    pf = (gw / gl) if gl > 0 else None
    total = sum((r.get("pnl_dollars") or 0) for r in settled)
    exp = total / n
    # Exit distribution
    counts: dict[str, int] = {}
    for r in settled:
        k = r.get("exit_reason") or "?"
        counts[k] = counts.get(k, 0) + 1
    exit_dist = {k: v / n for k, v in counts.items()}
    return {"n": n, "wins": wins, "losses": losses, "wr": wr, "pf": pf,
            "exp": exp, "total": total, "exit_dist": exit_dist}


def _exit_dist_pill_html(exit_dist: dict[str, float]) -> str:
    if not exit_dist:
        return ""
    parts = []
    for k in ("opposite_signal", "session_end", "stop", "stop_breakeven",
              "time_stop", "opposite_signal_loss"):
        v = exit_dist.get(k, 0.0)
        if v < 0.005:
            continue
        color = EXIT_COLORS.get(k, "#94a3b8")
        parts.append(
            f'<span style="color:{color};font-weight:600">{v*100:.0f}%</span> '
            f'<span class="dim">{k}</span>'
        )
    return "  ·  ".join(parts)


KNOWN_VARIANTS = ("v3-canon", "v3-trail", "v3-min2bar", "v3-armor", "v3-pctile")

# Promotion-gate thresholds — kept in sync with src/acme/ryan_spec/v3_promotion.py.
# Web has its own minimal requirements.txt (no pandas, no acme), so the gate
# logic is re-implemented here in pure python rather than importing from
# the canonical module. Update both when changing.
GATE_MIN_SETTLED = 200
GATE_MIN_PF = 1.5
GATE_MAX_AVG_SLIPPAGE_TICKS = 1.5
GATE_MIN_OPPOSITE_PCT = 0.40

VERDICT_COLORS = {
    "PROMOTE_LIVE": "#16a34a",
    "EXTEND_PAPER": "#94a3b8",
    "INVESTIGATE":  "#eab308",
    "HALT":         "#dc2626",
}


def _compute_verdict_pure(rows: list[dict]) -> dict:
    """Pure-python version of evaluate_paper_promotion. Mirrors the logic
    in src/acme/ryan_spec/v3_promotion.py:evaluate_paper_promotion. Returns
    a dict with keys: verdict, reason, settled, pf, opposite_pct, avg_slip.
    """
    settled = [r for r in rows if r.get("exit_reason")]
    n = len(settled)
    if n < GATE_MIN_SETTLED:
        return {
            "verdict": "EXTEND_PAPER",
            "reason": f"{n}/{GATE_MIN_SETTLED}",
            "settled": n,
            "pf": None,
            "opposite_pct": None,
            "avg_slip": None,
        }
    wins = sum(1 for r in settled if (r.get("pnl_dollars") or 0) > 0)
    gw = sum(r["pnl_dollars"] for r in settled if (r.get("pnl_dollars") or 0) > 0)
    gl = abs(sum(r["pnl_dollars"] for r in settled if (r.get("pnl_dollars") or 0) < 0))
    pf = gw / max(gl, 0.01)
    opp = sum(1 for r in settled if r.get("exit_reason") == "opposite_signal") / n
    slips = [r["slippage_ticks"] for r in settled
             if r.get("slippage_ticks") is not None]
    avg_slip = (sum(slips) / len(slips)) if slips else None
    base = {
        "settled": n, "pf": pf, "opposite_pct": opp, "avg_slip": avg_slip,
        "win_rate": wins / n,
    }
    if pf < GATE_MIN_PF:
        return {**base, "verdict": "HALT", "reason": f"PF {pf:.2f} < {GATE_MIN_PF}"}
    if avg_slip is not None and avg_slip > GATE_MAX_AVG_SLIPPAGE_TICKS:
        return {**base, "verdict": "HALT",
                "reason": f"slippage {avg_slip:.2f}t > {GATE_MAX_AVG_SLIPPAGE_TICKS}t"}
    if opp < GATE_MIN_OPPOSITE_PCT:
        return {**base, "verdict": "INVESTIGATE",
                "reason": f"opposite-exit {opp*100:.0f}% < {int(GATE_MIN_OPPOSITE_PCT*100)}%"}
    return {**base, "verdict": "PROMOTE_LIVE", "reason": "all gates pass"}


def _fetch_all_paper_for_gate(sb, *, since_iso: str) -> list[dict]:
    """All paper trades across strategies for the gate window."""
    try:
        res = (
            sb.table("ryan_spec_v3_trades")
            .select("strategy_id,exit_reason,pnl_dollars,slippage_ticks")
            .eq("mode", "paper")
            .gte("bar_ts", since_iso)
            .limit(20_000)
            .execute()
        )
        return res.data or []
    except Exception:
        return []


def _verdicts_per_strategy(rows: list[dict]) -> dict[str, dict]:
    """Group rows by strategy_id and run the gate per group."""
    by_strat: dict[str, list[dict]] = {}
    for r in rows:
        sid = r.get("strategy_id") or "unknown"
        by_strat.setdefault(sid, []).append(r)
    return {sid: _compute_verdict_pure(rs) for sid, rs in by_strat.items()}


def _variant_nav_html(summaries: list[dict], *, current: str,
                      token: str | None, mode: str,
                      verdicts: dict[str, dict]) -> str:
    """Render a horizontal pill nav listing each variant + its 24h P&L,
    with the active one styled distinctly."""
    by_id = {s["strategy_id"]: s for s in summaries}
    parts = []
    for sid in KNOWN_VARIANTS:
        s = by_id.get(sid, {"n": 0, "total": 0.0})
        active = sid == current
        total = s["total"]
        n = s["n"]
        # Color: green if positive, red if negative, gray if zero/no trades
        if total > 0.005:
            tcolor = "#16a34a"
        elif total < -0.005:
            tcolor = "#dc2626"
        else:
            tcolor = "#94a3b8"
        bg = "#0f172a" if active else "#020617"
        # Verdict tints the LEFT border so the gate state is visible at a glance
        v = verdicts.get(sid, {"verdict": "EXTEND_PAPER"})
        vcolor = VERDICT_COLORS.get(v["verdict"], "#94a3b8")
        border = "#38bdf8" if active else "#1e293b"
        weight = "700" if active else "500"
        text = "#e2e8f0" if active else "#94a3b8"
        params = []
        if token:
            params.append(f"token={token}")
        if mode != "paper":
            params.append(f"mode={mode}")
        params.append(f"strategy_id={sid}")
        href = f"/ryan-spec-v3?{'&'.join(params)}"
        parts.append(
            f'<a href="{href}" style="text-decoration:none" '
            f'title="{v["verdict"]}: {v.get("reason", "")}">'
            f'<span class="meta-pill" style="background:{bg};'
            f'border:1px solid {border};border-left:3px solid {vcolor};'
            f'color:{text};font-weight:{weight}">'
            f'{sid}'
            f' <span style="color:{tcolor}" class="mono">{_money(total)}</span>'
            f' <span class="dim">({n})</span>'
            f'</span></a>'
        )
    return "".join(parts)


_SHARED_CSS = """
  :root {
    --bg-0: #0a0f1c; --bg-1: #0f1729; --bg-2: #131c33;
    --border: #1f2a44; --border-strong: #2c3a5a;
    --text: #e8eef9; --dim: #7f8aa8; --dim-2: #5a6789;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg-0); color: var(--text);
         font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
         -webkit-font-smoothing: antialiased; }
  .wrap { max-width: 1200px; margin: 0 auto; padding: 18px; padding-bottom: 32px; }
  .topbar { display: flex; justify-content: space-between; align-items: center;
             margin-bottom: 14px; }
  .brand { font-size: 13px; letter-spacing: 2px; color: var(--dim); text-transform: uppercase; }
  .brand strong { color: var(--text); }
  .clock { font-size: 12px; color: var(--dim-2); }
  .breadcrumb { font-size: 12px; color: var(--dim-2); margin-bottom: 12px; }
  .breadcrumb a { color: var(--dim); text-decoration: none; }
  .breadcrumb a:hover { color: var(--text); }

  .statusrow { display: flex; gap: 8px; flex-wrap: wrap; align-items: center;
                margin-bottom: 16px; }
  .pill { display: inline-flex; align-items: center; gap: 6px;
           padding: 5px 11px; border-radius: 999px;
           font-size: 11px; font-weight: 700; letter-spacing: 1px;
           text-transform: uppercase; border: 1px solid var(--border-strong); }
  .pill .dot { width: 7px; height: 7px; border-radius: 999px; }
  .pill-ok { color: #34d399; background: rgba(16,185,129,0.08); border-color: #166534; }
  .pill-ok .dot { background: #34d399; }
  .pill-active { color: #38bdf8; background: rgba(56,189,248,0.08); border-color: #075985; }
  .pill-active .dot { background: #38bdf8; animation: pulse 1.4s infinite; }
  .pill-blocked { color: #fbbf24; background: rgba(251,191,36,0.08); border-color: #854d0e; }
  .pill-blocked .dot { background: #fbbf24; }
  @keyframes pulse { 0%,100% { opacity:1 } 50% { opacity:0.4 } }
  .meta-pill { font-size: 11px; color: var(--dim); padding: 4px 10px;
                background: var(--bg-2); border: 1px solid var(--border);
                border-radius: 999px; }
  .meta-pill.drift { color: #fbbf24; border-color: #854d0e; }
  .meta-pill strong { color: var(--text); }

  .stats { display: grid; grid-template-columns: repeat(4, minmax(0,1fr));
            gap: 10px; margin-bottom: 16px; }
  .card { background: var(--bg-1); border: 1px solid var(--border);
           border-radius: 14px; padding: 14px 16px; }
  .card .label { font-size: 10.5px; letter-spacing: 1.6px;
                  text-transform: uppercase; color: var(--dim); font-weight: 600; }
  .card .value { font-size: 26px; font-weight: 600; margin-top: 5px;
                  font-family: ui-monospace, SF Mono, Menlo, monospace;
                  font-variant-numeric: tabular-nums; }
  .card .meta { font-size: 11px; color: var(--dim-2); margin-top: 4px;
                 font-family: ui-monospace, SF Mono, Menlo, monospace; }

  .events { background: var(--bg-1); border: 1px solid var(--border);
             border-radius: 14px; overflow: hidden; }
  .events-header { padding: 10px 14px; border-bottom: 1px solid var(--border);
                    font-size: 11px; letter-spacing: 1.4px; text-transform: uppercase;
                    color: var(--dim); font-weight: 600;
                    display:flex; justify-content:space-between; align-items:center; gap: 16px; }
  .events-header .right { font-size: 11px; letter-spacing: 0.4px;
                           text-transform: none; color: var(--dim-2); }
  table { width:100%; border-collapse: collapse; }
  th, td { padding: 10px 14px; text-align: left;
            border-bottom: 1px solid var(--border); font-size: 13px; }
  th { background: rgba(255,255,255,0.02); color: var(--dim-2);
        text-transform: uppercase; font-size: 10.5px; letter-spacing: 1.2px;
        font-weight: 600; }
  tbody tr:hover { background: rgba(255,255,255,0.025); }
  tr:last-child td { border-bottom: none; }
  .mono { font-family: ui-monospace, SF Mono, Menlo, monospace;
           font-variant-numeric: tabular-nums; }
  .dim { color: var(--dim-2); }
  .kind { font-weight: 600; font-size: 12px; letter-spacing: 0.3px; }

  /* Multi-variant overview grid + panels (added 2026-05-06) */
  .panels { display: grid; grid-template-columns: repeat(3, minmax(0,1fr));
             gap: 12px; margin-bottom: 18px; }
  .panel { background: var(--bg-1); border: 1px solid var(--border);
            border-radius: 14px; padding: 14px 16px; text-decoration: none;
            color: inherit; transition: border-color 0.15s ease, transform 0.15s ease;
            display: block; }
  .panel:hover { border-color: var(--border-strong); transform: translateY(-1px); }
  .panel.armed { border-left: 3px solid #38bdf8; }
  .panel-head { display: flex; justify-content: space-between; align-items: center;
                 gap: 8px; margin-bottom: 10px; }
  .panel-name { font-size: 13px; font-weight: 700; letter-spacing: 0.4px; }
  .panel-status { font-size: 10px; color: var(--dim);
                   text-transform: uppercase; letter-spacing: 1px; font-weight: 700; }
  .panel-pnl { font-size: 22px; font-weight: 600;
                font-family: ui-monospace, SF Mono, Menlo, monospace;
                font-variant-numeric: tabular-nums; margin-bottom: 2px; }
  .panel-meta { font-size: 11px; color: var(--dim-2);
                 font-family: ui-monospace, SF Mono, Menlo, monospace; }
  .panel-row { display: flex; justify-content: space-between;
                font-size: 11px; color: var(--dim); margin-top: 6px;
                font-family: ui-monospace, SF Mono, Menlo, monospace; }
  .panel-row strong { color: var(--text); font-weight: 500; }
  .panel-verdict { font-size: 10px; font-weight: 700; letter-spacing: 1px;
                    text-transform: uppercase; padding: 3px 8px; border-radius: 6px;
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }

  /* Pagination */
  .pagination { padding: 10px 14px; display: flex; gap: 12px;
                 align-items: center; justify-content: center;
                 background: rgba(255,255,255,0.02); font-size: 12px; }
  .page-link { color: var(--dim); text-decoration: none; padding: 4px 10px;
                border: 1px solid var(--border); border-radius: 6px; }
  .page-link:hover { color: var(--text); border-color: var(--border-strong); }

  .footer { margin-top: 16px; color: var(--dim-2); font-size: 11px; text-align: center; }

  @media (max-width: 880px) {
    .stats { grid-template-columns: repeat(2, minmax(0,1fr)); }
    .panels { grid-template-columns: repeat(2, minmax(0,1fr)); }
    .col-ago, .col-mfe, .col-mae, .col-bars { display: none; }
  }
  @media (max-width: 520px) {
    .wrap { padding: 12px; padding-bottom: 28px; }
    .stats { gap: 8px; }
    .panels { grid-template-columns: 1fr; gap: 10px; }
    .card { padding: 12px 14px; border-radius: 12px; }
    .card .value { font-size: 22px; }
    th, td { padding: 9px 10px; font-size: 12px; }
  }
"""


def _fetch_trades_paginated(
    sb, *, mode: str, strategy_id: str | None, since_iso: str,
    page: int, page_size: int = 50,
) -> tuple[list[dict], int]:
    """Return (rows, total_count) for the requested page. page is 1-indexed.
    strategy_id=None means 'all variants' (no filter on strategy_id)."""
    try:
        q = (sb.table("ryan_spec_v3_trades")
             .select("*", count="exact")
             .eq("mode", mode)
             .gte("bar_ts", since_iso))
        if strategy_id:
            q = q.eq("strategy_id", strategy_id)
        offset = max(0, (page - 1) * page_size)
        res = (q.order("bar_ts", desc=True)
               .range(offset, offset + page_size - 1)
               .execute())
        return res.data or [], res.count or 0
    except Exception:
        return [], 0


def _compute_per_variant_summary(
    sb, *, mode: str, strategy_id: str, since_iso: str, today_iso: str,
) -> dict:
    """Pull the headline numbers a variant's panel shows. Returns:
        {today: settled_metrics, week: settled_metrics, open_pos, last_trade_ts}"""
    week_rows = _fetch_trades(sb, mode=mode, strategy_id=strategy_id,
                              since_iso=since_iso, limit=2000)
    today_rows = [r for r in week_rows
                  if (r.get("bar_ts") or "") >= today_iso]
    open_pos = _fetch_open(sb, mode=mode, strategy_id=strategy_id)
    last_trade_ts = week_rows[0].get("bar_ts") if week_rows else None
    return {
        "today": _settled_metrics(today_rows),
        "week": _settled_metrics(week_rows),
        "open_pos": open_pos,
        "last_trade_ts": last_trade_ts,
    }


def _pagination_html(*, page: int, total: int, page_size: int,
                     base_params: dict, page_param: str) -> str:
    """Render prev / next links for a paginated table. base_params is the
    other query string params to preserve; page_param is the key for the
    page number in the URL (e.g., 'trades_page')."""
    if total <= page_size:
        return ""
    last_page = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, last_page))
    start = (page - 1) * page_size + 1
    end = min(page * page_size, total)

    def _url(p: int) -> str:
        params = {**base_params, page_param: p}
        return "?" + "&".join(f"{k}={v}" for k, v in params.items()
                              if v is not None and v != "")

    prev_html = (f'<a href="{_url(page - 1)}" class="page-link">&larr; prev</a>'
                 if page > 1 else
                 '<span class="page-link dim">&larr; prev</span>')
    next_html = (f'<a href="{_url(page + 1)}" class="page-link">next &rarr;</a>'
                 if page < last_page else
                 '<span class="page-link dim">next &rarr;</span>')
    return (
        f'<div class="pagination">'
        f'  {prev_html}'
        f'  <span class="dim">page {page} / {last_page}</span>'
        f'  <span class="dim">({start}–{end} of {total:,})</span>'
        f'  {next_html}'
        f'</div>'
    )


def _trade_row_html(r: dict, *, show_strategy: bool = False) -> str:
    """One <tr> for the trades table. Optionally includes a strategy_id col
    (for the overview / mixed view)."""
    ts = _ct_str(r.get("bar_ts"))
    ago = _ago(r.get("bar_ts"))
    direction = (r.get("direction") or "").upper()
    dir_color = "#16a34a" if direction == "LONG" else "#dc2626"
    entry = r.get("entry_price")
    exit_price = r.get("exit_price")
    exit_reason = r.get("exit_reason") or ""
    ex_color = EXIT_COLORS.get(exit_reason, "#e2e8f0") if exit_reason else "#94a3b8"
    pnl = r.get("pnl_dollars")
    if pnl is None:
        pnl_str = '<span class="dim">—</span>'
    else:
        pcolor = "#16a34a" if pnl > 0 else ("#dc2626" if pnl < 0 else "#e2e8f0")
        pnl_str = f'<span style="color:{pcolor}" class="mono">{_money(pnl)}</span>'
    mfe = r.get("mfe_atr")
    mae = r.get("mae_atr")
    bars = r.get("bars_held")
    strat_cell = (
        f"<td class='mono dim'>{r.get('strategy_id') or '—'}</td>"
        if show_strategy else ""
    )
    return (
        "<tr>"
        f"{strat_cell}"
        f"<td class='mono'>{ts}</td>"
        f"<td class='dim mono col-ago'>{ago}</td>"
        f"<td><span class='kind' style='color:{dir_color}'>{direction}</span></td>"
        f"<td class='mono'>{_money(entry)}</td>"
        f"<td class='mono'>{_money(exit_price) if exit_price is not None else '—'}</td>"
        f"<td><span class='kind' style='color:{ex_color}'>{exit_reason or 'open'}</span></td>"
        f"<td>{pnl_str}</td>"
        f"<td class='dim mono col-mfe'>"
        f"{f'{mfe:.2f}' if mfe is not None else '—'}</td>"
        f"<td class='dim mono col-mae'>"
        f"{f'{mae:.2f}' if mae is not None else '—'}</td>"
        f"<td class='dim mono col-bars'>{bars if bars is not None else '—'}</td>"
        "</tr>"
    )


def render_overview(
    sb, *,
    mode: str = "paper",
    token: str | None = None,
    trades_page: int = 1,
    trades_strategy: str | None = None,
    page_size: int = 50,
) -> str:
    """Multi-variant overview: 5 panels (one per strategy) + paginated
    all-trades table. Each panel links into the per-strategy detail view."""
    now_ct = datetime.now(CT)
    now_utc = datetime.now(UTC)
    since = (now_utc - timedelta(days=7)).isoformat()
    # "Today" = since midnight CT (so overnight trades after 00:00 CT count).
    # We convert midnight-CT to UTC for the bar_ts string comparison since
    # Supabase returns bar_ts in UTC.
    today_ct_date = now_ct.date()
    today_utc_floor = datetime(
        today_ct_date.year, today_ct_date.month, today_ct_date.day,
        tzinfo=CT,
    ).astimezone(UTC).isoformat()

    # Per-variant summaries
    summaries: dict[str, dict] = {
        sid: _compute_per_variant_summary(
            sb, mode=mode, strategy_id=sid,
            since_iso=since, today_iso=today_utc_floor,
        )
        for sid in KNOWN_VARIANTS
    }

    # Verdicts (per-strategy gate)
    gate_rows = _fetch_all_paper_for_gate(sb, since_iso=since)
    verdicts = _verdicts_per_strategy(gate_rows)

    # Heartbeats — surface a stale/offline runner at the top of the page so an
    # operator doesn't mistake the auto-refresh for "everything's fine".
    heartbeats = _fetch_heartbeats(sb)

    # Aggregate (today)
    agg_today_pnl = sum(s["today"]["total"] for s in summaries.values())
    agg_today_n = sum(s["today"]["n"] for s in summaries.values())
    agg_week_pnl = sum(s["week"]["total"] for s in summaries.values())
    agg_week_n = sum(s["week"]["n"] for s in summaries.values())
    n_in_position = sum(1 for s in summaries.values() if s["open_pos"])

    # Paginated trades — last 7d, optionally filtered by strategy
    trades_strategy_filter = trades_strategy if trades_strategy and trades_strategy != "all" else None
    page_rows, total_count = _fetch_trades_paginated(
        sb, mode=mode, strategy_id=trades_strategy_filter,
        since_iso=since, page=trades_page, page_size=page_size,
    )

    # ----- HTML pieces -----

    def _params_for_panel(sid: str) -> str:
        params = []
        if token:
            params.append(f"token={token}")
        params.append(f"strategy_id={sid}")
        if mode != "paper":
            params.append(f"mode={mode}")
        return "?" + "&".join(params)

    panel_html_parts: list[str] = []
    for sid in KNOWN_VARIANTS:
        s = summaries[sid]
        v = verdicts.get(sid, {"verdict": "EXTEND_PAPER", "reason": "0/200"})
        vcolor = VERDICT_COLORS.get(v["verdict"], "#94a3b8")
        hb = _heartbeat_status(heartbeats.get(sid), now_utc)
        in_pos = s["open_pos"] is not None
        # OFFLINE/STALE supersedes IN POSITION/FLAT in the panel status line —
        # if the runner is dead, that's the most important thing to see.
        if hb["state"] in ("OFFLINE", "STALE", "UNKNOWN"):
            status_text = f"{hb['state']} · last hb {hb['ago']}"
            status_color = hb["color"]
        elif in_pos:
            direction = (s["open_pos"].get("direction") or "?").upper()
            status_text = f"IN POSITION · {direction}"
            status_color = "#38bdf8"
        else:
            status_text = "FLAT"
            status_color = "var(--dim)"

        today_pnl = s["today"]["total"]
        today_color = "#16a34a" if today_pnl > 0.005 else (
            "#dc2626" if today_pnl < -0.005 else "var(--text)"
        )
        week_pnl = s["week"]["total"]
        week_color = "#16a34a" if week_pnl > 0.005 else (
            "#dc2626" if week_pnl < -0.005 else "var(--dim)"
        )
        pf = s["week"]["pf"]
        pf_str = f"{pf:.2f}" if pf is not None else "—"
        wr = s["week"]["wr"]
        wr_str = f"{wr:.0f}%" if s["week"]["n"] > 0 else "—"

        panel_class = "panel armed" if in_pos else "panel"
        panel_html_parts.append(
            f'<a class="{panel_class}" href="{_params_for_panel(sid)}">'
            f'  <div class="panel-head">'
            f'    <span class="panel-name">{sid}</span>'
            f'    <span class="panel-verdict" style="color:{vcolor};background:{vcolor}22">{v["verdict"]}</span>'
            f'  </div>'
            f'  <div class="panel-pnl" style="color:{today_color}">{_money(today_pnl)}</div>'
            f'  <div class="panel-meta">today · {s["today"]["n"]} trades</div>'
            f'  <div class="panel-row"><span>7-day</span>'
            f'    <strong style="color:{week_color}">{_money(week_pnl)}</strong></div>'
            f'  <div class="panel-row"><span>{s["week"]["n"]} trades</span>'
            f'    <strong>{wr_str} WR · PF {pf_str}</strong></div>'
            f'  <div class="panel-row"><span style="color:{status_color}">{status_text}</span>'
            f'    <strong class="dim">{v.get("reason", "")}</strong></div>'
            f'</a>'
        )

    panels_html = "".join(panel_html_parts)

    # Filter dropdown for the trades table
    filter_opts = []
    for opt in ("all", *KNOWN_VARIANTS):
        sel = " selected" if (trades_strategy or "all") == opt else ""
        filter_opts.append(f'<option value="{opt}"{sel}>{opt}</option>')
    filter_form = (
        '<form method="get" style="display:inline">'
        + (f'<input type="hidden" name="token" value="{token}">' if token else "")
        + (f'<input type="hidden" name="mode" value="{mode}">' if mode != "paper" else "")
        + 'filter: <select name="trades_strategy" onchange="this.form.submit()" '
          'style="background:var(--bg-2);color:var(--text);border:1px solid var(--border);'
          'padding:3px 6px;border-radius:6px;font-size:11px">'
        + "".join(filter_opts)
        + "</select></form>"
    )

    # Trades table
    show_strategy_col = trades_strategy_filter is None  # show col when "all"
    rows_html = "".join(_trade_row_html(r, show_strategy=show_strategy_col) for r in page_rows)
    strategy_th = "<th>strategy</th>" if show_strategy_col else ""
    table_html = (
        f'<table>'
        f'<thead><tr>{strategy_th}<th>CT</th><th class="col-ago">ago</th>'
        f'<th>dir</th><th>entry</th><th>exit</th><th>reason</th><th>P&amp;L</th>'
        f'<th class="col-mfe">MFE</th><th class="col-mae">MAE</th>'
        f'<th class="col-bars">bars</th></tr></thead>'
        f'<tbody>{rows_html}</tbody>'
        f'</table>'
    )
    pagination = _pagination_html(
        page=trades_page, total=total_count, page_size=page_size,
        base_params={
            "token": token,
            "mode": mode if mode != "paper" else None,
            "trades_strategy": trades_strategy if trades_strategy else None,
        },
        page_param="trades_page",
    )

    # Hero color for aggregate today P&L
    agg_color = "#16a34a" if agg_today_pnl > 0.005 else (
        "#dc2626" if agg_today_pnl < -0.005 else "var(--text)"
    )
    agg_week_color = "#16a34a" if agg_week_pnl > 0.005 else (
        "#dc2626" if agg_week_pnl < -0.005 else "var(--dim)"
    )

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta http-equiv="refresh" content="5">
<meta name="theme-color" content="#0b1220">
<title>Acme Futures · v3 fleet</title>
<style>{_SHARED_CSS}</style>
</head><body><div class="wrap">
  <div class="topbar">
    <div class="brand"><strong>ACME</strong> · FUTURES · v3 fleet</div>
    <div class="clock mono">{now_ct.strftime('%a %Y-%m-%d  %H:%M:%S CT')}</div>
  </div>

  <div class="statusrow">
    {_fleet_heartbeat_pill(heartbeats, now_utc)}
  </div>

  <div class="stats">
    <div class="card">
      <div class="label">Fleet Today</div>
      <div class="value mono" style="color:{agg_color}">{_money(agg_today_pnl)}</div>
      <div class="meta">{agg_today_n} trades across {len(KNOWN_VARIANTS)} variants</div>
    </div>
    <div class="card">
      <div class="label">Fleet 7-Day</div>
      <div class="value mono" style="color:{agg_week_color}">{_money(agg_week_pnl)}</div>
      <div class="meta">{agg_week_n:,} trades</div>
    </div>
    <div class="card">
      <div class="label">In Position</div>
      <div class="value mono" style="color:{'#38bdf8' if n_in_position else 'var(--dim)'}">{n_in_position} / {len(KNOWN_VARIANTS)}</div>
      <div class="meta">variants currently holding</div>
    </div>
    <div class="card">
      <div class="label">Mode</div>
      <div class="value mono">{mode}</div>
      <div class="meta">all variants shadow-trading</div>
    </div>
  </div>

  <div class="panels">
    {panels_html}
  </div>

  <div class="events">
    <div class="events-header">
      <span>Recent trades</span>
      <span class="right">{filter_form}</span>
    </div>
    {table_html}
    {pagination}
  </div>

  <div class="footer">
    Auto-refresh 5s · service-role read · token-gated
    <br>
    Aggregate numbers compare variants of the same strategy — interpret accordingly.
  </div>
</div></body></html>"""


def render(sb, *, mode: str = "paper", token: str | None = None,
           strategy_id: str = "v3-canon",
           trades_page: int = 1, page_size: int = 50) -> str:
    now_ct = datetime.now(CT)
    now_utc = datetime.now(UTC)
    # Show last 7 days of paper trades
    since = (now_utc - timedelta(days=7)).isoformat()

    # Aggregate stats over the full 7d window (settled metrics)
    rows = _fetch_trades(sb, mode=mode, strategy_id=strategy_id,
                         since_iso=since, limit=2000)
    settled = _settled_metrics(rows)
    open_pos = _fetch_open(sb, mode=mode, strategy_id=strategy_id)
    # Paginated trade rows for the recent-trades table
    page_rows, page_total = _fetch_trades_paginated(
        sb, mode=mode, strategy_id=strategy_id, since_iso=since,
        page=trades_page, page_size=page_size,
    )
    # Multi-variant nav data — yesterday-onward window so each strategy's
    # daily total is comparable.
    nav_summaries = _fetch_strategy_summaries(
        sb, mode=mode,
        since_iso=(now_utc - timedelta(days=1)).isoformat(),
    )
    # Per-strategy promotion-gate verdicts. Window matches the watcher's
    # 7-day display so the verdict reflects what the user is looking at.
    gate_rows = _fetch_all_paper_for_gate(sb, since_iso=since)
    verdicts = _verdicts_per_strategy(gate_rows)
    current_verdict = verdicts.get(strategy_id, {
        "verdict": "EXTEND_PAPER",
        "reason": f"0/{GATE_MIN_SETTLED}",
        "settled": 0,
    })

    # Heartbeat for THIS variant — shown alongside the gate/status pills so a
    # dead runner is visible on the per-variant page too.
    heartbeats = _fetch_heartbeats(sb)
    hb_status = _heartbeat_status(heartbeats.get(strategy_id), now_utc)

    # Today's metrics (in CT)
    today_ct_date = now_ct.date()
    today_rows = []
    for r in rows:
        bar_ts_str = r.get("bar_ts")
        if not bar_ts_str:
            continue
        try:
            bts = datetime.fromisoformat(bar_ts_str.replace("Z", "+00:00")).astimezone(CT)
            if bts.date() == today_ct_date:
                today_rows.append(r)
        except Exception:
            continue
    today = _settled_metrics(today_rows)

    # Build status pill
    if open_pos is not None:
        direction = (open_pos.get("direction") or "?").upper()
        status_pill = (
            f'<span class="pill pill-active"><span class="dot"></span>'
            f'IN POSITION · {direction}</span>'
        )
    elif today["n"] > 0:
        status_pill = (
            '<span class="pill pill-ok"><span class="dot"></span>'
            'TRADING · FLAT</span>'
        )
    else:
        status_pill = (
            '<span class="pill pill-blocked"><span class="dot"></span>'
            'IDLE</span>'
        )

    # KPI cards
    pf = settled["pf"]
    pf_str = f"{pf:.2f}" if pf is not None else "—"
    pf_color = (
        "#16a34a" if (pf or 0) >= 1.5 else
        "#eab308" if (pf or 0) >= 1.0 else
        "#dc2626" if pf is not None else "#94a3b8"
    )
    cum_pnl = settled["total"]
    cum_color = "#16a34a" if cum_pnl > 0 else ("#dc2626" if cum_pnl < 0 else "#e2e8f0")
    today_pnl = today["total"]
    today_color = "#16a34a" if today_pnl > 0 else ("#dc2626" if today_pnl < 0 else "#e2e8f0")

    if open_pos is not None:
        op_dir = (open_pos.get("direction") or "?").upper()
        op_entry = open_pos.get("entry_price")
        op_stop = open_pos.get("stop_price")
        op_held = _ago(open_pos.get("entry_ts"))
        op_value = f"{op_dir} @ {_money(op_entry).replace('$','')}"
        op_meta = f"stop {_money(op_stop)} · held {op_held}"
        op_color = "#06b6d4"
    else:
        op_value = "FLAT"
        op_meta = "no open position"
        op_color = "#94a3b8"

    # OOS drift checks
    actual_opp_pct = settled["exit_dist"].get("opposite_signal", 0.0)
    expected_opp_pct = OOS_REF["exit_dist"]["opposite_signal"]
    drift_warn = ""
    if settled["n"] >= 50 and abs(actual_opp_pct - expected_opp_pct) > 0.13:
        drift_warn = (
            f'<span class="meta-pill drift">drift: opposite-signal '
            f'{actual_opp_pct*100:.0f}% vs OOS {expected_opp_pct*100:.0f}%</span>'
        )

    # Recent trades rows — paginated against the full 7d trade history.
    rows_html = "".join(_trade_row_html(r) for r in page_rows)
    pagination_html = _pagination_html(
        page=trades_page, total=page_total, page_size=page_size,
        base_params={
            "token": token,
            "strategy_id": strategy_id,
            "mode": mode if mode != "paper" else None,
        },
        page_param="trades_page",
    )

    exit_dist_inline = _exit_dist_pill_html(settled["exit_dist"])
    trading_date = _topstep_trading_date(now_ct)

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta http-equiv="refresh" content="5">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0b1220">
<title>Acme Futures · Ryan-Spec v3</title>
<style>{_SHARED_CSS}</style>
</head><body><div class="wrap">
  <div class="topbar">
    <div class="brand"><strong>ACME</strong> · FUTURES · RYAN-SPEC V3</div>
    <div class="clock mono">{now_ct.strftime('%a %Y-%m-%d  %H:%M:%S CT')}</div>
  </div>

  <div class="breadcrumb">
    <strong style="color:#e2e8f0">{strategy_id}</strong>
  </div>

  <div class="statusrow" style="border-bottom:1px solid #1e293b;padding-bottom:10px;margin-bottom:8px">
    {_variant_nav_html(nav_summaries, current=strategy_id, token=token, mode=mode, verdicts=verdicts)}
  </div>

  <div class="statusrow">
    {status_pill}
    <span class="pill" title="last heartbeat {hb_status['ago']}{(' · auth FAILED' if hb_status['auth_ok'] is False else '')}{(f' · {hb_status["errors"]} consecutive errors' if hb_status['errors'] else '')}"
          style="color:{hb_status['color']};border-color:{hb_status['color']}55;background:{hb_status['color']}11">
      <span class="dot" style="background:{hb_status['color']}"></span>
      {hb_status['state']} · {hb_status['ago']}
    </span>
    <span class="meta-pill" style="border-left:3px solid {VERDICT_COLORS.get(current_verdict['verdict'], '#94a3b8')}"
          title="{current_verdict.get('reason', '')}">
      gate <strong style="color:{VERDICT_COLORS.get(current_verdict['verdict'], '#94a3b8')}">{current_verdict['verdict']}</strong>
      <span class="dim">{current_verdict.get('reason', '')}</span>
    </span>
    <span class="meta-pill">mode <strong>{mode}</strong></span>
    <span class="meta-pill">topstep session <strong>{trading_date.strftime('%a %m/%d')}</strong></span>
    <span class="meta-pill">today trades <strong>{today['n']}</strong></span>
    <span class="meta-pill">expected ~<strong>{int(OOS_REF['trades_per_day'])}</strong></span>
    {drift_warn}
  </div>

  <div class="stats">
    <div class="card">
      <div class="label">Open Position</div>
      <div class="value mono" style="color:{op_color}">{op_value}</div>
      <div class="meta">{op_meta}</div>
    </div>
    <div class="card">
      <div class="label">Today P&amp;L</div>
      <div class="value mono" style="color:{today_color}">{_money(today_pnl)}</div>
      <div class="meta">{today['n']} trades · WR {today['wr']:.0f}%</div>
    </div>
    <div class="card">
      <div class="label">7-Day P&amp;L</div>
      <div class="value mono" style="color:{cum_color}">{_money(cum_pnl)}</div>
      <div class="meta">{settled['n']} trades · {settled['wins']}W / {settled['losses']}L</div>
    </div>
    <div class="card">
      <div class="label">Profit Factor</div>
      <div class="value mono" style="color:{pf_color}">{pf_str}</div>
      <div class="meta">OOS {OOS_REF['pf']:.2f} · floor 1.50</div>
    </div>
  </div>

  <div class="events">
    <div class="events-header">
      <span>Recent trades</span>
      <span class="right">{exit_dist_inline}</span>
    </div>
    <table>
      <thead><tr>
        <th>ct</th>
        <th class="col-ago">ago</th>
        <th>dir</th>
        <th>entry</th>
        <th>exit</th>
        <th>reason</th>
        <th>p&amp;l</th>
        <th class="col-mfe">mfe</th>
        <th class="col-mae">mae</th>
        <th class="col-bars">bars</th>
      </tr></thead>
      <tbody>{rows_html or '<tr><td colspan="10" class="dim" style="text-align:center;padding:20px">No trades in last 7 days.</td></tr>'}</tbody>
    </table>
    {pagination_html}
  </div>

  <div class="footer">
    Ryan-Spec v3 · OOS PF 2.30 · filter: cum_delta_in_dir &lt; -2000 · stop 1.5 ATR · no target<br>
    Auto-refresh 5s · service-role read · token-gated
  </div>
</div></body></html>"""
