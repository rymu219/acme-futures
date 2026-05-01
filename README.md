# Acme Futures

Multi-strategy futures-trading system targeting Topstep evaluation. MES only.

A "comparison engine" — strategies are contestants, the system is the coach. Every strategy generates signals; the conductor arbitrates and routes to the broker (or to a phantom-position simulator in dry-run); per-strategy performance drives a state-machine lifecycle (SHADOW → PILOT → LIVE → BENCH).

## Architecture

```
strategies (signal generators) ──► conductor (arbitration + execution)
                                        │
                                        ├─► broker (TopstepX/ProjectX REST + SignalR)
                                        │
                                        └─► supabase (event log)
                                                ▲
                                                │
                                  watcher (TUI + Railway web dashboard)
```

Strategies never touch the broker. Only the conductor does. This is the architectural answer to Topstep's no-hedging rule.

## Phase status

- ✅ **Phase A** — single-strategy bot end-to-end (EMA cross), real Combine sim round-trip, Supabase event log, watcher TUI + Railway web dashboard
- ✅ **Phase B**
  - ✅ B1 — Conductor + registry + flat-first protocol
  - ✅ B2 — Seed fleet of 5 strategies (EMA cross, Anti, ORB, Donchian, Bollinger MR)
  - ✅ B3 — Per-strategy perf tracking + confidence scoring + leaderboard
  - 🔜 B4 — Backtest harness (Databento data already cached)
  - 🔜 B5 — Eval-mode overrides + emergency flatten + Combine attempt

## Quick start

```bash
# Install dependencies
uv sync

# Copy env template, fill in your ProjectX + Supabase credentials
cp .env.example .env

# Run tests
uv run pytest

# Run the bot in dry-run (phantom positions only, no real orders)
caffeinate -dimsu uv run python -m acme.runner --dry-run

# In another terminal, watch live
uv run python -m acme.watch
```

Auto-start the runner on Mac login: see `launchd/README.md`.

## Constraints (load-bearing)

- **TopstepX prohibits VPS / cloud execution** of automated trading. The bot runs on the user's Mac. Web dashboard is fine on Railway because it only reads Supabase (no broker calls).
- **Single-instance API key** — only one runner can hold the broker session at a time. Browser sessions on `topstepx.com` will kick the runner.
- **Daily flatten by 14:55 CT** (15-min buffer before Topstep's 15:10 hard close).
- **Consistency rule**: best single day must stay below $1,500 on the 50K Combine.

## Layout

```
src/acme/
├── broker/         # ProjectX REST + SignalR adapter; paper broker for tests
├── conductor/      # Bar aggregation, arbitration, flat-first FSM, dry-run sim
├── perf/           # PerfTracker, scoring formula, snapshot flusher
├── strategies/     # Signal generators (one file per strategy)
├── smoke/          # Manual integration smoke scripts
├── calendar.py     # Topstep-aware trading calendar (holidays, news blackouts)
├── contracts.py    # Futures contract registry + front-month resolver
├── db.py           # Supabase event logger + DDL
├── registry.py     # Strategy lifecycle registry
├── risk.py         # Eval profiles + per-trade risk gates
├── runner.py       # Thin entry point — wires Conductor and runs forever
└── watch.py        # Terminal watcher (Rich-based TUI)

web/                # FastAPI dashboard deployed to Railway (read-only viewer)
tests/              # 161 unit + integration tests
launchd/            # macOS LaunchAgent plist for auto-start
```
