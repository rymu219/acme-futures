"""Tiny terminal UI for tracking bot activity.

Polls Supabase (broker_events) every 2 seconds and renders a live-updating
header panel + table. No broker session, so it's safe to run alongside the bot.

Run:  uv run python -m acme.watch
Quit: Ctrl-C
"""

from __future__ import annotations

import os
import time as _time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from acme.calendar import CT, can_trade_now, schedule_for, topstep_trading_date
from acme.db import Db

POLL_SECONDS = 2.0
MAX_ROWS = 25

# Combine starting balance for P&L baseline; override with ACME_STARTING_BALANCE env.
STARTING_BALANCE = float(os.getenv("ACME_STARTING_BALANCE", "50000"))

KIND_STYLES = {
    "auth_success":         "green",
    "auth_error":           "bold red",
    "order_submitted":      "bold cyan",
    "order_accepted":       "cyan",
    "order_rejected":       "bold red",
    "fill":                 "bold green",
    "flatten_triggered":    "bold yellow",
    "round_trip_complete":  "bold magenta",
    "risk_block":           "bold yellow",
    "calendar_block":       "yellow",
    "dry_run_signal":       "bold blue",
    "dry_run_close":        "blue",
    "account_snapshot":     "dim cyan",
    # B1 conductor event kinds
    "signal_emitted":       "dim blue",
    "signal_arbitrated":    "bold cyan",
    "signal_suppressed":    "dim yellow",
    "state_transition":     "bold magenta",
    "flatten_emergency":    "bold red",
    "test_ping":            "dim white",
}


def _kind_style(kind: str) -> str:
    return KIND_STYLES.get(kind, "white")


def _fmt_local(ts_iso: str) -> str:
    try:
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        return dt.astimezone(CT).strftime("%H:%M:%S")
    except Exception:
        return ts_iso[:8]


def _ago(ts_iso: str) -> str:
    try:
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        delta = datetime.now(ZoneInfo("UTC")) - dt
        s = int(delta.total_seconds())
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s // 60}m"
        return f"{s // 3600}h"
    except Exception:
        return "?"


def _summary_for(row: dict) -> str:
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
        if "order_id" in raw:
            parts.append(f"oid={raw['order_id']}")
        if "net_position" in raw:
            parts.append(f"net={raw['net_position']}")
        if "balance" in raw:
            parts.append(f"bal=${raw['balance']:.2f}")
        if "bar_close" in raw:
            parts.append(f"@{raw['bar_close']:.2f}")
    return " ".join(parts)


def _fetch_dry_run_stats(db: Db) -> dict | None:
    res = (
        db.client.table("broker_events")
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
    }


def _money(n: float | None) -> str:
    if n is None:
        return "—"
    sign = "-" if n < 0 else ""
    return f"{sign}${abs(n):,.2f}"


def _money_color(n: float | None) -> str:
    if n is None or abs(n) < 0.005:
        return "white"
    return "green" if n > 0 else "red"


