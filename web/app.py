"""Tiny FastAPI dashboard for remote monitoring of Acme Futures.

Runs on Railway. Reads broker_events from Supabase using the service-role key
(server-side only — never exposed to the browser). Renders a phone-friendly
HTML page that auto-refreshes every 5 seconds. Token-gated.

Local test:
    uv run uvicorn web.app:app --reload --port 8000
    open http://localhost:8000/?token=YOUR_TOKEN

Railway deploy:
    set env vars SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, ACME_VIEW_TOKEN
    Procfile is in this directory.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from supabase import create_client

# Load .env from project root for local dev. On Railway, env vars come from the
# platform — load_dotenv() is a no-op when no .env file is present.
load_dotenv()

CT = ZoneInfo("America/Chicago")
UTC = ZoneInfo("UTC")
MAX_ROWS = 30

# Event kinds excluded from the "Recent events" table — these are high-volume
# heartbeats (account_snapshot every 30s) whose data shows in the header/leaderboard.
HIDDEN_KINDS = frozenset({"account_snapshot", "test_ping"})

KIND_COLORS = {
    "auth_success":         "#16a34a",
    "auth_error":           "#dc2626",
    "order_submitted":      "#0891b2",
    "order_accepted":       "#06b6d4",
    "order_rejected":       "#dc2626",
    "fill":                 "#16a34a",
    "flatten_triggered":    "#ca8a04",
    "round_trip_complete":  "#a21caf",
    "risk_block":           "#ca8a04",
    "calendar_block":       "#eab308",
    "dry_run_signal":       "#2563eb",
    "dry_run_close":        "#3b82f6",
    "account_snapshot":     "#64748b",
    # B1 conductor event kinds
    "signal_emitted":       "#60a5fa",
    "signal_arbitrated":    "#0891b2",
    "signal_suppressed":    "#a78bfa",
    "state_transition":     "#c026d3",
    "flatten_emergency":    "#ef4444",
    "test_ping":            "#94a3b8",
}


app = FastAPI(title="Acme Futures · Watcher")

# Combine starting balance — used as the baseline for P&L. Override via env var
# if running a different account size (e.g. 25K or 100K Combine).
STARTING_BALANCE = float(os.getenv("ACME_STARTING_BALANCE", "50000"))


# View-only mirror of the runner's calendar gates. The runner has the full
# holiday-aware logic; the web UI just shows whether trading is currently
# allowed under normal-day rules. Holiday cases display as "weekend/closed".
def _topstep_trading_date(now_ct: datetime) -> date:
    return now_ct.date() if now_ct.hour < 17 else (now_ct.date() + timedelta(days=1))


def _flatten_for(d: date) -> str:
    if d.weekday() >= 5:
        return "—"
    return "14:55"


def _can_trade_now(now_ct: datetime) -> tuple[bool, str]:
    t = now_ct.time()
    if time(15, 10) <= t < time(17, 0):
        return False, "topstep_blackout_15:10-17:00"
    td = _topstep_trading_date(now_ct)
    if td.weekday() >= 5:
        return False, "market_closed"
    if now_ct.date() == td and t >= time(14, 55):
        return False, "past_today_flatten_14:55"
    return True, ""


def _client():
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    return create_client(url, key)


def _check_token(token: str | None) -> None:
    expected = os.environ.get("ACME_VIEW_TOKEN")
    if not expected:
        raise HTTPException(status_code=500, detail="ACME_VIEW_TOKEN not configured")
    if token != expected:
        raise HTTPException(status_code=401, detail="Bad or missing token")


def _money(n: float | None) -> str:
    if n is None:
        return "—"
    sign = "-" if n < 0 else ""
    return f"{sign}${abs(n):,.2f}"


def _ago(ts_iso: str) -> str:
    try:
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        s = int((datetime.now(UTC) - dt).total_seconds())
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s // 60}m"
        return f"{s // 3600}h"
    except Exception:
        return "?"


def _ct_str(ts_iso: str) -> str:
    try:
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        return dt.astimezone(CT).strftime("%H:%M:%S")
    except Exception:
        return ts_iso[:8]


def _summary(row: dict) -> str:
    parts: list[str] = []
    side = row.get("side")
    size = row.get("size")
    if side and size:
        parts.append(f"{side.upper()} {size}")
    if row.get("symbol"):
        parts.append(row["symbol"])
    raw = row.get("raw") or {}
    if isinstance(raw, dict):
        if "outcome" in raw:
            parts.append(f"{raw['outcome'].upper()}")
        if "net_pnl" in raw:
            parts.append(f"P&L={_money(float(raw['net_pnl']))}")
        if "reason" in raw and "outcome" not in raw:
            parts.append(f"reason={raw['reason']}")
        if "balance" in raw:
            parts.append(f"bal={_money(float(raw['balance']))}")
        if "net_position" in raw:
            parts.append(f"net={raw['net_position']}")
        if "bar_close" in raw:
            parts.append(f"@{raw['bar_close']:.2f}")
        if "order_id" in raw:
            parts.append(f"oid={raw['order_id']}")
    return " ".join(parts)


def _fetch_dry_run_stats(sb) -> dict | None:
    """Aggregate dry_run_close events into a stats dict, or None if no closes yet."""
    res = (
        sb.table("broker_events")
        .select("raw")
        .eq("kind", "dry_run_close")
        .execute()
    )
    rows = res.data or []
    if not rows:
        return None
    pnls = [float((r.get("raw") or {}).get("net_pnl") or 0.0) for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_wins = sum(wins)
    gross_losses = -sum(losses)
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else None
    return {
        "trades": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "total_pnl": sum(pnls),
        "win_rate_pct": 100.0 * len(wins) / len(pnls),
        "profit_factor": profit_factor,
        "best": max(pnls),
        "worst": min(pnls),
    }


def _session_start_ct(now_ct: datetime) -> datetime:
    """Topstep day starts at 17:00 CT prev day if before 17:00, else today."""
    base = now_ct.replace(hour=17, minute=0, second=0, microsecond=0)
    return base if now_ct.hour >= 17 else base - timedelta(days=1)


def _fetch_snapshots(sb, since_ct: datetime):
    cutoff = since_ct.astimezone(UTC).isoformat()
    res = (
        sb.table("broker_events")
        .select("occurred_at, raw")
        .eq("kind", "account_snapshot")
        .gte("occurred_at", cutoff)
        .order("id", desc=False)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return None, None
    return rows[0], rows[-1]


def _todays_baseline_balance(sb, session_start_ct: datetime) -> float | None:
    """Best-available baseline for today's P&L.

    Preferred: latest snapshot strictly BEFORE today's Topstep day start (17:00 CT).
    Fallback: first snapshot of THIS session (approximate — only true if the runner
    was running at 17:00 CT today).
    """
    cutoff = session_start_ct.astimezone(UTC).isoformat()
    pre = (
        sb.table("broker_events")
        .select("raw")
        .eq("kind", "account_snapshot")
        .lt("occurred_at", cutoff)
        .order("id", desc=True)
        .limit(1)
        .execute()
    )
    if pre.data:
        return float((pre.data[0].get("raw") or {}).get("balance") or 0)
    first_today = (
        sb.table("broker_events")
        .select("raw")
        .eq("kind", "account_snapshot")
        .gte("occurred_at", cutoff)
        .order("id", desc=False)
        .limit(1)
        .execute()
    )
    if first_today.data:
        return float((first_today.data[0].get("raw") or {}).get("balance") or 0)
    return None


def _fetch_leaderboard(sb) -> list[dict]:
    """Latest perf snapshot per strategy joined with the strategies row.
    Sorted by score descending.
    """
    try:
        strats_res = sb.table("strategies").select("name,state,score,tier").execute()
    except Exception:
        return []
    strats = strats_res.data or []
    rows: list[dict] = []
    for srow in strats:
        name = srow["name"]
        try:
            snap_res = (
                sb.table("strategy_perf_snapshot")
                .select("*")
                .eq("strategy", name)
                .order("id", desc=True)
                .limit(1)
                .execute()
            )
        except Exception:
            snap_res = None
        snap = ((snap_res.data if snap_res else None) or [None])[0]
        rows.append({
            "name": name,
            "state": srow.get("state") or "?",
            "tier": srow.get("tier") or 2,
            "score": float(srow.get("score") or 0),
            "n": int((snap or {}).get("n_trades") or 0),
            "net_pnl": float((snap or {}).get("net_pnl") or 0),
            "win_rate": float((snap or {}).get("win_rate") or 0),
            "pf": (snap or {}).get("profit_factor"),
            "sharpe": float((snap or {}).get("sharpe") or 0),
            "dd": float((snap or {}).get("max_drawdown") or 0),
        })
    rows.sort(key=lambda r: (-r["score"], r["name"]))
    return rows


_STATE_BG = {
    "LIVE":    "#16a34a",
    "PILOT":   "#0891b2",
    "SHADOW":  "#3b82f6",
    "BENCH":   "#a16207",
    "RETIRED": "#7f1d1d",
}


def _render_leaderboard_html(sb) -> str:
    rows = _fetch_leaderboard(sb)
    if not rows:
        return (
            '<div class="events" style="margin-bottom:14px;">'
            '<div class="events-header"><span>Strategy Leaderboard</span></div>'
            '<table><tbody><tr><td colspan="9" class="dim" style="padding:14px;">'
            'waiting for first perf snapshot…</td></tr></tbody></table></div>'
        )
    body_rows = []
    for r in rows:
        state = r["state"]
        state_bg = _STATE_BG.get(state, "#334155")
        pnl_color = (
            "#16a34a" if r["net_pnl"] > 0
            else ("#ef4444" if r["net_pnl"] < 0 else "#e2e8f0")
        )
        score_color = (
            "#16a34a" if r["score"] >= 0.55
            else ("#f59e0b" if r["score"] >= 0.30 else "#ef4444")
        )
        pf_str = f"{r['pf']:.2f}" if r["pf"] is not None else "—"
        body_rows.append(
            f"<tr>"
            f"<td><strong>{r['name']}</strong></td>"
            f"<td><span class='pill' style='background:{state_bg};color:white;border:none;font-size:10px;padding:3px 8px;'>{state}</span></td>"
            f"<td class='mono' style='text-align:right;color:{score_color};font-weight:700;'>{r['score']:.3f}</td>"
            f"<td class='mono dim' style='text-align:right;'>{r['n']}</td>"
            f"<td class='mono' style='text-align:right;color:{pnl_color};'>{_money(r['net_pnl'])}</td>"
            f"<td class='mono' style='text-align:right;'>{r['win_rate']*100:.0f}%</td>"
            f"<td class='mono' style='text-align:right;'>{pf_str}</td>"
            f"<td class='mono' style='text-align:right;'>{r['sharpe']:.2f}</td>"
            f"<td class='mono dim' style='text-align:right;'>{_money(r['dd'])}</td>"
            f"</tr>"
        )
    return f"""
  <div class="events" style="margin-bottom:14px;">
    <div class="events-header"><span>Strategy Leaderboard</span></div>
    <table>
      <thead><tr>
        <th>strategy</th><th>state</th>
        <th style='text-align:right;'>score</th>
        <th class='col-id' style='text-align:right;'>n</th>
        <th style='text-align:right;'>net P&amp;L</th>
        <th style='text-align:right;'>win%</th>
        <th style='text-align:right;'>PF</th>
        <th class='col-strategy' style='text-align:right;'>Sharpe</th>
        <th class='col-ago' style='text-align:right;'>MaxDD</th>
      </tr></thead>
      <tbody>{"".join(body_rows)}</tbody>
    </table>
  </div>
