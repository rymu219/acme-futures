# Eval-log build — Phase 3 conductor + charge_pct

`charge_pct` computed and included in every `EvalResult.gate_values` across all four strategies. `Db.write_eval_log` added. Conductor wired to dispatch `EvalResult` → `eval_log` and emit `ENTRY` rows on `Signal` fires. **Live runner not restarted.** 551/551 tests passing, lint clean.

---

## 1. Write path (matches the existing pattern)

[src/acme/db.py:58-91](src/acme/db.py:58) — new method `Db.write_eval_log`. Same fire-and-forget pattern as `log_event`: synchronous insert wrapped in try/except so failures don't tear down the bar loop. `near_miss` is required (no default) per the Phase 1 finding that PostgREST overrides Postgres defaults with explicit nulls on missing JSON keys.

```python
def write_eval_log(
    self, *,
    bar_ts: datetime, strategy: str, outcome: str, near_miss: bool,
    gate_failed: str | None = None, signal_side: str | None = None,
    gate_values: dict[str, Any] | None = None, reason: str | None = None,
) -> None:
    row = {
        "bar_ts": bar_ts.isoformat(), "strategy": strategy,
        "outcome": outcome, "near_miss": bool(near_miss),
        "gate_failed": gate_failed, "signal_side": signal_side,
        "gate_values": gate_values or {},
        "reason": (reason or "")[:120],
    }
    try:
        self.client.table("eval_log").insert(row).execute()
    except Exception as e:
        log.error("db_eval_log_write_failed", ...)
```

## 2. Conductor wiring

[src/acme/conductor/conductor.py](src/acme/conductor/conductor.py) — `_process_bar` loop, the only place `on_bar()` is called. Two new branches added; the existing Signal arbitration path is unchanged.

```python
sig = inst.on_bar(...)

# EvalResult branch — write eval_log, skip arbitration
if isinstance(sig, EvalResult):
    if self.db is not None:
        try:
            self.db.write_eval_log(
                bar_ts=bar.t, strategy=rec.name,
                outcome=sig.outcome, near_miss=sig.near_miss,
                gate_failed=sig.gate_failed, signal_side=sig.signal_side,
                gate_values=sig.gate_values, reason=sig.reason,
            )
        except Exception as e:
            log.warning("eval_log_write_eval_failed", ...)
    self._telemetry.log(..., signal=None, ...)   # sqlite telemetry expects Signal|None
    continue

# Existing path: sig is Signal | None
bar_event_id = self._telemetry.log(..., signal=sig, ...)
if sig is None:
    continue

# ENTRY branch — Signal fired; write matching eval_log row
if self.db is not None and sig.size > 0:
    try:
        last_gv = dict(getattr(inst, "_last_gate_values", None) or {})
        last_gv["fired"] = True
        self.db.write_eval_log(
            bar_ts=bar.t, strategy=rec.name,
            outcome="ENTRY", near_miss=False,
            signal_side=sig.side, gate_values=last_gv, reason=sig.reason,
        )
    except Exception as e:
        log.warning("eval_log_write_entry_failed", ...)

self._emit_signal_event(rec, sig, bar)
# ... arbitration unchanged ...
```

**Non-blocking property preserved:** both `write_eval_log` calls are synchronous-wrapped-in-try/except — exactly the pattern `log_event` uses. Same latency budget (~50-150ms per insert per Phase 0). Write failures land in structlog `log.warning` and are silently swallowed by the bar loop. eval_log writes never delay bar processing or cascade an error to the live runner.

**ENTRY row gate_values:** each strategy now stashes `self._last_gate_values` immediately after computing them inside `on_bar`. When a Signal fires, the conductor reads that stash via `getattr(inst, "_last_gate_values", None)`, adds `"fired": True`, and writes it as the ENTRY row's gate_values. Strategies that don't stash (none in the current fleet) gracefully degrade to an empty dict.

## 3. charge_pct formulas (as confirmed)

All four formulas implemented verbatim with your OVERNIGHT_DRIFT refinement.

### GO/NO-GO LEVELS
[src/acme/strategies/go_no_go_levels.py:_compute_charge_pct](src/acme/strategies/go_no_go_levels.py):

```
sep_term     = min(abs_sep / sep_thr, 1.0)
rvol_term    = min(vr / vr_thr, 1.0)
slope_term   = 1.0 if (up_aligned or down_aligned) else 0.0
level_term   = max(0, 1 - level_dist_ticks / buffer_ticks)
charge_pct   = mean(sep, rvol, slope, level), clamped to [0, 1]
```

`level_term` is 0 when no levels are set or `level_dist_ticks is None`. Reflects the 4-gate structure of the strategy.

### BOUNDARY
[src/acme/strategies/boundary.py:_compute_charge_pct](src/acme/strategies/boundary.py):

```
actionable_exhaustion = 1.0 iff exh.direction is top|bottom AND that side is allowed
                       else 0.0
level_proximity_charge = max(0, 1 - abs(dist_ticks) / buffer_ticks)
                         (dist_ticks: high-side for top, low-side for bottom; 0 if no level)
charge_pct = 0.5 * actionable_exhaustion + 0.5 * level_proximity_charge,
             clamped to [0, 1]
```

Two-gate structure (exhaustion direction + level proximity), equal weight.

### OVERNIGHT_DRIFT — your refinement applied
[src/acme/strategies/overnight_drift.py:_compute_charge_pct](src/acme/strategies/overnight_drift.py):

