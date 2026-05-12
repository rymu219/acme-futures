# Part 2 — work plan

Companion to the v3 audit ([`docs/v3_audit.md`](v3_audit.md)).

## Scope split — additive vs destructive

I'll execute **additive** steps autonomously and **stop at destructive
steps for explicit per-step approval**. The split:

### ✅ Additive (safe to execute without further confirmation)

- New strategy modules under [`src/acme/strategies/`](../src/acme/strategies/)
- New indicator / feature modules
- New migrations that ADD tables (never DROP or ALTER columns)
- New tests
- New scripts under [`scripts/`](../scripts/)
- New entries in `acme.runner.VARIANTS` registered in `SHADOW` lifecycle
- New plugins under `warden/` (out-of-process service)

### ⛔ Destructive (need explicit per-step approval before I run)

- Stop the v3 LaunchAgent (currently holding 20 LIVE LONG positions)
- Archive / move v3 runtime code under `archive/v3/`
- Strip the `insert_ryan_spec_v3_trade` / `update_ryan_spec_v3_trade`
  write paths from [`src/acme/db.py`](../src/acme/db.py)
- Mark `ryan_spec_v3_trades` read-only at the code or DB level
- Drop any `v3-*` entry from [`launchd/`](../launchd/) or
  [`scripts/runner_watchdog.sh`](../scripts/runner_watchdog.sh)

## Execution order

### Phase 2 — IGNITION (PULSE → Python port)

Sub-phases. Each is a separate commit; tests must pass between.

**2a. Core PULSE math (this turn).**
- New module: [`src/acme/strategies/pulse_features.py`](../src/acme/strategies/pulse_features.py).
  Pure-function port of the Pine math: slope of EMA-fast minus EMA-slow,
  4-bar decayed weights, RVOL, tanh-normalised magnitude, edge probability
  via logistic. No HTF or session gates — those are stacked on top later.
- Tests in `tests/test_pulse_features.py` covering: monotone behavior of
  `edge_score` with slope, probabilities sum to 1, weight-decay produces
  expected smoothing, edge-zero at flat slope.
- **No conductor wiring yet.** This sub-phase produces a feature engine,
  not a strategy.

**2b. PULSE entry gates.**
- HTF alignment (request 5-min OHLCV; reuse `acme.indicators.EMA`).
- Market structure: rolling swing-high/swing-low detector for higher-high
  / higher-low classification.
- Key levels: PDH/PDL from prior session; session H/L from rolling window;
  round-number snap. (BOUNDARY also wants this — DRY into a shared module.)
- Volatility regime: ATR vs ATR-SMA ratio.
- Session windows: data-driven from audit §2 (03–05 CT, 08–09 CT, 17 CT)
  instead of PULSE's RTH-centric default 09:30-16:00 ET windows.

**2c. IGNITION strategy.**
- New module: [`src/acme/strategies/ignition.py`](../src/acme/strategies/ignition.py).
  Wraps `pulse_features` + gates. Entry on `confirmedLong` /
  `confirmedShort`. Exit: min 2-bar hold (audit §3 fix), opposite
  signal, ATR stop. No 1-bar `opposite_signal` exits — that's the
  load-bearing audit finding the new fleet must honor.
- Tests covering the gate logic and the 2-bar minimum.
- **Registered in SHADOW state.** No PILOT/LIVE eligibility until
  PerfTracker confirms.

### Phase 3 — SESSION

- Entry windows from audit §2: 03:00–05:00 CT, 08:00–09:00 CT, 17:00 CT
  (small-sample 17 CT initially behind a feature flag).
- Entry signal: 2-min cum_delta extreme (same as v3 base) AND inside
  window AND not at a key level (reuse the level module from Phase 2b).
- Exit: window-close OR min-2-bar OR opposite signal OR ATR stop.
- Reuses `acme.calendar` for holidays.
- Tests + SHADOW registration.

### Phase 4 — REGIME

- Three-state classifier: compression, expansion, unclear.
- ATR + Bollinger Band width with a deadband. Reuse
  [`acme.indicators.Bollinger`](../src/acme/indicators.py).
- Behavior by state:
  - **expansion** → follow direction (entry on trend continuation)
  - **compression** → **skip** (per audit §7: 11/16 max-DDs happen
    in chop, so a deadband is more defensible than a fade rule)
  - **unclear** → no signal
- The classifier code lives at [`acme/ryan_spec/v4_regime.py`](../src/acme/ryan_spec/v4_regime.py)
  today; move into [`acme/classifiers/`](../src/acme/) (NEW dir) so
  REGIME imports from there, not from a v3 archive. Keep v3 imports
  working via a shim for now (additive).

### Phase 5 — BOUNDARY

The biggest new build.

**5a. Bar-level infrastructure.**
- New migration: `migrations/2026_05_12_bar_levels.sql` creating
  `bar_levels(date_ct, contract, pdh, pdl, onh, onl, orh, orl, ...)`.
