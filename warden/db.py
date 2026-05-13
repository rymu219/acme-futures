"""Warden-side Supabase client. Read trading tables; write operator_events only.

Mirrors the connection pattern used by scripts/v3_audit/db.py — same env
vars (`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`). When deployed to
Railway with a restricted role, the role's grants enforce the read-only
trading-tables / write-only operator_events split at the DB level so a
Warden bug can't accidentally write to a trading table.
"""
from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from typing import Any

from dotenv import load_dotenv
from supabase import Client, create_client


def get_client() -> Client:
    load_dotenv()
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("error: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY required",
              file=sys.stderr)
        sys.exit(2)
    return create_client(url, key)


# ───────────── reads ─────────────────────────────────────────────────


def fetch_heartbeats(sb: Client, services: list[str] | None = None
                      ) -> list[dict[str, Any]]:
    q = sb.table("runtime_heartbeats").select("*")
    if services is not None:
        q = q.in_("service", services)
    return (q.execute().data or [])


def fetch_recent_operator_events(
    sb: Client, *, kind: str | None = None, since_minutes: int = 60,
) -> list[dict[str, Any]]:
    from datetime import timedelta
    floor = (datetime.now(UTC)
              - timedelta(minutes=since_minutes)).isoformat()
    q = (sb.table("operator_events").select("*")
         .gte("occurred_at", floor)
         .order("id", desc=True))
    if kind is not None:
        q = q.eq("kind", kind)
    return (q.execute().data or [])


# ───────────── writes ────────────────────────────────────────────────


def insert_operator_event(
    sb: Client, *,
    kind: str, severity: str, summary: str,
    strategy: str | None = None,
    details: dict[str, Any] | None = None,
    source: str = "warden",
) -> int | None:
    """Best-effort insert. Returns the new id, or None on failure."""
    row = {
        "kind": kind,
        "severity": severity,
        "summary": summary,
        "strategy": strategy,
        "details": details or {},
        "source": source,
    }
    try:
        res = sb.table("operator_events").insert(row).execute()
        data = res.data or []
        return int(data[0]["id"]) if data else None
    except Exception as e:
        print(f"warden: insert_operator_event failed: {e}", file=sys.stderr)
        return None


def resolve_operator_event(sb: Client, event_id: int) -> None:
    """Mark an event resolved (sets resolved_at = now)."""
    try:
        sb.table("operator_events").update(
            {"resolved_at": datetime.now(UTC).isoformat()}
        ).eq("id", event_id).execute()
    except Exception as e:
        print(f"warden: resolve_operator_event failed: {e}", file=sys.stderr)
