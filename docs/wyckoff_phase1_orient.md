# Wyckoff Phase Classifier — Phase 1 orient

Read-only. No code changes, no strategy modifications, no runner contact (still unloaded).

---

## What you're proposing, structurally

A persistent, stateful **phase classifier** that runs alongside the existing strategies, emits its current phase + last confirmed Wyckoff event on every bar, and exposes that state to strategies (Phase 2's Spring Strategy is the first consumer). The classifier itself doesn't trade — it's infrastructure, like `RegimeEngine`.

This maps cleanly onto a pattern that already exists in the codebase: **the regime engine**. Reading that pattern is the cheapest way to understand where the Wyckoff classifier should live and how it should be wired.

---

## The existing template: RegimeEngine

Source: `src/acme/regime/classifier.py:RegimeEngine` + DB migrations.

- **Module:** `acme/regime/` is a peer package to `acme/strategies/`. Holds `classifier.py`, `indicators.py` (specialised — Hurst, BBWidthPercentile, etc.), `habitat.py` (gating rules), `analytics.py`, `backfill.py`.
- **Engine class:** `RegimeEngine.on_bar(bar) -> RegimeSnapshot` — stateful, fed bars in chronological order. Engine owns its indicators (ATR, ADX, BBWidthPercentile, Hurst, SMA(volume), momentum windows). Engine does NOT know about Supabase — persistence is the caller's job.
- **Snapshot dataclass:** `RegimeSnapshot` carries `ts, timeframe, regime, direction, confidence` plus the raw indicator values + reasoning trace. Has `.to_db_row()` for persistence.
- **Conductor wiring:** [conductor.py:355-365](src/acme/conductor/conductor.py:355)

  ```python
  if self._regime_engine is not None and tf == self._regime_tf:
      self._latest_regime = self._regime_engine.on_bar(bar, news_blackout=blackout)
      if self.db is not None:
          self.db.insert_regime_snapshot(self._latest_regime.to_db_row())
  ```

  Runs on every bar at the regime timeframe (default 5m). Stores `_latest_regime` in conductor state. Strategies consult it via habitat gating before `inst.on_bar()` is called.
- **DB table:** `market_regimes` — per-bar snapshot row with all indicator values + classification + JSONB reasoning. Defined inline in `src/acme/db.py:546-566`.
- **Tested in backtest harness:** NO. `scripts/backtest_new_fleet.py` does not instantiate a `RegimeEngine`. Backtests run strategies without regime gating today. Anything tied to regime state would need backtest-harness changes.

**This is the template.** Substitute "Wyckoff" for "Regime" and most of the architecture transfers.

---

## Where the Wyckoff classifier should live

### Module: `src/acme/wyckoff/`

A new peer package to `acme/regime/`. Recommended file layout:

```
acme/wyckoff/
  __init__.py
  classifier.py     # WyckoffClassifier (state machine) + WyckoffSnapshot
  events.py         # Per-event detection rules (SC, AR, ST, Spring, SOS, UT)
  state.py          # State enum + transition table
  backfill.py       # (Phase 4 — replay historical bars to rebuild state)
```

### Conductor wiring

Same pattern as regime engine. Conductor's `__init__` accepts an optional `wyckoff_classifier: WyckoffClassifier | None = None`. In `_process_bar`, on every 2-min bar (the user's stated timeframe):

```python
if self._wyckoff is not None and tf == self._wyckoff_tf:
    self._latest_wyckoff = self._wyckoff.on_bar(bar)
    if self.db is not None:
        try:
            self.db.insert_wyckoff_snapshot(self._latest_wyckoff.to_db_row())
            for evt in self._latest_wyckoff.new_events:
                self.db.insert_wyckoff_event(evt.to_db_row())
        except Exception as e:
            log.warning("wyckoff_persist_failed", error=str(e))
```

Wyckoff strategies (Phase 2 — the Spring Strategy is named in your spec) consult `self._latest_wyckoff` from the conductor in the same place habitat gating does today. No new gating layer needed; the strategy's own `on_bar` can read `self.config.wyckoff_state_ref` (or a similar handle the conductor injects at instantiation) and self-gate.

### Data tables (need new migrations, applied via Studio per existing convention)

**`wyckoff_state`** — current phase per-bar snapshot. One row per bar like `market_regimes`:

