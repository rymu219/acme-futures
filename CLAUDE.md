# Acme Futures — Claude conventions

Context that isn't derivable from the code. See [README.md](README.md) for project overview.

## Load-bearing constraints

- **MES only.** No other futures contracts.
- **No VPS / cloud execution.** TopstepX prohibits automated trading from VPS. The runner runs on Ryan's Mac. Railway-hosted watcher is fine — it only reads Supabase, no broker calls.
- **Single-instance API key.** Only one runner can hold the broker session at a time. Don't spawn a second runner — it will kick the live one and corrupt the session. Browser sessions on `topstepx.com` will also kick it.
- **Daily flatten by 14:55 CT.** 15-min buffer before Topstep's 15:10 hard close.
- **Consistency rule:** best single day must stay below $1,500 on the 50K Combine.

## Live supervision (don't fight it)

```
launchd com.acme-futures.v3-runner
  └─ scripts/runner_watchdog.sh             ← outer loop, two zombie checks
      └─ caffeinate -i uv run python -m acme.runner --dry-run
```

- **Don't manually start a runner** while the watchdog is up — single-instance API key.
- **Don't add a top-level `caffeinate`** to anything. The watchdog already wraps each spawn.
- **Don't rename heartbeat services without grepping** — `scripts/check_runner_heartbeat.py` and the watcher both filter by `service like 'v3-%'`. Silent breakage if you rename.

## Live state probes (read-only, safe anytime)

When asked "is the bot working?" / "did anything break?" reach for these first:

- `uv run python scripts/latest_trade.py [--limit N]` — recent `ryan_spec_v3_trades` rows + all `runtime_heartbeats`. Best one-shot answer.
- `uv run python scripts/check_runner_heartbeat.py` — exit 0 fresh, 1 stale, 2 transient. Same logic the watchdog uses.
- `tail logs/watchdog.log` — respawn history. 3+ respawns in 10 min = something's wrong.
- `tail logs/runner.out.log` — structured logs from the live runner.

## Architecture invariant

Strategies generate signals; the conductor (classic) or v3 runtime arbitrates and routes to the broker. **Strategies never touch the broker.** Don't introduce code paths that violate this — it's the architectural answer to Topstep's no-hedging rule.

The v3 multi-variant runtime is the main page (5 variants on one shared ProjectX SignalR connection via a market mux). Classic conductor still exists for the seed fleet.

## Workflow

- Tests: `uv run pytest` (must pass before merge — currently 364).
- Run a one-off script: `uv run python path/to/script.py`. Don't `pip install`.
- Lint: ruff defaults; CI catches it.
- DB: Supabase via service-role key in `.env`. The `runtime_heartbeats` and `ryan_spec_v3_trades` tables are the live event log.

## Tracked but easy to miss

- `launchd/com.acme-futures.v3-runner.plist` is the live LaunchAgent (the older `com.acme-futures.runner.plist` label was BTM-burned; don't try to revive it).
- `scripts/runner_watchdog.sh` — the entire outer supervision loop.
- `scripts/check_runner_heartbeat.py` — the staleness probe.

If any of these go untracked or get reverted, the supervision chain breaks silently.