- New module: [`src/acme/levels.py`](../src/acme/levels.py) computing
  PDH/PDL/ONH/ONL/ORH/ORL from cached Databento bars and writing to
  `bar_levels`. Idempotent (upsert per date).

**5b. Historical retag.**
- Script: `scripts/retag_v3_trades.py` — joins `ryan_spec_v3_trades`
  with `bar_levels` and computes `dist_to_nearest_level_above` and
  `dist_to_nearest_level_below` per historical trade. Reports a
  bucket analysis. **If the bucket analysis does NOT support
  level-proximity → reversal, I stop and surface that before
  continuing.**

**5c. Exhaustion-bar detector.**
- `is_exhaustion_bar(bar, history)` — local extreme over N bars, doji
  body (body / range < 0.3), below-average volume, confirmed close.

**5d. BOUNDARY strategy.**
- Entry: price within N ticks of a level AND exhaustion bar AND
  reversal-direction matches level type.
- Exit: target = VWAP or level-midpoint; stop = level-break + buffer.
- SHADOW registration.

### Phase 6 — Warden

After Phases 2-5 are in SHADOW and accumulating data for ≥8 hours.

- New migration: `operator_events` table.
- New service skeleton under `warden/`.
- Daily brief @ 7:00 CT via Resend.
- Anomaly monitor (cadence drift, reconciliation mismatches, telemetry
  gaps, cluster firings).
- Lives on Railway, DB role with read-only on trading tables,
  write-only on `operator_events`. **Permissions enforced at the DB
  level, not in code.**

## Dependencies you'll be asked to confirm

| Item | Why | When |
|---|---|---|
| Stop v3 LaunchAgent | Phase 1 cleanup; 20 LIVE positions need to exit first | When you say |
| PULSE Pine source (the truncated portion) | The tail of your paste cut off at `not (useMTF and not mt…` — I'll work with what I have but the missing piece may affect the entry-state classifier | When you re-paste or confirm what was lost |
| Stealth Umbrella source | You linked it but didn't paste the script. Behavior-tracking is a separate concern from signal generation | When you paste it |
| Bar-data retention | BOUNDARY needs Databento bars at 2-min cached locally. Confirm the existing cache covers 90+ days | Before Phase 5 |

## Status

| Phase | Status |
|---|---|
| Phase 1 — v3 unwind | **done** (21 rows force-closed, +$2,618 locked, agent stopped) |
| Phase 1 leftover — archive v3 code, strip write paths | gated (user approval) |
| Phase 2a — PULSE core math | **done** |
| Phase 2b — PULSE gate stack (HTF, vol regime, zones, pullback, exhaustion, lockout) | **done** |
| Phase 2c — IGNITION strategy | **done** (SHADOW) |
| Phase 3 — SESSION | **done** (SHADOW) |
| Phase 4 — REGIME | **done** (SHADOW) |
| Phase 5 — BOUNDARY (incl. levels infra + exhaustion detector) | **done** (SHADOW) |
| Phase 5b — historical retag script | **written**, but blocked: Databento cache ends 2026-04-30 vs trades from 2026-05-04. Refresh cache to enable retroactive validation. Not blocking — BOUNDARY validates via live SHADOW data instead. |
| Strategy registration (Supabase `strategies` table) | **done** (`scripts/register_new_fleet.py --execute`) |
| New LaunchAgent + runner for the new fleet | **pending** — biggest remaining piece |
| Phase 6 — Warden | pending |

### Phase 2c decisions (already executed)

- **Entry filter**: GO/NO-GO Box's 4 binary gates ([`go_no_go.py`](../src/acme/strategies/go_no_go.py)).
  PULSE features (`pulse_features.py`) and gates (`pulse_gates.py`)
  remain as a feature library available to future strategies and to
  Warden for diagnostics. Rationale: audit §3 said simpler entry +
  disciplined exit wins; layering PULSE on top would repeat the v3.1
  pattern that didn't help.
- **Min-2-bar hold lives in the strategy**, not the conductor. IGNITION
  self-suppresses opposite signals before bar 2.
  ([`ignition.py`](../src/acme/strategies/ignition.py)).
- **Time windows**: 03:00–05:00 CT and 08:00–09:00 CT only — from audit §2.
  Pine's 13:00–14:15 CT window is dropped (worst hour in fleet).
- **Direction**: long-only by default (audit §4 — short edge unproven).
  Configurable.

## What's next (gated on user)

1. Phase 3 — SESSION (the strategy whose ONLY entry signal is the time
   window itself + cum-delta extreme, no extra gates). The simplest of
   the four.
2. Phase 4 — REGIME (compression deadband + expansion follow).
3. Phase 5 — BOUNDARY (new infra for level tables, historical retag, then
   the strategy).
4. New LaunchAgent for the new fleet (after some / all of the strategies
   are in SHADOW).
5. v3 code archive + write-path strip (still destructive, still gated).