```sql
create table if not exists wyckoff_state (
  id              bigserial primary key,
  ts              timestamptz not null,
  timeframe       text not null,                  -- '2m'
  contract        text not null,                  -- 'MES'
  phase           text not null,                  -- accumulation|markup|distribution|markdown|unknown
  last_event      text,                           -- sc|ar|st|spring|sos|ut|none
  last_event_ts   timestamptz,
  armed_for       text,                           -- next-expected event in the sequence
  spread_atr      numeric,                        -- captured at this bar for debug
  rvol            numeric,
  raw_state       jsonb,
  created_at      timestamptz not null default now()
);
create index wyckoff_state_ts_idx on wyckoff_state (ts desc);
```

**`wyckoff_events`** — append-only event log. One row per confirmed event. This is what survives across runner restarts:

```sql
create table if not exists wyckoff_events (
  id              bigserial primary key,
  bar_ts          timestamptz not null,
  contract        text not null,                  -- 'MES'
  event_kind      text not null,                  -- sc|ar|st|spring|sos|ut
  phase_at_event  text not null,
  bar_o           numeric, bar_h numeric,
  bar_l           numeric, bar_c numeric, bar_v bigint,
  spread_pts      numeric, atr_pts numeric, spread_atr_ratio numeric,
  rvol            numeric, volume_vs_anchor numeric,    -- e.g. vol / SC_vol for ST
  ref_event_id    bigint references wyckoff_events(id), -- e.g. ST → references prior SC row
  notes           text,                                  -- reasoning trace
  created_at      timestamptz not null default now()
);
create index wyckoff_events_kind_ts_idx on wyckoff_events (event_kind, bar_ts desc);
create index wyckoff_events_bar_ts_idx  on wyckoff_events (bar_ts desc);
```

### State persistence across sessions

Your spec says "State persists across sessions in Supabase." There are two ways to read that:

1. **Reconstruct state on startup:** at conductor init, query `wyckoff_events` for the most recent N events and replay them through `WyckoffClassifier.replay()` to rebuild in-memory state. Cheap because `wyckoff_events` is small (events are rare). Same pattern `PerfTracker.backfill_from_db` uses for `dry_run_close`.
2. **Snapshot full state on each bar to `wyckoff_state.raw_state` JSONB:** load the most recent row on startup, restore in-memory state. More direct but introduces a "state schema" inside JSONB that has to evolve carefully.

**Recommendation:** option (1). Event log is the source of truth; state is derived. Mirrors the existing `PerfTracker` backfill approach.

---

## What data the classifier needs

Per your event specs, the classifier needs per-bar:

| Datum                          | Source                                   |
|--------------------------------|------------------------------------------|
| OHLCV                          | `Bar` passed into `on_bar` (already available) |
| Bar spread (h - l)             | Computed inline                          |
| ATR                            | `acme.indicators.ATR(14)` — already exists |
| 20-bar volume SMA → RVOL       | `acme.indicators.SMA` — already exists   |
| Multi-bar low/high lookback    | Internal `deque[float]` in the classifier |
| Anchor-event volume references | Internal state — store SC volume when SC fires, reference from ST |
| Close position within bar      | Computed inline: `(bar.c - bar.l) / (bar.h - bar.l)` |

**No new indicator code is needed.** Every primitive your event specs reference is already in `acme.indicators` or trivially derivable from the `Bar` dataclass.

---

## State machine — what the spec implies

Events from your spec form a directed sequence with arming relationships:

```
       (no state)
           │
           ▼
     SC detected ─────────────► ACCUMULATION phase begins
           │ arms AR
           ▼
     AR detected
           │ arms ST
           ▼
     ST detected ─────────────► (within window; volume < 0.6 × SC)
           │ arms Spring
           ▼
   Spring detected ───────────► (breaks ST low on light vol; recovers 1-3 bars)
           │ arms SOS
           ▼
     SOS detected ─────────────► MARKUP phase begins (entry for Spring Strategy)


       (no state)
           │
           ▼
     UT detected ──────────────► DISTRIBUTION phase begins
           │ (mirror sequence for shorting setups)
           ▼
        (Phase 2+)
```

Open design questions the spec doesn't pin down:

