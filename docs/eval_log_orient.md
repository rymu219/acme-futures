# Eval-log build — Phase 0 orient

Read-only inventory of the surfaces this build will touch. No code changes.

## Q1 — Current Supabase write path

**`src/acme/db.py:29-56` → `Db.log_event(kind, ...)` → `client.table("broker_events").insert(row).execute()`**

```python
class Db:
    def log_event(
        self, kind: str, *,
        contract_id, symbol, account_id, side, size, price, strategy, raw,
    ) -> None:
        row = {"kind": kind, ..., "raw": raw or {}}
        try:
            self.client.table("broker_events").insert(row).execute()
        except Exception as e:
            log.error("db_log_event_failed", kind=kind, error=str(e))
```

- **Single chokepoint** — every Supabase row written by the conductor goes through this method. Synchronous call wrapped in `try/except` so write failures don't tear down the bar loop. This is the existing pattern to match for `eval_log` writes.
- Callers of `log_event` (all in `conductor.py`): `_emit_signal_event` (`signal_emitted`), `_emit_arbitration_events` (`signal_arbitrated`, `signal_suppressed`), `_open_phantom_position` (`dry_run_signal`), `_execute_signal` (`order_submitted`), `_flatten_position` (`flatten_triggered`), plus `risk_block` and `calendar_block` inline emits in `_process_bar`.
- One sibling write surface for heartbeats: `Db.write_heartbeat()` at `db.py:322` — upserts to `runtime_heartbeats`. Separate function because it's an upsert, not an insert. We'll reuse the `log_event`-style insert pattern for `eval_log`.

## Q2 — Migration runner

**There is no automated migration runner.** Migrations are SQL files in `migrations/`, applied manually in Supabase Studio.

- Path: `/Users/ryanmurphy/acme-futures/.../migrations/`
- Convention: `YYYY_MM_DD_<short_description>.sql`
- Existing examples (5 files, all 2026-05):
  - `2026_05_04_ryan_spec_v3_trades.sql`
  - `2026_05_05_runtime_heartbeats_and_config.sql`
  - `2026_05_05_strategy_id_on_v3_trades.sql`
  - `2026_05_12_bar_levels.sql`
  - `2026_05_12_operator_events.sql`
- Conventions observed in every migration:
  - Header SQL comment block explaining purpose, columns, severity routing, etc.
  - `create table if not exists ...` (idempotent)
  - `create index if not exists ...`
  - Seed rows where applicable use `insert ... on conflict do nothing`
  - Bottom comment line: *"Apply against existing Supabase. Idempotent."* or similar
- Schema-source-of-truth note: there's *also* an inline `SUPABASE_DDL` string at `db.py:431-528` for the core `broker_events` / `daily_state` tables — that one is the legacy single-source-of-truth. Newer tables (`runtime_heartbeats`, `operator_events`, `bar_levels`) live in `migrations/` only.

**Application:** the DDL is run manually via Supabase Studio SQL editor — or, since the `Db` client has full access via the service-role key, we can apply it programmatically via `db.client.postgrest.rpc(...)` / raw SQL — but the established human convention is "apply in Studio after PR merge."

## Q3 — Existing "I evaluated and did nothing" hook

**Yes — but it doesn't write to Supabase.**

In `conductor.py:_process_bar` (lines 391-423), the conductor loops every active strategy on every bar and calls `inst.on_bar(...)`. **Critically, regardless of whether the strategy fires, it calls:**

```python
bar_event_id = self._telemetry.log(
    bar=bar, timeframe=tf, strategy=rec.name,
    signal=sig, context=ctx_features,    # signal may be None
    position=strat_pos,
    balance=starting_balance + state.realized_pnl,
)
```

`self._telemetry` is a `BarEventLogger` (`src/acme/telemetry.py`). It writes one row per `(strategy, bar)` to a **local SQLite file** at `~/.acme/telemetry/bar_events.db` — not to Supabase. The schema captures `bar_t`, `timeframe`, `strategy`, `sig_side`, `sig_size`, `sig_reason`, a fixed set of context features (volume_ratio_20, momentum_5, range_vs_atr, etc.), and a `fired` flag (`1` when signal had size > 0, `0` otherwise).

