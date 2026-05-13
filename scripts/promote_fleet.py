"""Promote BOUNDARY / OVERNIGHT_DRIFT / GAP_FILL to PILOT (or LIVE).

Strategies in SHADOW emit phantom signals only. The conductor executes
real broker orders only for PILOT / LIVE state. This script flips the
three keepers to the target state via the registry's `transition` API,
which validates the SHADOW → target transition and writes a
`state_transition` event for the audit trail.

Allowed transitions (per acme.registry):
  SHADOW  → PILOT | BENCH | RETIRED
  PILOT   → LIVE  | BENCH | SHADOW | RETIRED
  LIVE    → BENCH | PILOT | RETIRED

So `SHADOW → LIVE` is NOT allowed in one step. The script defaults to
`--state PILOT`; pass `--state LIVE` once they're already PILOT to do
the final hop. PILOT and LIVE behave identically for trade execution;
only the lifecycle label differs.

Usage:
  uv run python scripts/promote_fleet.py            # dry-run (default)
  uv run python scripts/promote_fleet.py --execute  # apply
  uv run python scripts/promote_fleet.py --execute --state LIVE
  uv run python scripts/promote_fleet.py --execute --state BENCH    # cool off
  uv run python scripts/promote_fleet.py --execute --strategy boundary  # one only
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

KEEPERS = ("boundary", "overnight_drift", "gap_fill")
TARGET_STATES = ("PILOT", "LIVE", "BENCH", "SHADOW", "RETIRED")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--execute", action="store_true",
                   help="Actually write the transition. Without this, dry-run.")
    p.add_argument("--state", default="PILOT", choices=TARGET_STATES,
                   help="Target lifecycle state (default PILOT).")
    p.add_argument("--strategy", default=None,
                   help="Operate on one strategy only "
                        "(default: all three keepers).")
    p.add_argument("--reason", default="manual promotion via promote_fleet.py",
                   help="Audit-log reason for the transition.")
    args = p.parse_args()

    targets = [args.strategy] if args.strategy else list(KEEPERS)
    from acme.db import Db
    from acme.registry import IllegalTransitionError, StrategyRegistry

    db = Db()
    reg = StrategyRegistry(db=db)
    reg.load_from_db()

    print(f"Target state: {args.state}\n")
    print(f"  {'name':<18}  {'current':<8}  → {'new':<8}  status")
    print("-" * 56)

    for name in targets:
        recs = [s for s in reg.list_all() if s.name == name]
        if not recs:
            print(f"  {name:<18}  {'(missing)':<8}  → {args.state:<8}  "
                  f"not in registry (run register_new_fleet.py first)")
            continue
        rec = recs[0]
        if rec.state == args.state:
            print(f"  {name:<18}  {rec.state:<8}  → {args.state:<8}  "
                  f"already {args.state}, skipping")
            continue
        if not args.execute:
            print(f"  {name:<18}  {rec.state:<8}  → {args.state:<8}  "
                  f"(dry-run, would transition)")
            continue
        try:
            reg.transition(name=name, to_state=args.state,
                           reason=args.reason, triggered_by="promote_fleet.py")
            print(f"  {name:<18}  {rec.state:<8}  → {args.state:<8}  ✓ done")
        except IllegalTransitionError as e:
            print(f"  {name:<18}  {rec.state:<8}  → {args.state:<8}  "
                  f"ILLEGAL: {e}")

    if not args.execute:
        print("\n(dry-run — pass --execute to apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
