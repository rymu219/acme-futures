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


def _fetch_trades(sb, *, mode: str, since_iso: str, limit: int = 1000) -> list[dict]:
    try:
        res = (
            sb.table("ryan_spec_v3_trades")
            .select("*")
            .eq("mode", mode)
            .gte("bar_ts", since_iso)
            .order("bar_ts", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data or []
    except Exception:
        return []


def _fetch_open(sb, *, mode: str) -> dict | None:
    try:
        res = (
            sb.table("ryan_spec_v3_trades")
            .select("*")
            .eq("mode", mode)
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


def render(sb, *, mode: str = "paper", token: str | None = None) -> str:
    now_ct = datetime.now(CT)
    now_utc = datetime.now(UTC)
    # Show last 7 days of paper trades
    since = (now_utc - timedelta(days=7)).isoformat()

    rows = _fetch_trades(sb, mode=mode, since_iso=since, limit=2000)
    settled = _settled_metrics(rows)
    open_pos = _fetch_open(sb, mode=mode)

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

    # Recent trades rows
    def _row_html(r: dict) -> str:
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
        return (
            f"<tr>"
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
            f"</tr>"
        )

    rows_for_table = rows[:MAX_ROWS]
    rows_html = "".join(_row_html(r) for r in rows_for_table)

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
<style>
  :root {{
    --bg-0: #0a0f1c; --bg-1: #0f1729; --bg-2: #131c33;
    --border: #1f2a44; --border-strong: #2c3a5a;
    --text: #e8eef9; --dim: #7f8aa8; --dim-2: #5a6789;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg-0); color: var(--text);
         font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
         -webkit-font-smoothing: antialiased; }}
  .wrap {{ max-width: 1200px; margin: 0 auto; padding: 18px; padding-bottom: 32px; }}
  .topbar {{ display: flex; justify-content: space-between; align-items: center;
             margin-bottom: 14px; }}
  .brand {{ font-size: 13px; letter-spacing: 2px; color: var(--dim); text-transform: uppercase; }}
  .brand strong {{ color: var(--text); }}
  .clock {{ font-size: 12px; color: var(--dim-2); }}
  .breadcrumb {{ font-size: 12px; color: var(--dim-2); margin-bottom: 12px; }}
  .breadcrumb a {{ color: var(--dim); text-decoration: none; }}
  .breadcrumb a:hover {{ color: var(--text); }}

  .statusrow {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center;
                margin-bottom: 16px; }}
  .pill {{ display: inline-flex; align-items: center; gap: 6px;
           padding: 5px 11px; border-radius: 999px;
           font-size: 11px; font-weight: 700; letter-spacing: 1px;
           text-transform: uppercase; border: 1px solid var(--border-strong); }}
  .pill .dot {{ width: 7px; height: 7px; border-radius: 999px; }}
  .pill-ok {{ color: #34d399; background: rgba(16,185,129,0.08); border-color: #166534; }}
  .pill-ok .dot {{ background: #34d399; }}
  .pill-active {{ color: #38bdf8; background: rgba(56,189,248,0.08); border-color: #075985; }}
  .pill-active .dot {{ background: #38bdf8; animation: pulse 1.4s infinite; }}
  .pill-blocked {{ color: #fbbf24; background: rgba(251,191,36,0.08); border-color: #854d0e; }}
  .pill-blocked .dot {{ background: #fbbf24; }}
  @keyframes pulse {{ 0%,100% {{ opacity:1 }} 50% {{ opacity:0.4 }} }}
  .meta-pill {{ font-size: 11px; color: var(--dim); padding: 4px 10px;
                background: var(--bg-2); border: 1px solid var(--border);
                border-radius: 999px; }}
  .meta-pill.drift {{ color: #fbbf24; border-color: #854d0e; }}
  .meta-pill strong {{ color: var(--text); }}

  .stats {{ display: grid; grid-template-columns: repeat(4, minmax(0,1fr));
            gap: 10px; margin-bottom: 16px; }}
  .card {{ background: var(--bg-1); border: 1px solid var(--border);
           border-radius: 14px; padding: 14px 16px; }}
  .card .label {{ font-size: 10.5px; letter-spacing: 1.6px;
                  text-transform: uppercase; color: var(--dim); font-weight: 600; }}
  .card .value {{ font-size: 26px; font-weight: 600; margin-top: 5px;
                  font-family: ui-monospace, SF Mono, Menlo, monospace;
                  font-variant-numeric: tabular-nums; }}
  .card .meta {{ font-size: 11px; color: var(--dim-2); margin-top: 4px;
                 font-family: ui-monospace, SF Mono, Menlo, monospace; }}

  .events {{ background: var(--bg-1); border: 1px solid var(--border);
             border-radius: 14px; overflow: hidden; }}
  .events-header {{ padding: 10px 14px; border-bottom: 1px solid var(--border);
                    font-size: 11px; letter-spacing: 1.4px; text-transform: uppercase;
                    color: var(--dim); font-weight: 600;
                    display:flex; justify-content:space-between; align-items:center; gap: 16px; }}
  .events-header .right {{ font-size: 11px; letter-spacing: 0.4px;
                           text-transform: none; color: var(--dim-2); }}
  table {{ width:100%; border-collapse: collapse; }}
  th, td {{ padding: 10px 14px; text-align: left;
            border-bottom: 1px solid var(--border); font-size: 13px; }}
  th {{ background: rgba(255,255,255,0.02); color: var(--dim-2);
        text-transform: uppercase; font-size: 10.5px; letter-spacing: 1.2px;
        font-weight: 600; }}
  tbody tr:hover {{ background: rgba(255,255,255,0.025); }}
  tr:last-child td {{ border-bottom: none; }}
  .mono {{ font-family: ui-monospace, SF Mono, Menlo, monospace;
           font-variant-numeric: tabular-nums; }}
  .dim {{ color: var(--dim-2); }}
  .kind {{ font-weight: 600; font-size: 12px; letter-spacing: 0.3px; }}

  .footer {{ margin-top: 16px; color: var(--dim-2); font-size: 11px; text-align: center; }}

  @media (max-width: 880px) {{
    .stats {{ grid-template-columns: repeat(2, minmax(0,1fr)); }}
    .col-ago, .col-mfe, .col-mae, .col-bars {{ display: none; }}
  }}
  @media (max-width: 520px) {{
    .wrap {{ padding: 12px; padding-bottom: 28px; }}
    .stats {{ gap: 8px; }}
    .card {{ padding: 12px 14px; border-radius: 12px; }}
    .card .value {{ font-size: 22px; }}
    th, td {{ padding: 9px 10px; font-size: 12px; }}
  }}
</style>
</head><body><div class="wrap">
  <div class="topbar">
    <div class="brand"><strong>ACME</strong> · FUTURES · RYAN-SPEC V3</div>
    <div class="clock mono">{now_ct.strftime('%a %Y-%m-%d  %H:%M:%S CT')}</div>
  </div>

  <div class="breadcrumb">
    <a href="/{('?token=' + token) if token else ''}">&larr; Fleet</a>
  </div>

  <div class="statusrow">
    {status_pill}
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
  </div>

  <div class="footer">
    Ryan-Spec v3 · OOS PF 2.30 · filter: cum_delta_in_dir &lt; -2000 · stop 1.5 ATR · no target<br>
    Auto-refresh 5s · service-role read · token-gated
  </div>
</div></body></html>"""