```
if body <  min_body_points:                     return 0.0   # too weak / doji / bearish
if body >  weak_body_threshold_points:          return 0.0   # too strong (the cliff)
charge_pct = (body - min_body_points) / (weak_body_threshold_points - min_body_points)
```

Triangle function with peak (=1.0) at `weak_body_threshold_points`. Anything outside the band — too weak OR too strong — reads 0. Defaults: peak at 3.0pt body; 2.5pt body → 0.5 charge; 4.0pt body → 0 (over the cliff).

### GAP_FILL
[src/acme/strategies/gap_fill.py:_compute_charge_pct](src/acme/strategies/gap_fill.py):

```
charge_pct = min(1.0, abs_gap_points / min_gap_points)
```

Linear ramp; saturates at the threshold. 0 when `abs_gap` is None (no prior close).

## 4. Dry-run row-count verification

Drove each of the four production strategies through 5,325 2-min bars (2026-04-21 → 2026-04-30, 8-9 trading days; the parquet's tail) and counted what would land in `eval_log`. The harness exercises the Phase 2 EvalResult path directly; the Phase 3 conductor wiring is structural (`isinstance` dispatch + `write_eval_log` call) and is exercised separately by the test suite (`551 passed`).

| Strategy          | Total bars | PASS    | NEAR | HOLD | ENTRY | warmup | eval_log rows |
|-------------------|-----------:|--------:|-----:|-----:|------:|-------:|--------------:|
| boundary          | 5,325      | 118     | 3    | 0    | 3     | 5,201  | **124**       |
| overnight_drift   | 5,325      | 5,325   | 0    | 0    | 0     | 0      | **5,325**     |
| gap_fill          | 5,325      | 5,320   | 0    | 0    | 5     | 0      | **5,325**     |
| go_no_go_levels   | 5,325      | 5,241   | 39   | 0    | 4     | 41     | **5,284**     |
| **Total**         |            |         |      |      |       |        | **16,058**    |

**Rate interpretation** (the spec's "~4-8 per strategy per 2-min bar during active windows" is ambiguous, so I'm reporting all three plausible aggregations):

- **Per strategy per 2-min bar (non-warmup):** ~1 row. Each strategy writes one row per bar it evaluates — that's the design.
- **Across all 4 strategies per 2-min bar:** ~3.0 rows (16,058 / 5,325 ≈ 3.0). Boundary is mostly warmup so it pulls the average down; the other three are close to 1.0 each.
- **Per day total across all 4:** ~1,780 rows/day (16,058 / 9 days). Per hour: ~75. Per 2-min bar: ~3.

NEAR rows specifically (the invisible-save signal): 42 across the 5-day window, ~5/day. Concentrated in go_no_go_levels (39 of 42) — boundary contributed 3, the other two zero. That tracks intuition: GO/NO-GO has the most external-blocker gates (window, direction policy), so it's the easiest place for "all internal gates pass + external blocks."

**Boundary's high warmup rate** (5,201 of 5,325 = 97.7%) is expected: the ExhaustionDetector returns None on every bar that doesn't form a valid exhaustion pattern (small body + below-avg volume + new local extreme + close in opposite half), and BOUNDARY's `if exh is None: return None` warmup guard kicks in for those — bare None, no eval_log row. Only the 124 bars that produce a complete exhaustion signal get logged. This is correct — eval_log captures the strategy's *evaluated decisions*, not bars where the upstream detector hadn't formed a candidate yet.

**Overnight_drift writes on every bar** because its `required_history_bars()` returns 0 — there's no warmup guard. Every bar gets an EvalResult (PASS during the 16:00-16:59 maintenance break, PASS while building the bias bar, etc.). Phase 4 might want to filter the dashboard activity feed by outcome to keep the "still working" PASSes from drowning out the interesting NEAR rows.

## 5. Live runner — explicitly untouched

PID was 72979 at the start of this phase per the last verification. **I did not restart it.** The live runner is still executing the pre-Phase-2 code path (strategies return None for non-fires; conductor handles `Signal | None` only). The Phase 2 + Phase 3 code lives in the worktree and master will pull it only after the user merges + runs `git pull && kill <pid>`.

**Why this matters:** restarting the live runner now would activate Phase 2's `EvalResult` returns *with* Phase 3's conductor branch already wired — that's the intended deployment. But the eval_log table also needs to exist in Supabase (it does — verified in Phase 1) and the `_last_gate_values` attribute needs to be set before first read (it is — every strategy stashes it immediately on entering on_bar). Order of operations on the eventual restart: Phase 1 (table exists ✓) → Phase 2 (strategies return EvalResult ✓) → Phase 3 (conductor handles EvalResult ✓) → user merges + restarts → eval_log rows start landing.

**Activation chain has one more dependency:** Phase 4 (dashboard reader). Without Phase 4 the rows still land but nothing surfaces them. The runner is safe to restart after Phase 4 lands; rows accumulate immediately and the dashboard reads them on its next refresh.

## Status

Phase 3 complete.

- charge_pct: 4 formulas implemented as confirmed (with OVERNIGHT_DRIFT triangle refinement)
- `Db.write_eval_log` added; matches `log_event` fire-and-forget pattern
- Conductor: EvalResult branch + ENTRY branch wired; existing Signal arbitration path untouched
- Strategies stash `_last_gate_values` on every on_bar entry so ENTRY rows get the full gate context
- Dry-run on 9 days: 16,058 rows total, ~75/hour, ~3/bar across the fleet
- 551/551 tests passing, ruff clean
- Live runner not touched

Awaiting explicit go for Phase 4 (dashboard reader).
