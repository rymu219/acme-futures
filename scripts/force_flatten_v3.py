"""Force-close every open ryan_spec_v3_trades row.

Used in Part 2 Phase 1 unwind: closes all paper positions at the current
market price proxy, then leaves the runtime in a clean state for the
operator to stop the LaunchAgent.

Two categories of open rows:
  1. LIVE (heartbeat agrees position is open): close at current price
     proxy with computed real P&L.
  2. ORPHAN (heartbeat says flat): close at entry price with -$1.40
     commission (same pattern as cleanup_orphan_v3_trades*.py).

Current-price proxy = most recent `entry_price` from any variant in the
last hour. The runtime fires position clusters every few minutes and
each cluster has a known entry price; the freshest one is a reliable
near-now-price for MES.

MES contract spec:
  - point value: $5 per point
  - round-turn commission: $1.40 ($0.70/side per acme/ryan_spec/v3_runtime.py:68)
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from supabase import create_client

EXIT_REASON_LIVE = "manual_close_2026_05_11_unwind"
EXIT_REASON_ORPHAN = "manual_cleanup_2026_05_11_audit"
ROUND_TURN_COMMISSION = 1.40
MES_POINT_VALUE = 5.0
BAR_MINUTES = 2


def _bars_held(entry_ts_iso: str, exit_dt: datetime) -> int | None:
    try:
        e = datetime.fromisoformat(entry_ts_iso.replace("Z", "+00:00"))
    except Exception:
        return None
    minutes = (exit_dt - e).total_seconds() / 60.0
    return max(1, int(minutes / BAR_MINUTES))


def _current_price(sb) -> float:
    """Most recent entry_price across all variants in the last 60 min."""
    floor = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
    res = (
        sb.table("ryan_spec_v3_trades")
        .select("entry_price,bar_ts")
        .gte("bar_ts", floor)
        .order("bar_ts", desc=True)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    if not rows:
        raise SystemExit("error: no recent trade rows to derive a current price")
    return float(rows[0]["entry_price"])


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--execute", action="store_true",
                   help="Actually write changes. Without this, dry-run only.")
    p.add_argument("--exit-price", type=float, default=None,
                   help="Override the current-price proxy. Useful if you want "
                        "to lock in a specific price.")
    args = p.parse_args()

    load_dotenv()
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("error: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY required",
              file=sys.stderr)
        return 2
    sb = create_client(url, key)

    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()

    # 1. Current price proxy
    exit_price = args.exit_price if args.exit_price else _current_price(sb)
    print(f"Current-price proxy: ${exit_price:,.2f}")
    print(f"Cleanup timestamp:   {now_iso}")
    print()

    # 2. Categorize open rows
    hb_state = {
        h["service"]: (h.get("position_state") or "flat").lower()
        for h in (sb.table("runtime_heartbeats").select("service,position_state")
                  .execute().data or [])
    }
    opens = (
        sb.table("ryan_spec_v3_trades").select("*").eq("mode", "paper")
        .is_("exit_ts", "null").order("entry_ts", desc=False).execute()
    ).data or []

    live_rows: list[dict] = []
    orphan_rows: list[dict] = []
    for r in opens:
        if hb_state.get(r["strategy_id"], "flat") == "flat":
            orphan_rows.append(r)
        else:
            live_rows.append(r)

    print(f"Found {len(opens)} open trade rows ({len(live_rows)} LIVE, {len(orphan_rows)} ORPHAN)")
    print()

    # 3. Compute payloads
    payloads: list[tuple[dict, dict, str]] = []  # (row, update_fields, classification)

    for r in live_rows:
        entry_price = float(r["entry_price"])
        direction = r["direction"]
        sign = 1.0 if direction == "long" else -1.0
        pnl_points = (exit_price - entry_price) * sign
        pnl_dollars = pnl_points * MES_POINT_VALUE - ROUND_TURN_COMMISSION
        bars = _bars_held(r["entry_ts"], now_utc)
        payloads.append((r, {
            "exit_ts": now_iso,
            "exit_price": exit_price,
            "exit_reason": EXIT_REASON_LIVE,
            "pnl_dollars": round(pnl_dollars, 2),
            "bars_held": bars,
            "mfe_atr": None,
            "mae_atr": None,
            "slippage_ticks": 0,
        }, "LIVE"))

    for r in orphan_rows:
        payloads.append((r, {
            "exit_ts": now_iso,
            "exit_price": r["entry_price"],  # voided at entry
            "exit_reason": EXIT_REASON_ORPHAN,
            "pnl_dollars": -ROUND_TURN_COMMISSION,
            "bars_held": None,
            "mfe_atr": None,
            "mae_atr": None,
            "slippage_ticks": 0,
        }, "ORPHAN"))

    # 4. Print summary
    total_live_pnl = sum(p[1]["pnl_dollars"] for p in payloads if p[2] == "LIVE")
    print(f"{'id':>5}  {'classify':<8}  {'variant':<22}  {'entry':<8}  {'exit':<8}  "
          f"{'dir':<5}  {'bars':<5}  {'pnl_$':<10}")
    print("-" * 90)
    for row, fields, classify in payloads:
        print(
            f"{row['id']:>5}  {classify:<8}  {row['strategy_id']:<22}  "
            f"{row['entry_price']:<8}  {fields['exit_price']:<8}  "
            f"{row['direction']:<5}  "
            f"{str(fields['bars_held'] or '-'):<5}  "
            f"${fields['pnl_dollars']:<+9.2f}"
        )
    print()
    print(f"Total LIVE P&L at exit: ${total_live_pnl:+,.2f}")
    print(f"Total ORPHAN commission: ${-ROUND_TURN_COMMISSION * len(orphan_rows):,.2f}")
    print()

    if not args.execute:
        print("(dry-run — pass --execute to actually write)")
        return 0

    n_ok = n_err = 0
    for row, fields, _ in payloads:
        try:
            sb.table("ryan_spec_v3_trades").update(fields).eq("id", row["id"]).execute()
            n_ok += 1
            print(f"  ✓ id={row['id']} ({row['strategy_id']})")
        except Exception as e:
            n_err += 1
            print(f"  ✗ id={row['id']}: {e}", file=sys.stderr)
    print()
    print(f"Done. {n_ok} updated, {n_err} failed.")
    return 0 if n_err == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
