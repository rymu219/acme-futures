# archive/v3/

Archived code from the v3 multi-variant runtime, retired 2026-05-11
after the forensic audit ([`docs/v3_audit.md`](../../docs/v3_audit.md)).

## What lives here

- **`runner.py`** — the v3 multi-variant launcher. Defined the 16
  variants (v3-canon, v3-trail, v3-min2bar, v3-armor, v3-pctile, plus
  v3.1, v4, v5 families). Loaded by the now-unloaded LaunchAgent
  `com.acme-futures.v3-runner`.

A compatibility shim at [`src/acme/runner.py`](../../src/acme/runner.py)
re-exports `VARIANTS` + supporting symbols from this archive so the
audit script and the v3 backtest harness keep working. Importing it
emits a `DeprecationWarning`.

## What is NOT archived (still live)

- `src/acme/ryan_spec/v3_*.py` (engine, runtime, backfill, promotion,
  tick_delta) — kept in place because their test modules import them
  directly. They are not instantiated by any live process.
- `src/acme/ryan_spec/v4_regime.py` — **actively used** by the new fleet
  (REGIME and SESSION import its classifiers).
- `src/acme/ryan_spec/v4_engine.py` — referenced by `acme.backtest.v3_replay`;
  retained for backtest reproducibility.

## What replaced it

- Runner: [`src/acme/fleet_runner.py`](../../src/acme/fleet_runner.py)
- LaunchAgent: [`launchd/com.acme-futures.fleet-runner.plist`](../../launchd/com.acme-futures.fleet-runner.plist)
- Watchdog: [`scripts/fleet_runner_watchdog.sh`](../../scripts/fleet_runner_watchdog.sh)
- Strategies: `acme.strategies.{ignition,session,regime,boundary}`

## Trade-table policy

The `ryan_spec_v3_trades` Supabase table is now historical only. The
runner doesn't write to it. Cleanup / force-flatten scripts may write
rows with `exit_reason` matching `manual_*` (those are the only
expected writes). Any other write is flagged by a structured
`ryan_spec_v3_trades_unexpected_write` warning in
[`src/acme/db.py`](../../src/acme/db.py).
