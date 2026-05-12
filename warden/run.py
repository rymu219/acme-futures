"""Warden CLI runner — one-pass invocation.

For the v1, Warden runs as a periodic process. Usage:

    uv run python -m warden.run                    # one pass, print events
    uv run python -m warden.run --emit             # one pass, write events to Supabase
    uv run python -m warden.run --emit --loop 60   # every 60s

The runner doesn't daemonize itself — schedule via Railway cron or a
systemd timer / launchd plist on Mac. The default pass is dry-run
(prints what it WOULD emit) so you can validate behavior before
flipping `--emit` on.
"""
from __future__ import annotations

import argparse
import json
import time
from typing import Any

from warden import monitors  # noqa: F401  (package marker)
from warden.db import (
    get_client,
    insert_operator_event,
    resolve_operator_event,
)
from warden.monitors.heartbeat import run as run_heartbeat_monitor


MONITORS = [
    ("heartbeat", run_heartbeat_monitor),
]


def _pass(sb, *, emit: bool) -> dict[str, Any]:
    """Run every monitor once. Returns a summary dict."""
    new_events: list[dict[str, Any]] = []
    to_resolve: list[int] = []
    for name, fn in MONITORS:
        try:
            r = fn(sb)
        except Exception as e:
            new_events.append({
                "kind": "monitor_failure",
                "severity": "error",
                "strategy": None,
                "summary": f"warden monitor {name} crashed: {e}",
                "details": {"monitor": name, "error": str(e)},
            })
            continue
        new_events.extend(r.events_to_emit)
        to_resolve.extend(r.events_to_resolve)

    if not emit:
        for ev in new_events:
            print(json.dumps(ev, default=str))
        for ev_id in to_resolve:
            print(json.dumps({"resolve": ev_id}))
    else:
        for ev in new_events:
            insert_operator_event(sb, **ev)
        for ev_id in to_resolve:
            resolve_operator_event(sb, ev_id)

    return {
        "emitted": len(new_events) if emit else 0,
        "to_emit": len(new_events),
        "resolved": len(to_resolve),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--emit", action="store_true",
                   help="Write events to Supabase. Without this, dry-run "
                        "(prints to stdout).")
    p.add_argument("--loop", type=int, default=0, metavar="SECONDS",
                   help="If >0, run every N seconds in a loop.")
    args = p.parse_args()

    sb = get_client()
    if args.loop > 0:
        while True:
            summary = _pass(sb, emit=args.emit)
            print(f"warden pass: {summary}")
            time.sleep(args.loop)
    else:
        summary = _pass(sb, emit=args.emit)
        print(f"warden pass: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
