"""Register the four Part-2 strategies into the Supabase `strategies` table.

After Phase 1 unwind (v3 LaunchAgent stopped, trades reconciled), the
new fleet — IGNITION, SESSION, REGIME, BOUNDARY — needs registry rows
so a future runner can bind instances and the perf tracker can score
SHADOW performance.

Each strategy is registered in SHADOW state (the default per its
metadata). The runner itself is a separate concern (Phase 6 / new
LaunchAgent), not handled by this script.

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
from acme.strategies.ignition import IgnitionStrategy  # noqa: E402
from acme.strategies.regime import RegimeStrategy  # noqa: E402
from acme.strategies.session import SessionStrategy  # noqa: E402

STRATEGIES = [
    IgnitionStrategy,
    SessionStrategy,
    RegimeStrategy,
    BoundaryStrategy,
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
            notes="registered by scripts/register_new_fleet.py for Part 2 SHADOW launch",
        )
        print(f"  ✓ upserted {rec.name} v{rec.version} state={rec.state}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
