"""Heartbeat-staleness probe used by the watchdog.

Reads SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY from the project .env, queries
the runtime_heartbeats table, and exits 0 if any v3-* heartbeat is fresh
(< MAX_STALE_SEC), exit 1 if all are stale (or table is empty).

Watchdog calls this on a timer; if it returns 1, the runner is hung even
if its log doesn't show signalrcore zombie spam (e.g. Mac sleep, ProjectX
auth expiry, whatever).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Cheap .env loader — avoid pulling python-dotenv just for this.
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
if ENV_PATH.exists():
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from supabase import create_client  # noqa: E402

MAX_STALE_SEC = int(os.environ.get("ACME_HEARTBEAT_MAX_STALE_SEC", "300"))


def main() -> int:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not (url and key):
        print("missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY", file=sys.stderr)
        return 2  # config issue — don't kill runner, but signal something is off
    sb = create_client(url, key)
    try:
        res = (
            sb.table("runtime_heartbeats")
            .select("service,ts")
            .like("service", "v3-%")
            .execute()
        )
    except Exception as e:
        print(f"supabase query failed: {e}", file=sys.stderr)
        return 2  # don't kill on a transient supabase blip
    rows = res.data or []
    if not rows:
        print("no v3 heartbeats found")
        return 1
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=MAX_STALE_SEC)
    fresh: list[str] = []
    stale: list[tuple[str, float]] = []
    for r in rows:
        ts_str = r.get("ts") or ""
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            stale.append((r["service"], -1.0))
            continue
        age_s = (now - ts).total_seconds()
        if ts >= cutoff:
            fresh.append(r["service"])
        else:
            stale.append((r["service"], age_s))
    if fresh:
        print(f"OK fresh={len(fresh)} stale={len(stale)} max_stale_sec={MAX_STALE_SEC}")
        return 0
    print(
        f"STALE all_heartbeats_old={len(stale)} oldest_age_sec="
        f"{max((s[1] for s in stale), default=-1):.0f}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
