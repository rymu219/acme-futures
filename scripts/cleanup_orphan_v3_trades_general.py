"""General orphan cleanup — find every open trade row whose variant's
heartbeat reports `flat`, and apply the same cleanup payload used for
the 2026-05-07 anchor cluster.

This is the more general version of cleanup_orphan_v3_trades.py.
That script only handled the 10 rows at $7,374.50 ± $0.50. The audit
itself missed several other orphans that share the same pattern
(open trade row + heartbeat says flat).

Cleanup payload matches the precedent (exit_reason
`manual_cleanup_2026_05_06_signalr_drop` shape):
  - exit_price  = entry_price          (no fabricated P&L)
  - exit_ts     = now                  (cleanup timestamp)
  - exit_reason = manual_cleanup_2026_05_11_audit
  - pnl_dollars = -1.40                (round-turn commission, MES)
  - bars_held, mfe_atr, mae_atr = NULL
  - slippage_ticks = 0

Safety:
  - Default dry-run.
  - Refuses to write if the candidate set would include a heartbeat-confirmed
    LIVE position (heartbeat says long/short for the variant).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime

from dotenv import load_dotenv
from supabase import create_client

EXIT_REASON_TAG = "manual_cleanup_2026_05_11_audit"
ROUND_TURN_COMMISSION = 1.40


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--execute", action="store_true",
                   help="Actually write changes. Without this, dry-run only.")
    args = p.parse_args()

    load_dotenv()
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("error: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY required",
              file=sys.stderr)
        return 2

    sb = create_client(url, key)

    # 1. Current heartbeat states
    hb_res = sb.table("runtime_heartbeats").select("service,position_state,ts").execute()
    hb_state = {h["service"]: (h.get("position_state") or "flat").lower()
                for h in (hb_res.data or [])}

    # 2. All open trades (paper, exit_ts null)
    opens_res = (
        sb.table("ryan_spec_v3_trades")
        .select("*")
        .eq("mode", "paper")
        .is_("exit_ts", "null")
        .order("entry_ts", desc=False)
        .execute()
    )
    opens = opens_res.data or []

    # 3. An open trade is an orphan iff the variant's heartbeat is `flat`
    candidates = []
    live = []
    for o in opens:
        sid = o.get("strategy_id")
        state = hb_state.get(sid, "?")
        if state == "flat":
            candidates.append(o)
        else:
            live.append((o, state))

    print(f"Open trade rows: {len(opens)}")
    print(f"  → orphan candidates (heartbeat=flat): {len(candidates)}")
    print(f"  → live (heartbeat agrees): {len(live)}")

    if live:
        print("\nLive positions kept (do not touch):")
        for o, st in live:
            print(f"  id={o['id']:>5}  {o['strategy_id']:<22}  "
                  f"{o['entry_ts']}  @ {o['entry_price']}  ({o['direction']})  "
                  f"hb={st}")

    if not candidates:
        print("\nNo orphans to clean.")
        return 0

    print(f"\nOrphan candidates to clean ({len(candidates)}):")
    for o in candidates:
        print(f"  id={o['id']:>5}  {o['strategy_id']:<22}  "
              f"{o['entry_ts']}  @ {o['entry_price']}  ({o['direction']})")

    now_utc = datetime.now(UTC).isoformat()
    print("\nPayload:")
    print("  exit_price   = entry_price (per row)")
    print(f"  exit_ts      = {now_utc}")
    print(f"  exit_reason  = '{EXIT_REASON_TAG}'")
    print(f"  pnl_dollars  = -{ROUND_TURN_COMMISSION:.2f}")
    print("  bars_held    = NULL")
    print("  mfe_atr      = NULL")
    print("  mae_atr      = NULL")
    print("  slippage_ticks = 0")

    if not args.execute:
        print("\n(dry-run — pass --execute to actually write)")
        return 0

    # 4. Execute
    fields_template = {
        "exit_ts": now_utc,
        "exit_reason": EXIT_REASON_TAG,
        "pnl_dollars": -ROUND_TURN_COMMISSION,
        "bars_held": None,
        "mfe_atr": None,
        "mae_atr": None,
        "slippage_ticks": 0,
    }
    n_ok = n_err = 0
    for o in candidates:
        row_fields = dict(fields_template)
        row_fields["exit_price"] = o["entry_price"]
        try:
            sb.table("ryan_spec_v3_trades").update(row_fields).eq(
                "id", o["id"]
            ).execute()
            n_ok += 1
            print(f"  ✓ updated id={o['id']} ({o['strategy_id']})")
        except Exception as e:
            n_err += 1
            print(f"  ✗ FAILED id={o['id']}: {e}", file=sys.stderr)

    print(f"\nDone. {n_ok} updated, {n_err} failed.")
    return 0 if n_err == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
