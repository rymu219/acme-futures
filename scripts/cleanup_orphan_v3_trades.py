"""One-shot cleanup of orphaned ryan_spec_v3_trades rows from 2026-05-07 22:08 CT.

The v3 audit (docs/v3_audit.md §0.4) identified 10 rows with
`exit_ts IS NULL` whose variants' heartbeats are all flat — the runtime
restart / SignalR drop never wrote exit info. They sit at entry price
$7,374.50 (one outlier at $7,374.75) from 2026-05-07 22:08:01-04 CT.

Cleanup policy matches the precedent for exit_reason
`manual_cleanup_2026_05_06_signalr_drop`:
  - exit_price  = entry_price          (no fabricated P&L)
  - exit_ts     = now                  (cleanup timestamp)
  - exit_reason = manual_cleanup_2026_05_11_audit
  - pnl_dollars = -1.40                (round-turn commission, MES)
  - bars_held, mfe_atr, mae_atr = NULL (no real swings)
  - slippage_ticks = 0

Usage:
  uv run python scripts/cleanup_orphan_v3_trades.py             # dry-run (default)
  uv run python scripts/cleanup_orphan_v3_trades.py --execute   # writes
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import create_client

# Anchor parameters from the audit
ANCHOR_PRICE = 7374.50
ANCHOR_TOLERANCE = 0.50
ANCHOR_DATE = "2026-05-07"   # only touch rows from this day
EXIT_REASON_TAG = "manual_cleanup_2026_05_11_audit"
ROUND_TURN_COMMISSION = 1.40  # 2 * ROUND_TURN_COMMISSION_DOLLARS (0.70) from v3_runtime.py:68

EXPECTED_VARIANTS = {
    "v3-armor", "v3-min2bar", "v3-pctile", "v3-trail",
    "v4-loose-shorts", "v4-overnight-bias", "v4-trend-flip",
    "v4-trend-gate", "v4-vol-regime", "v5-mtf-anchor",
}


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

    # Identify candidate rows: open + within a generous window around the
    # anchor day + at anchor price.
    # ANCHOR_DATE is in CT (2026-05-07 22:08 CT = 2026-05-08 03:08 UTC).
    # Use a wide UTC window covering CT-day 2026-05-07 ± 1 day.
    res = (
        sb.table("ryan_spec_v3_trades")
        .select("*")
        .eq("mode", "paper")
        .is_("exit_ts", "null")
        .gte("entry_ts", "2026-05-07T00:00:00Z")
        .lt("entry_ts", "2026-05-09T00:00:00Z")
        .execute()
    )
    candidates = []
    for r in (res.data or []):
        ep = r.get("entry_price")
        if ep is None:
            continue
        try:
            ep = float(ep)
        except (TypeError, ValueError):
            continue
        if abs(ep - ANCHOR_PRICE) <= ANCHOR_TOLERANCE:
            candidates.append(r)

    print(f"Found {len(candidates)} candidate orphan rows.")
    if not candidates:
        print("Nothing to do.")
        return 0

    # Safety: every candidate's strategy_id should be in the expected set,
    # and the candidate set's strategy_ids should be a SUBSET of expected.
    seen = {c.get("strategy_id") for c in candidates}
    unexpected = seen - EXPECTED_VARIANTS
    if unexpected:
        print(f"REFUSING — unexpected variants in candidate set: {unexpected}",
              file=sys.stderr)
        print("If this is intended, edit EXPECTED_VARIANTS in this script.",
              file=sys.stderr)
        return 3
    missing = EXPECTED_VARIANTS - seen
    if missing:
        print(f"Note: expected variants not in candidate set (already cleaned?): {missing}")

    # Show the cleanup payload
    now_utc = datetime.now(timezone.utc).isoformat()
    print(f"\nCleanup payload (all rows get the same shape):")
    print(f"  exit_price   = entry_price (per row)")
    print(f"  exit_ts      = {now_utc}")
    print(f"  exit_reason  = '{EXIT_REASON_TAG}'")
    print(f"  pnl_dollars  = -{ROUND_TURN_COMMISSION:.2f}  (MES round-turn commission)")
    print(f"  bars_held    = NULL")
    print(f"  mfe_atr      = NULL")
    print(f"  mae_atr      = NULL")
    print(f"  slippage_ticks = 0")

    print(f"\nCandidates ({len(candidates)}):")
    for c in sorted(candidates, key=lambda r: r.get("entry_ts") or ""):
        print(f"  id={c.get('id')}  {c.get('strategy_id'):<22}  "
              f"{c.get('entry_ts')}  @ {c.get('entry_price')} "
              f"({c.get('direction')})")

    if not args.execute:
        print("\n(dry-run — pass --execute to actually write)")
        return 0

    # Write
    fields = {
        "exit_ts": now_utc,
        "exit_reason": EXIT_REASON_TAG,
        "pnl_dollars": -ROUND_TURN_COMMISSION,
        "bars_held": None,
        "mfe_atr": None,
        "mae_atr": None,
        "slippage_ticks": 0,
    }
    n_ok = 0
    n_err = 0
    for c in candidates:
        row_fields = dict(fields)
        row_fields["exit_price"] = c["entry_price"]
        try:
            sb.table("ryan_spec_v3_trades").update(row_fields).eq(
                "id", c["id"]
            ).execute()
            n_ok += 1
            print(f"  ✓ updated id={c['id']} ({c['strategy_id']})")
        except Exception as e:
            n_err += 1
            print(f"  ✗ FAILED id={c['id']}: {e}", file=sys.stderr)

    print(f"\nDone. {n_ok} updated, {n_err} failed.")
    return 0 if n_err == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
