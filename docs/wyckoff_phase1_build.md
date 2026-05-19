# Wyckoff Phase Classifier — Phase 1 build

Package built, migrations written, conductor wired with feature flag (default off), classifier validated offline against the 25-month parquet. **No strategy code.** **Live runner not touched** (still unloaded; will stay feature-flagged off even after reload until `fleet_runner.py` is changed to pass a `WyckoffClassifier` instance).

609/609 tests pass. Ruff clean.

---

## 1. Package — `src/acme/wyckoff/`

Peer to `acme/regime/`. Four files:

```
src/acme/wyckoff/
├── __init__.py          # re-exports
├── state.py             # Phase + EventKind enums (StrEnum)
├── events.py            # WyckoffEvent dataclass + to_db_row
└── classifier.py        # WyckoffConfig, WyckoffSnapshot, WyckoffClassifier
```

### Data model

- `Phase` — `unknown | accumulation | markup | distribution | markdown`. v1 single-threaded state machine; distribution / markdown reached only via UT detection (deferred — see §6).
- `EventKind` — `sc | ar | st | spring | sos | ut`. v1 drives the accumulation sequence; `ut` defined but not detected.
- `WyckoffEvent` — frozen dataclass, one per confirmed event. Carries OHLCV at the *event* bar (which is the **recovery bar** for Spring, not the candidate bar), spread/ATR context, volume context, ref to the prior event in the sequence, and a reasoning trace.
- `WyckoffSnapshot` — frozen dataclass, one per bar (whether or not events fire). Carries current phase, last-event reference, armed-for-next state, spread/ATR + RVOL on this bar, and any `new_events` confirmed this bar.

### Config (defaults from Phase 0)

| Knob                              | Default | Source                                            |
|-----------------------------------|--------:|---------------------------------------------------|
| `atr_period`                      | 14      | Matches BOUNDARY / regime engine                  |
| `rvol_sma_period`                 | 20      | Matches GO/NO-GO LEVELS RVOL                      |
| `multi_bar_low_lookback`          | 20      | Phase 0 default                                   |
| `near_tolerance_ticks`            | 2       | Phase 0 default                                   |
| `sc_volume_multiple`              | 2.0     | User's SC spec ("≥ 2× 20-bar avg")               |
| `sc_spread_atr_multiple`          | 1.5     | User's SC spec ("≥ 1.5× ATR")                    |
| `sc_close_position_min`           | 0.70    | User's SC spec ("close in upper 30%")             |
| `ar_window_bars`                  | 30      | Phase 0 default                                   |
| `ar_min_advance_atr`              | 0.5     | Implementation choice (orient flagged as open)    |
| `ar_volume_max_frac_sc`           | 1.0     | User's AR spec ("moderate volume < SC volume")    |
| `st_window_bars`                  | 30      | Phase 0 default                                   |
| `st_volume_max_frac_sc`           | 0.60    | User's ST spec ("volume < 60% of SC")             |
| `spring_window_bars`              | 30      | Phase 0 default                                   |
| `spring_volume_max_frac_st`       | 0.80    | User's Spring spec ("volume < 80% of ST")         |
| `spring_recovery_max_bars`        | 3       | User's Spring spec ("recovers within 1-3 bars")   |
| `sos_window_bars`                 | 5       | Phase 0 default                                   |
| `sos_spread_atr_multiple`         | 1.0     | User's SOS spec ("wide spread")                   |
| `sos_volume_expanding`            | True    | User's SOS spec ("expanding volume")              |

### Look-ahead-free design (load-bearing)

Spring detection is the only event with a forward-looking definition ("recovers above ST low within 1-3 bars"). Implementation:

1. **On the candidate bar** (price breaks below ST low on light volume): create an in-memory `_SpringCandidate` record. **No event written. No state change. No phase transition.**
2. **On each subsequent bar** (within `spring_recovery_max_bars`): check if `bar.c > st_low`. If yes — emit the Spring event with `bar_ts` set to **this bar** (the recovery bar), not the candidate. If no — wait.
3. **If the window expires without recovery**: drop the candidate silently. No event written.

A Spring event in `wyckoff_events` therefore corresponds to a recovery bar. The future Phase 2 Spring Strategy will enter on the *next* bar after the recovery, mirroring the execution-lag pattern used by other strategies — no lookahead, no opportunity for the level_rvol Phase 1 mistake to recur.

---

## 2. Migration — `migrations/2026_05_19_wyckoff.sql`

Two tables, mirrors the existing convention (idempotent `create table if not exists`, apply via Supabase Studio).