def _fetch_snapshots(db: Db, since_ct: datetime) -> tuple[dict | None, dict | None]:
    """Returns (first_snapshot_today, latest_snapshot). Either may be None."""
    cutoff_iso = since_ct.astimezone(ZoneInfo("UTC")).isoformat()
    res = (
        db.client.table("broker_events")
        .select("id, occurred_at, raw, account_id")
        .eq("kind", "account_snapshot")
        .gte("occurred_at", cutoff_iso)
        .order("id", desc=False)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return None, None
    return rows[0], rows[-1]


def _todays_baseline_balance(db: Db, session_start_ct: datetime) -> float | None:
    """Best-available baseline for today's P&L. Prefer last snapshot before session
    start; fall back to first snapshot of this session.
    """
    cutoff_iso = session_start_ct.astimezone(ZoneInfo("UTC")).isoformat()
    pre = (
        db.client.table("broker_events")
        .select("raw")
        .eq("kind", "account_snapshot")
        .lt("occurred_at", cutoff_iso)
        .order("id", desc=True)
        .limit(1)
        .execute()
    )
    if pre.data:
        return float((pre.data[0].get("raw") or {}).get("balance") or 0)
    first_today = (
        db.client.table("broker_events")
        .select("raw")
        .eq("kind", "account_snapshot")
        .gte("occurred_at", cutoff_iso)
        .order("id", desc=False)
        .limit(1)
        .execute()
    )
    if first_today.data:
        return float((first_today.data[0].get("raw") or {}).get("balance") or 0)
    return None


def render_header(db: Db, now_ct: datetime) -> Panel:
    trade_date = topstep_trading_date(now_ct)
    sched = schedule_for(trade_date)
    can_trade, gate_reason = can_trade_now(now_ct)

    if sched.fully_closed:
        sched_str = "[dim]MARKET CLOSED[/dim]"
    else:
        flatten = sched.flatten_at.strftime("%H:%M") if sched.flatten_at else "—"
        sched_str = f"flatten=[bold]{flatten}[/bold]"
    gate_color = "green" if can_trade else "red"
    gate_label = "TRADING" if can_trade else f"BLOCKED · {gate_reason}"

    # Topstep day starts at 17:00 CT prev day
    if now_ct.hour < 17:
        session_start_ct = now_ct.replace(hour=17, minute=0, second=0, microsecond=0) - timedelta(days=1)
    else:
        session_start_ct = now_ct.replace(hour=17, minute=0, second=0, microsecond=0)

    _, latest_snap = _fetch_snapshots(db, session_start_ct)
    if latest_snap is None:
        balance_str = "[dim]waiting for first account_snapshot…[/dim]"
        pnl_str = today_str = net_str = "[dim]—[/dim]"
        snap_age = ""
    else:
        latest_raw = latest_snap.get("raw") or {}
        balance = float(latest_raw.get("balance") or 0)
        net_pos = int(latest_raw.get("net_position") or 0)
        pnl = balance - STARTING_BALANCE
        balance_str = f"[bold white]{_money(balance)}[/bold white]"
        pnl_str = f"[bold {_money_color(pnl)}]{_money(pnl)}[/bold {_money_color(pnl)}]"
        net_color = "white" if net_pos == 0 else ("green" if net_pos > 0 else "red")
        net_str = f"[bold {net_color}]{net_pos:+d}[/bold {net_color}]"
        snap_age = f" [dim](as of {_ago(latest_snap['occurred_at'])} ago)[/dim]"
        today_baseline = _todays_baseline_balance(db, session_start_ct)
        if today_baseline is None:
            today_str = "[dim]—[/dim]"
        else:
            today_pnl = balance - today_baseline
            if today_pnl < -0.005:
                tcolor = "red"
            elif today_pnl >= 1300:
                tcolor = "yellow"
            elif today_pnl > 0.005:
                tcolor = "green"
            else:
                tcolor = "white"
            today_str = f"[bold {tcolor}]{_money(today_pnl)}[/bold {tcolor}]"

    stats = _fetch_dry_run_stats(db)
    if stats is None:
        dry_str = "[dim]—[/dim]"
        dry_meta = ""
    else:
        dry_pnl = stats["total_pnl"]
        dcolor = "white" if abs(dry_pnl) < 0.005 else ("blue" if dry_pnl > 0 else "red")
        dry_str = f"[bold {dcolor}]{_money(dry_pnl)}[/bold {dcolor}]"
        pf = f"PF {stats['profit_factor']:.2f}" if stats["profit_factor"] else "PF —"
        dry_meta = (
            f" [dim]({stats['trades']}t {stats['wins']}W/{stats['losses']}L"
            f" {stats['win_rate_pct']:.0f}% {pf})[/dim]"
        )

    body = Text.from_markup(
        f"[bold]BAL[/bold] {balance_str}     "
        f"[bold]CUM P&L[/bold] {pnl_str}     "
        f"[bold]TODAY P&L[/bold] {today_str}     "
        f"[bold]DRY-RUN P&L[/bold] {dry_str}{dry_meta}     "
        f"[bold]NET POS[/bold] {net_str}{snap_age}\n"
        f"[bold]{now_ct.strftime('%a %Y-%m-%d %H:%M:%S CT')}[/bold]   "
        f"trade_date=[bold]{trade_date}[/bold]   {sched_str}   "
        f"[{gate_color}]{gate_label}[/{gate_color}]"
    )
    return Panel(body, title="ACME FUTURES", border_style="cyan")


def _fetch_leaderboard(db: Db) -> list[dict]:
    """Latest perf snapshot per strategy, joined with the strategies row for state.
    Returns a list of dicts ordered by composite-style score (sharpe-ish) descending.
    """
    # Pull all strategies first
    try:
        strats_res = db.client.table("strategies").select("name,state,score,tier").execute()
    except Exception:
        return []
    strats = {row["name"]: row for row in (strats_res.data or [])}
    if not strats:
        return []

    # For each strategy, fetch its latest perf snapshot
    rows: list[dict] = []
    for name, srow in strats.items():
        snap_res = (
            db.client.table("strategy_perf_snapshot")
            .select("*")
            .eq("strategy", name)
            .order("id", desc=True)
            .limit(1)
            .execute()
        )
        snap = (snap_res.data or [None])[0]
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


_STATE_STYLE = {
    "LIVE":    "bold green",
    "PILOT":   "bold cyan",
    "SHADOW":  "blue",
    "BENCH":   "dim yellow",
    "RETIRED": "dim red",
    "REPLAY":  "dim",
    "BACKTEST": "dim",
}


def render_leaderboard(db: Db) -> Table:
    rows = _fetch_leaderboard(db)
    table = Table(
        title="Strategy Leaderboard",
        title_justify="left",
        show_lines=False,
        expand=True,
    )
    table.add_column("strategy", width=12)
    table.add_column("state", width=8)
    table.add_column("score", justify="right", width=7)
    table.add_column("n", justify="right", width=5, style="dim")
    table.add_column("net P&L", justify="right", width=10)
    table.add_column("win%", justify="right", width=6)
    table.add_column("PF", justify="right", width=6)
    table.add_column("Sharpe", justify="right", width=7)
    table.add_column("MaxDD", justify="right", width=8, style="dim")
    if not rows:
        table.add_row("[dim]waiting for first perf snapshot…[/dim]", "", "", "", "", "", "", "", "")
        return table
    for r in rows:
        state_style = _STATE_STYLE.get(r["state"], "white")
        pnl_color = _money_color(r["net_pnl"])
        pf_str = f"{r['pf']:.2f}" if r["pf"] is not None else "—"
        score_color = (
            "green" if r["score"] >= 0.55
            else ("yellow" if r["score"] >= 0.30 else "red")
        )
        table.add_row(
            r["name"],
            Text(r["state"], style=state_style),
            Text(f"{r['score']:.3f}", style=f"bold {score_color}"),
            str(r["n"]),
            Text(_money(r["net_pnl"]), style=pnl_color),
            f"{r['win_rate']*100:.0f}%",
            pf_str,
            f"{r['sharpe']:.2f}",
            _money(r["dd"]),
        )
    return table


# Event kinds excluded from the recent-events table — these are high-volume
# heartbeats (account_snapshot every 30s, perf_snapshot on every closed trade)
# whose data is already reflected in the header + leaderboard panels.
_HIDDEN_KINDS = frozenset({"account_snapshot", "test_ping"})


def render_table(db: Db) -> Table:
    # Pull more than MAX_ROWS so we still get MAX_ROWS after filtering noise out.
    res = (
        db.client.table("broker_events")
        .select("*")
        .order("id", desc=True)
        .limit(MAX_ROWS * 4)
        .execute()
    )
    raw_rows = res.data or []
    filtered = [r for r in raw_rows if (r.get("kind") or "") not in _HIDDEN_KINDS][:MAX_ROWS]
    rows = list(reversed(filtered))
    table = Table(
        title="Recent events",
        title_justify="left",
        show_lines=False,
        expand=True,
    )
    table.add_column("id", justify="right", style="dim", width=6)
    table.add_column("ct", width=10)
    table.add_column("ago", width=5, style="dim")
    table.add_column("kind", width=22)
    table.add_column("strategy", width=12, style="dim")
    table.add_column("summary", overflow="fold")
    for r in rows:
        kind = r.get("kind") or ""
        ts = r.get("occurred_at") or ""
        table.add_row(
            str(r.get("id") or ""),
            _fmt_local(ts),
            _ago(ts),
            Text(kind, style=_kind_style(kind)),
            r.get("strategy") or "",
            _summary_for(r),
        )
    return table


def render(db: Db) -> Group:
    now_ct = datetime.now(CT)
    return Group(
        render_header(db, now_ct),
        render_leaderboard(db),
        render_table(db),
    )


def main() -> None:
    console = Console()
    db = Db()
    try:
        db.client.table("broker_events").select("id").limit(1).execute()
    except Exception as e:
        console.print(f"[bold red]Supabase connection failed:[/bold red] {e}")
        return
    with Live(render(db), console=console, refresh_per_second=2, screen=False) as live:
        try:
            while True:
                _time.sleep(POLL_SECONDS)
                live.update(render(db))
        except KeyboardInterrupt:
            console.print("\n[dim]watcher stopped.[/dim]")


if __name__ == "__main__":
    main()
