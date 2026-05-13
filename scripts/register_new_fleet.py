"""Register the three keeper strategies into the Supabase `strategies` table.

After 2-year backtest filtering, the active fleet is:
  - BOUNDARY         (PF 3.80, 09-13 CT blacklist + PD drop + OR-hour blacklist)
  - OVERNIGHT_DRIFT  (PF 1.88, weak-bullish 2-3pt body band)
  - GAP_FILL         (PF 1.68, |gap| >= 12pt, 08:30 CT entry, 13:00 close)

The previously-registered IGNITION/SESSION/REGIME strategies stay in
the strategies table (we don't delete their history) but are no longer
attached by the runner. Their rows remain at whatever state they were
last set; the runner just doesn't bind instances to them anymore.

Each keeper is registered in SHADOW state (the default per its
metadata). The runner binds instances in `acme.fleet_runner`.

Idempotent — re-running upserts the same rows.

Usage:
  uv run python scripts/register_new_fleet.py            # dry-run (default)
  uv run python scripts/register_new_fleet.py --execute  # writes rows
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acme.strategies.boundary import BoundaryStrategy  # noqa: E402
from acme.strategies.gap_fill import GapFillStrategy  # noqa: E402
from acme.strategies.overnight_drift import OvernightDriftStrategy  # noqa: E402

STRATEGIES = [
    BoundaryStrategy,
    OvernightDriftStrategy,
    GapFillStrategy,
]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--execute", action="store_true",
                   help="Actually upsert rows. Without this, dry-run.")
    args = p.parse_args()

    print("Strategies to register:\n")
    print(f"  {'name':<12}  {'version':<8}  {'state':<8}  {'tier':<4}  "
          f"timeframe  default_lifecycle")
    print("-" * 80)
    for cls in STRATEGIES:
        meta = cls.metadata
        instance = cls()
        print(f"  {instance.name:<12}  {instance.version:<8}  "
              f"{meta.default_lifecycle:<8}  {meta.tier:<4}  "
              f"{meta.timeframe_minutes:>3}min      {meta.default_lifecycle}")

    if not args.execute:
        print("\n(dry-run — pass --execute to upsert to Supabase)")
        return 0

    from acme.db import Db
    from acme.registry import StrategyRegistry

    db = Db()
    reg = StrategyRegistry(db=db)
    for cls in STRATEGIES:
        instance = cls()
        rec = reg.upsert(
            name=instance.name,
            version=instance.version,
            state=instance.metadata.default_lifecycle,
            tier=instance.metadata.tier,
            params={},
            notes="registered by scripts/register_new_fleet.py — three-keeper fleet (2-year backtested)",
        )
        print(f"  ✓ upserted {rec.name} v{rec.version} state={rec.state}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