```
wyckoff_state                            wyckoff_events
├── id                bigserial PK       ├── id                bigserial PK
├── ts                timestamptz        ├── bar_ts            timestamptz
├── timeframe         text               ├── contract          text
├── contract          text               ├── event_kind        text
├── phase             text               ├── phase_at_event    text
├── last_event        text               ├── bar_o/h/l/c       numeric
├── last_event_ts     timestamptz        ├── bar_v             bigint
├── armed_for         text               ├── spread_pts        numeric
├── spread_atr        numeric            ├── atr_pts           numeric
├── rvol              numeric            ├── spread_atr_ratio  numeric
├── raw_state         jsonb              ├── rvol              numeric
└── created_at        ...                ├── volume_vs_anchor  numeric
                                          ├── ref_event_kind    text
                                          ├── ref_event_ts      timestamptz
                                          ├── notes             text
                                          └── created_at        ...

INDEXES                                  INDEXES
  ts desc                                  bar_ts desc
  phase, ts desc                           event_kind, bar_ts desc
  contract, ts desc                        contract, bar_ts desc
```

**Not applied yet** — same constraint as Phase 1 of the eval_log build. The Supabase service-role key authenticates against PostgREST, which doesn't support DDL. To apply: paste `migrations/2026_05_19_wyckoff.sql` into Supabase Studio → SQL Editor → Run.

The classifier package + conductor wiring do not require the tables to exist — the conductor's persistence is wrapped in try/except per the established pattern. Tables can be applied any time before turning the feature flag on.

---

## 3. Db methods — `src/acme/db.py`

Three new methods, all fire-and-forget (same pattern as `log_event`, `write_eval_log`):

- `write_wyckoff_snapshot(row: dict)` — append to `wyckoff_state`
- `write_wyckoff_event(row: dict)` — append to `wyckoff_events`
- `fetch_recent_wyckoff_events(*, contract, limit)` — read most recent N events in chronological order for replay-on-startup

---

## 4. Conductor wiring — feature flag DEFAULTS OFF

`src/acme/conductor/conductor.py` changes:

- New optional constructor args: `wyckoff_classifier: WyckoffClassifier | None = None` (default None) and `wyckoff_timeframe_minutes: int = 2`.
- When `wyckoff_classifier is None` (the default): the entire Wyckoff code path in `_process_bar` is skipped. **Zero behavioral change vs pre-build.** Lint, tests, and runtime are all identical for the existing fleet.
- When a classifier is passed: `_process_bar` calls `classifier.on_bar(bar)` on bars at the wyckoff timeframe, writes the snapshot + any new events via the new Db methods (both wrapped in try/except).
- New helper `_replay_wyckoff_from_db()` runs once in `__init__` when both `wyckoff_classifier` and `db` are set — fetches the last 20 confirmed events and replays them so the in-memory state machine starts from the last confirmed transition rather than UNKNOWN.

### Activation requires a deliberate code change

To turn it on, `src/acme/fleet_runner.py` (or wherever the live `Conductor` is instantiated) has to pass an instance:

```python
from acme.wyckoff import WyckoffClassifier
...
conductor = Conductor(
    broker, db, config, registry,
    dry_run=dry_run, telemetry=telemetry,
    wyckoff_classifier=WyckoffClassifier(),  # ← opt in here
)
```

**This change has NOT been made.** The feature flag stays off until you deliberately turn it on.

---

## 5. Offline parquet validation

Drove the full 25-month parquet (368,441 2-min bars) through the classifier — no Supabase writes, no live runner contact.

### Event counts

| Event   | Count   | Per-month |
|---------|--------:|----------:|
| SC      | 1,486   | ~59       |
| AR      | 1,170   | ~47       |
| ST      | 639     | ~26       |
| Spring  | 273     | ~11       |
| SOS     | 181     | ~7        |

**SC → AR follow-through:** 78.7%. **AR → ST:** 54.6%. **ST → Spring confirmed:** 42.7%. **Spring → SOS:** 66.3%. **Full SC→SOS chains: 181** (≈ 7/month). Trade frequency for a future Spring Strategy at this default config would be in the 5-10/month range. Workable but sparse — same magnitude as the other fleet members.

### Arm-on-candidate / confirm-on-recovery — the load-bearing mechanic

**Spring candidates armed:** 593
**Spring events confirmed:** 273
**Confirmation rate:** 46.0%

If the classifier had lookahead, this rate would be 100% (every candidate would be a confirmation by construction). Instead, **320 candidates expired** without recovery — the silent-drop path. This is exactly the behaviour we built in, and exactly what the level_rvol Phase 1 mistake was lacking.

Recovery-lag distribution:

| Bars between candidate & confirmation | Count |
|---------------------------------------|------:|
| 1 bar                                 | 174   |
| 2 bars                                | 58    |
| 3 bars                                | 41    |
| **All ≤ 3 bars per `spring_recovery_max_bars` config** | **YES** |