1. **Sequence-timeout windows.** How many bars can pass between SC and AR before the SC arming expires? Between AR and ST? Spring and SOS? Without a timeout, a 2-month-old SC could still arm an AR. Almost certainly we want bar-count caps (e.g., SC→AR ≤ 30 bars). Each arming relationship needs its own.
2. **Multi-bar low lookback for SC.** "Price near multi-bar low" — 10 bars? 50 bars? 200? Different choices yield wildly different SC rates.
3. **ATR period for the spread comparison.** "Spread ≥ 1.5 × ATR" — ATR(14)? ATR(20)? RegimeEngine uses ATR(14); BOUNDARY uses ATR(14); GO/NO-GO LEVELS uses ATR(4). Pick to match.
4. **What is "near"?** "Holds above SC low" — exactly above? Within N ticks above? Same for Spring: "recovers above ST low" — by how many ticks?
5. **Phase exit / decay.** If a Spring is detected but SOS never confirms within a window, what happens? Stay in ACCUMULATION until a fresh SC? Reset to UNKNOWN? Decay confidence?
6. **Concurrent sequences.** Can the classifier track Accumulation AND Distribution arms in parallel (e.g., we're between markdown and accumulation; SC pattern is starting to form while UT setup is still unresolved)? Or single-thread state machine where one sequence at a time?

Each of these is a 5-minute parameter decision. **None are blockers** — but Phase 1 implementation should freeze concrete values up front (defaults, not later edits) so the backtest is replicable. I'd default to: SC→AR ≤ 30 bars, AR→ST ≤ 30 bars, ST→Spring ≤ 30 bars, Spring→SOS ≤ 5 bars; multi-bar low lookback = 20 bars; ATR(14); "near" = within 2 ticks; phase stays until next decisive event.

---

## What blocks building it

### Things genuinely blocking

1. **No automated migration runner.** Same finding as the eval_log build. The two new tables (`wyckoff_state` + `wyckoff_events`) need to be applied via Supabase Studio after the migration files land. Mechanically identical to Phase 1 of the eval-log build.
2. **Backtest harness doesn't drive a per-bar classifier today.** `scripts/backtest_new_fleet.py` runs strategies but doesn't instantiate `RegimeEngine` — so the regime path is *untested in backtest*. Phase 2 strategies that depend on regime never see regime gating in the harness. The Wyckoff classifier would inherit this same blind spot unless the harness is modified.

   **Workaround:** add an optional `wyckoff_classifier` argument to `backtest_strategy()` that, when provided, runs `classifier.on_bar(bar)` before driving the strategy. Inject classifier state into the strategy via an attribute set on every bar. This is ~30 lines of harness modification. Not blocking but it has to happen before Phase 2 can validate the Spring Strategy.
3. **State-replay on restart needs the events table populated first.** Until the table exists and events have been recorded, restarts start from `UNKNOWN`. The Phase 2 Spring Strategy that only arms in `ACCUMULATION` will sit idle until at least one SC has been confirmed post-launch. That's days-to-weeks of warm-up in live. Acceptable but should be expected.

### Things not blocking but worth flagging

4. **Coupling with existing regime engine.** RegimeEngine and WyckoffClassifier would both run on the same conductor on different timeframes (regime at 5m, Wyckoff at 2m by the user's spec). They emit independent state; strategies could consult either. No conflict, just two concurrent state sources. Habitat gating is currently regime-only; Wyckoff would gate at the strategy level.
5. **Wyckoff at 2-min cadence.** Wyckoff was developed on daily/weekly charts where accumulation phases last weeks to months. Compressing to 2-min means events at minutes-to-hours timescales. Your event thresholds (volume ≥ 2× 20-bar avg, spread ≥ 1.5× ATR, etc.) are tuned for that compression — but the *behavioural premise* of Wyckoff (institutional accumulation behind retail panic) is harder to apply on intraday bars. Worth backtesting before believing the framework transfers.
6. **The Strategy Protocol doesn't fit the classifier.** Strategies return `Signal | EvalResult | None`. The classifier returns a `WyckoffSnapshot` — different shape. Don't shoehorn into `Strategy`; build as a sibling component (the regime-engine pattern).
7. **Lookahead-bias risk, post the level_rvol surprise.** Spring detection requires "recovers above ST low within 1-3 bars" — that's looking 1-3 bars forward to confirm. If we pre-compute Spring events using future bars and then test trades at the Spring bar, that's lookahead. **Mitigation:** classifier should ARM on the candidate bar but only CONFIRM (and write to `wyckoff_events`) at the bar where the recovery happens — i.e., 1-3 bars after the candidate. Spring Strategy entries fire on the confirmation bar, not the candidate bar. Same pattern the level_rvol arming used, but properly applied this time.

### Things explicitly NOT blocking

8. **No code missing.** All required indicators exist. `Bar` dataclass has every field needed. Conductor wiring pattern is established. DB write path is established. eval_log integration is established for the strategy that will consume the classifier (Phase 2).

---

## eval_log integration (Phase 2 concern, surfaced here)

The user's spec: "All event detections logged to eval_log with gate_values for dashboard display."

There's a subtlety here. `eval_log` is written by the **conductor's per-bar strategy loop**, dispatched from `EvalResult` returns or `Signal` fires. The Wyckoff *classifier* doesn't return `EvalResult` — it returns `WyckoffSnapshot`. So events themselves are NOT logged to eval_log in the natural code path.

Two ways to thread this needle:

1. **Wyckoff events → `wyckoff_events` table (proposed); `eval_log` stays as-is.** The dashboard reads both tables. Activity feed shows eval_log; the Wyckoff panel shows wyckoff_events. Cleanest, two-table approach.
2. **Wyckoff events also written to eval_log with `strategy='wyckoff_classifier'`, `outcome='ENTRY'` for confirmed events, `outcome='NEAR'` for armed-but-unconfirmed.** Unifies the activity feed but stretches eval_log's schema beyond strategy evaluations. Less clean.

**Recommendation:** option (1). Two tables, two purposes. The dashboard panel for Wyckoff (Phase 3) reads `wyckoff_events` directly.

---

## Honest assessment

**The build is feasible. The pattern exists.** Substituting "Wyckoff" for "Regime" gives you ~80% of the architecture for free, and `RegimeEngine` is already in production (writing to `market_regimes` per bar). The two new tables, the new package layout, and the conductor wiring are all small, well-understood pieces.

**The unknowns are about the *signal*, not the *plumbing*.** Specifically:
- Will Wyckoff events at 2-min cadence on MES produce a usable hit rate?
- Will the Spring Strategy (Phase 2) outperform BOUNDARY — which is itself a level-fade strategy with a similar "rejection at a key low" thesis but using exhaustion bars instead of Spring/SOS?
- Will the state-machine ambiguity (when to reset, how long arming windows live) get tuned away cleanly or pollute everything downstream?

These are the same questions the level_rvol Phase 1 surfaced — and we learned that one the hard way through lookahead bias. The Wyckoff version has its own structural risk (the 1-3 bar Spring-recovery window): the classifier MUST confirm at the recovery bar, not the candidate bar, or we'll see another bogus PF 21 result. The orient flags this; Phase 2 implementation has to bake it in.

**Recommended sequencing:**

1. **Phase 1 (build the classifier):** add `acme/wyckoff/` package, migration for two tables, conductor wiring with feature flag (default off), `WyckoffClassifier.on_bar` returns snapshot. Lock the parameter defaults (timeout windows, lookback, ATR period) and don't fiddle. Verify it runs offline against the parquet without writing anything anomalous. **No strategy yet.**
2. **Phase 1.5 (harness wiring):** modify `scripts/backtest_new_fleet.py` to accept an optional `wyckoff_classifier` and inject state into strategies that consume it. ~30 lines.
3. **Phase 2 (Spring Strategy):** follow the existing five-phase pattern (orient → extract → backtest → OOS → register). The strategy gates on classifier state; the classifier is the dependency.
4. **Phase 3 (dashboard):** read `wyckoff_events` + `wyckoff_state`, render alongside the FLEET ACTIVITY panel. Mirrors the Phase 4 eval_log dashboard work.

**One concrete thing to verify before approving Phase 1 build:** apply the lookahead-mitigation pattern (arm-on-candidate, confirm-on-recovery) at design time, not after. Otherwise we're guaranteed to repeat the level_rvol Phase 1 outcome.

---

## Status

Phase 1 orient complete. No code changes. Runner still unloaded.

**Awaiting explicit go** for Phase 1 build (classifier package + migrations + conductor wiring + offline parquet validation, no strategy code yet).

Open parameter decisions you'll need to confirm before build (suggested defaults in parentheses):

- SC arming-window to AR (30 bars)
- AR arming-window to ST (30 bars)
- ST arming-window to Spring (30 bars)
- Spring arming-window to SOS (5 bars)
- Multi-bar low lookback for SC detection (20 bars)
- ATR period for spread comparison (14)
- "Near" tolerance for level holds (2 ticks)
- What happens when an arming window expires (revert to prior state vs. reset to UNKNOWN)
- Single state-machine thread or parallel accumulation/distribution tracking

Either confirm these defaults or override before we touch code.
