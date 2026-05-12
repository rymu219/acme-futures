"""Heartbeat-staleness monitor.

For each fleet strategy:
  - LIVE heartbeat (last tick < `live_threshold_s`) — no event
  - STALE (between live and offline thresholds) — emit `heartbeat_stale`
    at severity=warn unless one is already open
  - OFFLINE (last tick > `offline_threshold_s`) — emit at severity=error
    or upgrade an existing stale event
  - Recovery — if a previously-emitted stale/offline event hasn't been
    resolved AND the strategy is now LIVE, emit `heartbeat_recovered`
    + resolve the open event

Same staleness thresholds as the dashboard's hb pill (HB_LIVE_MAX_S /
HB_OFFLINE_MIN_S in web/fleet_view.py).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from warden.db import fetch_heartbeats, fetch_recent_operator_events
from warden.monitors import MonitorResult


FLEET = ["ignition", "session", "regime", "boundary"]

LIVE_MAX_S = 300       # < 5 min: LIVE
OFFLINE_MIN_S = 900    # > 15 min: OFFLINE


def _hb_age_s(hb: dict[str, Any]) -> int | None:
    ts = hb.get("ts")
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None
    return int((datetime.now(timezone.utc) - d).total_seconds())


def _hb_status(hb: dict[str, Any] | None) -> str:
    """Returns 'live' | 'stale' | 'offline' | 'unknown'."""
    if hb is None:
        return "unknown"
    age = _hb_age_s(hb)
    if age is None:
        return "unknown"
    if age < LIVE_MAX_S:
        return "live"
    if age < OFFLINE_MIN_S:
        return "stale"
    return "offline"


def run(sb) -> MonitorResult:
    hbs = {h.get("service"): h for h in fetch_heartbeats(sb, FLEET)}
    # Open events = unresolved heartbeat_stale or heartbeat_offline for
    # the fleet, last 24h
    open_events = {}
    for ev in fetch_recent_operator_events(sb, since_minutes=24 * 60):
        if ev.get("kind") not in ("heartbeat_stale", "heartbeat_offline"):
            continue
        if ev.get("resolved_at"):
            continue
        strat = ev.get("strategy")
        if strat in FLEET:
            open_events[strat] = ev

    out = MonitorResult()
    for name in FLEET:
        hb = hbs.get(name)
        status = _hb_status(hb)
        open_ev = open_events.get(name)

        if status == "live" and open_ev:
            # Recovered — resolve the open event and emit a recovery note
            out.events_to_resolve.append(int(open_ev["id"]))
            out.events_to_emit.append({
                "kind": "heartbeat_recovered",
                "severity": "info",
                "strategy": name,
                "summary": f"{name} heartbeat back to LIVE",
                "details": {
                    "previously": open_ev.get("kind"),
                    "resolved_event_id": open_ev["id"],
                    "age_s": _hb_age_s(hb) if hb else None,
                },
            })

        elif status == "stale":
            if open_ev and open_ev.get("kind") == "heartbeat_stale":
                continue   # already alerted
            out.events_to_emit.append({
                "kind": "heartbeat_stale",
                "severity": "warn",
                "strategy": name,
                "summary": f"{name} heartbeat stale "
                            f"({_hb_age_s(hb)}s since last tick)",
                "details": {
                    "age_s": _hb_age_s(hb),
                    "last_ts": (hb or {}).get("ts"),
                    "last_bar_ts": (hb or {}).get("last_bar_ts"),
                    "live_threshold_s": LIVE_MAX_S,
                },
            })

        elif status == "offline":
            if open_ev and open_ev.get("kind") == "heartbeat_offline":
                continue
            out.events_to_emit.append({
                "kind": "heartbeat_offline",
                "severity": "error",
                "strategy": name,
                "summary": f"{name} heartbeat OFFLINE "
                            f"({_hb_age_s(hb)}s since last tick)",
                "details": {
                    "age_s": _hb_age_s(hb),
                    "last_ts": (hb or {}).get("ts"),
                    "last_bar_ts": (hb or {}).get("last_bar_ts"),
                    "offline_threshold_s": OFFLINE_MIN_S,
                },
            })

        elif status == "unknown" and not open_ev:
            out.events_to_emit.append({
                "kind": "heartbeat_unknown",
                "severity": "warn",
                "strategy": name,
                "summary": f"{name} has no heartbeat row at all",
                "details": {"hint": "runner may not be running, or strategy not registered"},
            })

    return out