The classifier is detecting recovery on the next bar most of the time (64% at 1-bar lag), with a tail at 2-3 bars. Bars > 3 are dropped per the config.

### Phase-bar distribution

| Phase           | Bars      | %     |
|-----------------|----------:|------:|
| `unknown`       | 210       | 0.1%  |
| `accumulation`  | 332,808   | 90.3% |
| `markup`        | 35,423    |  9.6% |
| `distribution`  | 0         |  0.0% |
| `markdown`      | 0         |  0.0% |

90% of bars sit in ACCUMULATION — expected, since the classifier transitions to MARKUP only on SOS and currently has no path back to a new accumulation cycle (the next SC just re-arms the sequence under MARKUP, overwriting state). MARKUP at 9.6% reflects the time between SOS and the next SC. **Distribution / markdown remain at zero in v1 by design** — UT detection is deferred to a follow-up phase.

### Sequence-ordering integrity

`Out-of-sequence events: 0`. The state machine enforces SC → AR → ST → Spring → SOS strictly; no AR is ever written without a prior SC, no Spring without a prior ST, etc.

### Sample SOS confirmations (first 5)

```
2024-04-02 08:00:00 UTC  SOS  spread=2.25pt  rvol=1.95   ← Spring ref 4 min earlier
2024-04-05 07:54:00 UTC  SOS  spread=1.75pt  rvol=1.32   ← Spring ref 4 min earlier
2024-04-09 18:08:00 UTC  SOS  spread=5.25pt  rvol=1.48   ← Spring ref 4 min earlier
2024-04-10 08:14:00 UTC  SOS  spread=1.25pt  rvol=1.20   ← Spring same bar (multi-event)
2024-04-16 22:00:00 UTC  SOS  spread=5.50pt  rvol=3.37   ← Spring ref 66 min earlier
```

Each SOS has a `ref_event_ts` pointing to the prior Spring's *recovery* bar (per the lookahead-free design). The 2024-04-10 row shows a same-bar Spring+SOS confirmation, which is rare but legal — the recovery bar happened to also satisfy SOS's wide-spread-on-expanding-volume criteria.

---

## 6. What's deferred to follow-up phases (explicit)

- **UT (Upthrust) detection and the distribution sequence.** v1 single-threaded state machine handles accumulation only. Distribution would need its own anchors (BC → AR_dist → ST_dist → UT) and either a parallel-thread state machine or a separate classifier instance. The `EventKind.UT` enum is defined for forward compatibility but never emitted.
- **`Phase 1.5` — backtest harness integration.** `scripts/backtest_new_fleet.py` doesn't currently instantiate a classifier. The Phase 2 Spring Strategy will need ~30 lines of harness modification so backtests can drive the classifier alongside the strategy. Out of scope here.
- **Phase 2 — Spring Strategy.** Will gate on `wyckoff_state.phase == accumulation` AND consume Spring events from `wyckoff_events`. Same five-phase pattern (orient / extract / backtest / OOS / register).
- **Phase 3 — dashboard panel.** Reads `wyckoff_state` + `wyckoff_events`, renders alongside FLEET ACTIVITY.

---

## 7. To activate (when you're ready)

In order:

1. Apply `migrations/2026_05_19_wyckoff.sql` in Supabase Studio.
2. (Optional but recommended) load the launchd job: `launchctl load ~/Library/LaunchAgents/com.acme-futures.fleet-runner.plist`. The runner will come up with the new code but Wyckoff stays off (default).
3. To turn Wyckoff ON in live, edit `src/acme/fleet_runner.py` to instantiate `WyckoffClassifier()` and pass it to the `Conductor` constructor. Commit, push, merge, pull, kill the runner. Watchdog respawns with Wyckoff classifier active.

Until step (3), nothing changes in live behaviour. The Wyckoff package is dormant infrastructure.

---

## Status

Phase 1 build complete.

- Package: `src/acme/wyckoff/` (4 files, ~500 LOC)
- Migration: `migrations/2026_05_19_wyckoff.sql` (two tables, three indexes each)
- Db methods: 3 new (write_wyckoff_snapshot, write_wyckoff_event, fetch_recent_wyckoff_events)
- Conductor wiring: optional + feature-flagged off
- Validation: 25 months, 368K bars, classifier processes in <3 sec, all sequencing invariants hold, arm-on-candidate / confirm-on-recovery confirmed working (46% confirmation rate, lags ≤ 3 bars)
- Tests: 609/609 passing; ruff clean

Awaiting your direction on whether to commit + open PR, or hold for any tweaks.