**What this means for the eval-log build:**
- The "evaluate every bar regardless of fire" *loop* already exists; we don't need to add it.
- The *Supabase write* for non-fires doesn't exist; that's what this build adds.
- The existing telemetry sqlite logger is **per-process local** — useless to the dashboard. The new `eval_log` table fills the gap by mirroring the per-bar evaluation to Supabase.
- Schemas don't overlap: the sqlite telemetry captures fixed ctx features; the new `eval_log` will capture strategy-specific `gate_values` as JSONB.

## Q4 — What does on_bar() currently return when not trading?

**Plain `None`.** No typed object, no diagnostic. All four production strategies (`boundary.py`, `overnight_drift.py`, `gap_fill.py`, `go_no_go_levels.py`) return `None` at every gate-failure / wait point.

Sample sites where rich information exists but is discarded:

| Strategy          | Site (line approx)                          | Discarded information                                                       |
|-------------------|---------------------------------------------|------------------------------------------------------------------------------|
| boundary          | `if entry_hour in blacklist: return None`   | which level was closest, exhaustion direction, ATR                          |
| boundary          | `if abs(lvl - bar.h) > buffer: return None` | distance to nearest level in ticks, level name, exhaustion strength          |
| overnight_drift   | `if body <= 0: return None`                 | bias body magnitude, sign, bias window minutes, distance to entry time      |
| overnight_drift   | `if body > weak_threshold: return None`     | body overshoot vs threshold, "too strong" diagnostic                         |
| gap_fill          | `if abs(gap) < min_gap: return None`        | gap magnitude, direction, distance below threshold                          |
| gap_fill          | `if target_distance <= 0: return None`      | how far through the fill we entered, was-wrong-side flag                     |
| go_no_go_levels   | `if signal_raw == 0: return None`           | sep_ok / vr_ok / up_aligned / down_aligned booleans, abs_sep, vr, slope vals |
| go_no_go_levels   | `if not self._in_window(bar.t): return None`| GO/NO-GO signal direction that would have fired                              |
| go_no_go_levels   | `if not self._level_proximity_ok(...)`      | which side was checked, distance to nearest matching level                  |

All four strategies have a top-of-`on_bar` warmup guard (`if gng is None or not self._atr.is_warm: return None` or equivalent) — those should *stay* as bare `None` per the spec. Every other `None` is a candidate for `EvalResult`.

## Surrounding-system notes worth flagging

- **`Db.log_event` is synchronous, not async.** The conductor's `_process_bar` is async but every Supabase write is a blocking `requests`-backed call. The bar loop tolerates this because Supabase writes typically take 50-150ms and bars are 2 min apart. The same pattern works for `eval_log` writes — but writing 4 rows per 2-min bar (one per strategy) is 4× the synchronous load. Still fine in latency budget (~600ms per bar set vs 120,000ms bar interval), but worth a watch.
- **The conductor still ignores `None` returns** by design (`if sig is None: continue` at line 424). When `on_bar` starts returning `EvalResult`, we'll need to teach the conductor to distinguish `EvalResult` from `Signal` and route the former to `eval_log` writes. That's a Phase 3 task, not Phase 2.
- **The `Strategy` Protocol at `base.py:39-56` declares `on_bar(...) -> Signal | None`.** Adding `EvalResult` to the return type changes the Protocol — needs updating in Phase 2.
- **`bar_event_id` from the sqlite telemetry is carried into `dry_run_signal` and `dry_run_close` events** as a way to link Supabase rows back to local sqlite rows. The new `eval_log` rows live in Supabase, so they don't need this linkage — they'll be standalone rows keyed by `(bar_ts, strategy)`.

## Anticipated structural concerns (none blocking)

1. **Signal write semantics unchanged.** The spec says "Do NOT change the Signal return path." Reading the code confirms this is straightforward — `Signal` returns flow through `_emit_signal_event` / arbitration unchanged. `EvalResult` returns will be intercepted *before* that path and dispatched to `eval_log` instead.
2. **Per-strategy charge_pct formulas.** Each of the four strategies will need its own definition (Phase 3 spec). For three of them (overnight_drift, gap_fill, boundary) the natural formulation isn't immediately obvious from a quick read — I'll think through each in Phase 3 and document the reasoning in the conductor doc.
3. **No async batch infrastructure exists.** If `eval_log` write volume turns out to be a problem, we'd add batching — but it's not needed for the initial build. Same fire-and-forget pattern as `log_event`.

## Status

Phase 0 complete. No code changes, no live runner contact. Awaiting explicit go for Phase 1 (migration + schema).