"""


def _render_html(sb) -> str:
    now_ct = datetime.now(CT)
    session_start = _session_start_ct(now_ct)
    _, latest = _fetch_snapshots(sb, session_start)
    if latest is None:
        bal_str = pnl_str = today_str = net_str = "—"
        snap_age = ""
        pnl_color = today_color = "#94a3b8"
    else:
        latest_raw = latest.get("raw") or {}
        bal = float(latest_raw.get("balance") or 0)
        net = int(latest_raw.get("net_position") or 0)
        # P&L since Combine start — tracks progress toward profit target.
        pnl = bal - STARTING_BALANCE
        bal_str = _money(bal)
        pnl_str = _money(pnl)
        net_str = f"{net:+d}"
        snap_age = f"as of {_ago(latest['occurred_at'])} ago"
        pnl_color = "#e2e8f0" if abs(pnl) < 0.005 else ("#16a34a" if pnl > 0 else "#dc2626")
        # Today's P&L — relative to balance at start of current Topstep day (17:00 CT).
        today_baseline = _todays_baseline_balance(sb, session_start)
        if today_baseline is None:
            today_str = "—"
            today_color = "#94a3b8"
        else:
            today_pnl = bal - today_baseline
            today_str = _money(today_pnl)
            # Color thresholds: red below 0, green positive, amber as we approach
            # the $1,500 consistency-rule cap on best single day for 50K Combine.
            if today_pnl < -0.005:
                today_color = "#dc2626"
            elif today_pnl >= 1300:    # within $200 of consistency cap
                today_color = "#f59e0b"
            elif today_pnl > 0.005:
                today_color = "#16a34a"
            else:
                today_color = "#e2e8f0"

    stats = _fetch_dry_run_stats(sb)
    if stats is None:
        dry_str = "—"
        dry_meta = "no closed trades yet"
        dry_color = "#94a3b8"
    else:
        dry_pnl = stats["total_pnl"]
        dry_str = _money(dry_pnl)
        pf_str = f"PF {stats['profit_factor']:.2f}" if stats["profit_factor"] else "PF —"
        dry_meta = (
            f"{stats['trades']}t · {stats['wins']}W/{stats['losses']}L"
            f" · {stats['win_rate_pct']:.0f}% · {pf_str}"
        )
        dry_color = "#e2e8f0" if abs(dry_pnl) < 0.005 else ("#3b82f6" if dry_pnl > 0 else "#ef4444")

    # Pull extra rows so we still get MAX_ROWS after filtering out the noisy
    # heartbeat events whose data is already reflected in the header / leaderboard.
    res = (
        sb.table("broker_events").select("*").order("id", desc=True)
        .limit(MAX_ROWS * 4).execute()
    )
    raw_rows = res.data or []
    rows = [r for r in raw_rows if (r.get("kind") or "") not in HIDDEN_KINDS][:MAX_ROWS]

    def row_html(r):
        kind = r.get("kind") or ""
        color = KIND_COLORS.get(kind, "#e2e8f0")
        return (
            f"<tr>"
            f"<td class='dim mono col-id'>{r.get('id','')}</td>"
            f"<td class='mono'>{_ct_str(r.get('occurred_at') or '')}</td>"
            f"<td class='dim mono col-ago'>{_ago(r.get('occurred_at') or '')}</td>"
            f"<td><span class='kind' style='color:{color}'>{kind}</span></td>"
            f"<td class='dim col-strategy'>{r.get('strategy') or ''}</td>"
            f"<td class='mono'>{_summary(r)}</td>"
            f"</tr>"
        )

    rows_html = "".join(row_html(r) for r in rows)

    # Build the status pill markup separately to keep the f-string readable
    can_trade, gate_reason = _can_trade_now(now_ct)
    trading_date = _topstep_trading_date(now_ct)
    flatten_str = _flatten_for(trading_date)
    if can_trade:
        status_pill = '<span class="pill pill-ok"><span class="dot"></span>TRADING</span>'
    else:
        status_pill = (
            f'<span class="pill pill-blocked"><span class="dot"></span>'
            f'{gate_reason.upper()}</span>'
        )

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta http-equiv="refresh" content="5">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0b1220">
<title>Acme Futures</title>
<style>
  :root {{
    --bg-0: #0a0f1c;
    --bg-1: #0f1729;
    --bg-2: #131c33;
    --border: #1f2a44;
    --border-strong: #2c3a5a;
    --text: #e8eef9;
    --dim: #7f8aa8;
    --dim-2: #5a6789;
    --accent: #06b6d4;
    --green: #10b981;
    --red: #f43f5e;
    --amber: #f59e0b;
    --blue: #3b82f6;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; background: var(--bg-0); color: var(--text);
                font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Inter",
                  system-ui, sans-serif;
                font-feature-settings: "ss01","cv11";
                -webkit-font-smoothing: antialiased; }}
  body::before {{
    content:""; position: fixed; inset:0; pointer-events:none;
    background: radial-gradient(900px 600px at 0% 0%, rgba(59,130,246,0.06), transparent 60%),
                radial-gradient(800px 500px at 100% 0%, rgba(6,182,212,0.05), transparent 60%);
    z-index: -1;
  }}
  .wrap {{ max-width: 1100px; margin: 0 auto; padding: 16px; padding-bottom: 32px; }}
  .topbar {{
    display:flex; justify-content:space-between; align-items:center;
    padding: 4px 4px 14px; gap: 10px;
  }}
  .brand {{ font-weight: 700; letter-spacing: 2px; font-size: 12px; color: var(--dim); }}
  .brand strong {{ color: var(--text); letter-spacing: 3px; }}
  .clock {{ font-family: ui-monospace, SF Mono, Menlo, monospace; font-size: 12px; color: var(--dim); }}

  .stats {{
    display: grid; gap: 10px;
    grid-template-columns: repeat(4, minmax(0,1fr));
    margin-bottom: 14px;
  }}
  .card {{
    position: relative;
    background: linear-gradient(180deg, var(--bg-2), var(--bg-1));
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 16px 18px;
    overflow: hidden;
  }}
  .card::after {{
    content:""; position:absolute; left:0; right:0; top:0; height:1px;
    background: linear-gradient(90deg, transparent, rgba(255,255,255,0.08), transparent);
  }}
  .card .label {{
    font-size: 10.5px; letter-spacing: 1.4px; color: var(--dim);
    text-transform: uppercase; font-weight: 600;
  }}
  .card .value {{
    font-size: clamp(24px, 5vw, 32px);
    font-weight: 700; line-height: 1.1; margin-top: 6px;
    font-variant-numeric: tabular-nums; letter-spacing: -0.5px;
  }}
  .card .value.mono {{ font-family: ui-monospace, SF Mono, Menlo, monospace; }}
  .card .meta {{ font-size: 11.5px; color: var(--dim-2); margin-top: 8px; }}

  .statusrow {{
    display:flex; flex-wrap:wrap; align-items:center; gap: 10px;
    padding: 4px 2px; margin-bottom: 14px; font-size: 12px; color: var(--dim);
  }}
  .pill {{
    display: inline-flex; align-items:center; gap: 8px;
    padding: 6px 12px; border-radius: 999px;
    font-size: 11px; font-weight: 700; letter-spacing: 1.2px;
    background: var(--bg-2); border: 1px solid var(--border);
  }}
  .pill .dot {{
    width: 8px; height: 8px; border-radius: 50%;
    background: currentColor; box-shadow: 0 0 0 0 currentColor;
  }}
  .pill-ok {{ color: var(--green); border-color: rgba(16,185,129,0.35); }}
  .pill-ok .dot {{ animation: pulse 1.6s ease-out infinite; }}
  .pill-blocked {{ color: var(--red); border-color: rgba(244,63,94,0.4); }}
  @keyframes pulse {{
    0%   {{ box-shadow: 0 0 0 0 rgba(16,185,129,0.5); }}
    70%  {{ box-shadow: 0 0 0 8px rgba(16,185,129,0); }}
    100% {{ box-shadow: 0 0 0 0 rgba(16,185,129,0); }}
  }}
  .meta-pill {{
    background: var(--bg-2); border: 1px solid var(--border);
    padding: 5px 10px; border-radius: 999px; font-size: 11px;
    color: var(--dim);
  }}
  .meta-pill strong {{ color: var(--text); margin-left: 4px; font-weight: 600; }}

  .events {{
    background: var(--bg-1); border: 1px solid var(--border);
    border-radius: 14px; overflow: hidden;
  }}
  .events-header {{
    padding: 12px 16px; border-bottom: 1px solid var(--border);
    font-size: 11px; letter-spacing: 1.4px; text-transform: uppercase;
    color: var(--dim); font-weight: 600;
    display:flex; justify-content:space-between; align-items:center;
  }}
  table {{ width:100%; border-collapse: collapse; }}
  th, td {{
    padding: 10px 14px; text-align: left;
    border-bottom: 1px solid var(--border);
    font-size: 13px;
  }}
  th {{
    background: rgba(255,255,255,0.02); color: var(--dim-2);
    text-transform: uppercase; font-size: 10.5px; letter-spacing: 1.2px;
    font-weight: 600;
  }}
  tbody tr {{ transition: background 0.15s; }}
  tbody tr:hover {{ background: rgba(255,255,255,0.025); }}
  tr:last-child td {{ border-bottom: none; }}
  .mono {{ font-family: ui-monospace, SF Mono, Menlo, monospace; font-variant-numeric: tabular-nums; }}
  .dim {{ color: var(--dim-2); }}
  .kind {{ font-weight: 600; font-size: 12px; letter-spacing: 0.3px; }}

  .footer {{ margin-top: 16px; color: var(--dim-2); font-size: 11px; text-align: center; }}

  /* tablet */
  @media (max-width: 880px) {{
    .stats {{ grid-template-columns: repeat(2, minmax(0,1fr)); }}
    .col-id, .col-ago {{ display: none; }}
  }}
  /* phone */
  @media (max-width: 520px) {{
    .wrap {{ padding: 12px; padding-bottom: 28px; }}
    .stats {{ gap: 8px; }}
    .card {{ padding: 12px 14px; border-radius: 12px; }}
    .card .value {{ font-size: 22px; }}
    .col-strategy {{ display: none; }}
    th, td {{ padding: 9px 10px; font-size: 12px; }}
  }}
</style>
</head><body><div class="wrap">
  <div class="topbar">
    <div class="brand"><strong>ACME</strong> · FUTURES</div>
    <div class="clock mono">{now_ct.strftime('%a %Y-%m-%d  %H:%M:%S CT')}</div>
  </div>

  <div class="statusrow">
    {status_pill}
    <span class="meta-pill">today <strong>{now_ct.strftime('%a %m/%d')}</strong></span>
    <span class="meta-pill">topstep session <strong>{trading_date.strftime('%a %m/%d')}</strong></span>
    <span class="meta-pill">flatten <strong>{flatten_str} CT</strong></span>
    <span class="meta-pill">snapshot <strong>{snap_age or '—'}</strong></span>
  </div>

  <div class="stats">
    <div class="card">
      <div class="label">Balance</div>
      <div class="value mono">{bal_str}</div>
    </div>
    <div class="card">
      <div class="label">Cumulative P&amp;L</div>
      <div class="value mono" style="color:{pnl_color}">{pnl_str}</div>
      <div class="meta">vs ${STARTING_BALANCE:,.0f} start</div>
    </div>
    <div class="card">
      <div class="label">Today P&amp;L</div>
      <div class="value mono" style="color:{today_color}">{today_str}</div>
      <div class="meta">consistency cap $1,500</div>
    </div>
    <div class="card">
      <div class="label">Dry-Run P&amp;L</div>
      <div class="value mono" style="color:{dry_color}">{dry_str}</div>
      <div class="meta">{dry_meta}</div>
    </div>
  </div>

  {_render_leaderboard_html(sb)}

  <div class="events">
    <div class="events-header">
      <span>Recent events</span>
      <span class="dim">net pos <strong style="color:var(--text)">{net_str}</strong></span>
    </div>
    <table>
      <thead><tr>
        <th class="col-id">id</th>
        <th>ct</th>
        <th class="col-ago">ago</th>
        <th>kind</th>
        <th class="col-strategy">strategy</th>
        <th>summary</th>
      </tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>

  <div class="footer">Auto-refresh 5s · service-role read · token-gated</div>
</div></body></html>"""


@app.get("/", response_class=HTMLResponse)
def home(token: str | None = Query(default=None)):
    _check_token(token)
    sb = _client()
    return _render_html(sb)


@app.get("/health")
def health():
    return {"ok": True}
