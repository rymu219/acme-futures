"""Print the most recent row(s) from ryan_spec_v3_trades.

Reads SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY from the environment (or
a project-root .env). Use this to sanity-check what the watcher UI
should be showing as the latest trade.

    uv run python scripts/latest_trade.py
    uv run python scripts/latest_trade.py --strategy-id v3-canon --limit 5
    uv run python scripts/latest_trade.py --mode paper --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from supabase import create_client

CT = ZoneInfo("America/Chicago")
UTC = ZoneInfo("UTC")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--strategy-id", default=None,
                   help="Filter to one variant (e.g. v3-canon). Default: all.")
    p.add_argument("--mode", default=None, choices=("paper", "live", "shadow"),
                   help="Filter by mode. Default: all.")
    p.add_argument("--limit", type=int, default=1,
                   help="How many rows to print (most recent first). Default: 1.")
    p.add_argument("--json", action="store_true",
                   help="Print raw JSON instead of a human-readable summary.")
    args = p.parse_args()

    load_dotenv()
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("error: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set "
              "(via env or .env)", file=sys.stderr)
        return 2

    sb = create_client(url, key)
    q = sb.table("ryan_spec_v3_trades").select("*")
    if args.strategy_id:
        q = q.eq("strategy_id", args.strategy_id)
    if args.mode:
        q = q.eq("mode", args.mode)
    res = q.order("bar_ts", desc=True).limit(args.limit).execute()
    rows = res.data or []

    if not rows:
        print("(no trades found for the given filters)")
        return 1

    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0

    now_utc = datetime.now(UTC)
    for r in rows:
        bar_ts = r.get("bar_ts")
        ago = "?"
        ct_str = "?"
        if bar_ts:
            try:
                ts = datetime.fromisoformat(bar_ts.replace("Z", "+00:00"))
                ct_str = ts.astimezone(CT).strftime("%Y-%m-%d %H:%M:%S CT")
                age_s = int((now_utc - ts).total_seconds())
                if age_s < 60:
                    ago = f"{age_s}s ago"
                elif age_s < 3600:
                    ago = f"{age_s // 60}m ago"
                elif age_s < 86400:
                    ago = f"{age_s // 3600}h ago"
                else:
                    ago = f"{age_s // 86400}d ago"
            except Exception:
                pass
        print(
            f"{ct_str}  ({ago})\n"
            f"  strategy_id : {r.get('strategy_id')}\n"
            f"  mode        : {r.get('mode')}\n"
            f"  direction   : {r.get('direction')}\n"
            f"  entry_ts    : {r.get('entry_ts')}\n"
            f"  exit_ts     : {r.get('exit_ts') or '(open)'}\n"
            f"  entry/exit  : {r.get('entry_price')} -> {r.get('exit_price')}\n"
            f"  exit_reason : {r.get('exit_reason') or '(open)'}\n"
            f"  pnl_dollars : {r.get('pnl_dollars')}\n"
            f"  bars_held   : {r.get('bars_held')}\n"
        )

    # Also surface the heartbeat for the same strategy so it's clear whether
    # "most recent trade was 4h ago" means "runner is dead" or "no signals".
    try:
        hb_q = sb.table("runtime_heartbeats").select("*")
        if args.strategy_id:
            hb_q = hb_q.eq("service", args.strategy_id)
        hbs = (hb_q.execute().data or [])
        if hbs:
            print("heartbeats:")
            for hb in sorted(hbs, key=lambda h: h.get("service") or ""):
                ts = hb.get("ts")
                age = "?"
                if ts:
                    try:
                        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        s = int((now_utc - t).total_seconds())
                        age = (f"{s}s" if s < 60 else
                               f"{s//60}m" if s < 3600 else
                               f"{s//3600}h" if s < 86400 else
                               f"{s//86400}d")
                    except Exception:
                        pass
                print(f"  {hb.get('service'):<14} hb {age:>4} ago "
                      f"auth_ok={hb.get('auth_ok')} "
                      f"errs={hb.get('consecutive_errors')} "
                      f"state={hb.get('position_state')}")
    except Exception as e:
        print(f"(could not read runtime_heartbeats: {e})", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
