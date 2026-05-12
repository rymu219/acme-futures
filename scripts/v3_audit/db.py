"""Shared utilities for the v3 audit scripts: Supabase client, CT timezone,
canonical queries against ryan_spec_v3_trades and runtime_heartbeats.

Inherits the connection pattern from scripts/latest_trade.py so we read with
the same env vars the rest of the live tooling uses.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from supabase import Client, create_client

CT = ZoneInfo("America/Chicago")
UTC = ZoneInfo("UTC")

# Any per-variant statistic computed on fewer trades than this is tagged
# [low_sample] in every output table. Forensic-audit rule from the plan.
MIN_TRADES_FOR_INFERENCE = 30

# Output directories. The audit only writes to docs/v3_audit/.
REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs"
AUDIT_DIR = DOCS_DIR / "v3_audit"


def get_client() -> Client:
    """Build the Supabase client. Exits with code 2 if env is missing."""
    load_dotenv()
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print(
            "error: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set "
            "(via env or .env)",
            file=sys.stderr,
        )
        sys.exit(2)
    return create_client(url, key)


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def to_ct(ts: str | datetime | None) -> datetime | None:
    """Convert an ISO string or aware datetime to CT (America/Chicago)."""
    if ts is None:
        return None
    if isinstance(ts, str):
        d = parse_iso(ts)
    else:
        d = ts
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(CT)


def fmt_ct(ts: str | datetime | None) -> str:
    """Format an ISO/datetime as 'YYYY-MM-DD HH:MM:SS CT', or '?' if unparseable."""
    d = to_ct(ts)
    return d.strftime("%Y-%m-%d %H:%M:%S CT") if d else "?"


# ---------- canonical queries ----------


def fetch_all_v3_trades(
    sb: Client, *, mode: str = "paper", since_days: int | None = None,
    page_size: int = 1000,
) -> list[dict[str, Any]]:
    """Page through ryan_spec_v3_trades. Supabase tops out at 1000 rows per
    select unless you page. Returns rows ordered ascending by bar_ts.
    """
    rows: list[dict[str, Any]] = []
    floor: str | None = None
    if since_days is not None:
        floor = (datetime.now(UTC) - timedelta(days=since_days)).isoformat()
    offset = 0
    while True:
        q = sb.table("ryan_spec_v3_trades").select("*")
        if mode:
            q = q.eq("mode", mode)
        if floor:
            q = q.gte("bar_ts", floor)
        res = (
            q.order("bar_ts", desc=False)
            .range(offset, offset + page_size - 1)
            .execute()
        )
        batch = res.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def fetch_heartbeats(sb: Client) -> list[dict[str, Any]]:
    res = sb.table("runtime_heartbeats").select("*").execute()
    return res.data or []


def fetch_open_trades(sb: Client, *, mode: str = "paper") -> list[dict[str, Any]]:
    """All ryan_spec_v3_trades rows where exit_ts IS NULL."""
    res = (
        sb.table("ryan_spec_v3_trades")
        .select("*")
        .eq("mode", mode)
        .is_("exit_ts", "null")
        .order("entry_ts", desc=False)
        .execute()
    )
    return res.data or []


# ---------- output helpers ----------


def ensure_audit_dir() -> Path:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    return AUDIT_DIR


def write_csv(name: str, rows: list[dict[str, Any]], columns: list[str]) -> Path:
    import csv
    ensure_audit_dir()
    path = AUDIT_DIR / name
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path
