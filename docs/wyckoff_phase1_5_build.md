# Wyckoff Phase 1.5 — backtest harness integration

`scripts/backtest_new_fleet.py:backtest_strategy` now accepts an optional `wyckoff_classifier` argument and drives `classifier.on_bar(bar)` immediately before each `strategy.on_bar(bar, ...)` call, injecting the resulting snapshot via `strategy.wyckoff_state`. ~10 lines of new code in the harness. No strategy code changed. 609/609 tests pass. Ruff clean.

Plus one unrelated bug-fix surfaced by the verification — described in §3.

---

## 1. Migration status

Confirmed in Supabase before starting:

```
wyckoff_state    EXISTS (rows: 0)
wyckoff_events   EXISTS (rows: 0)
```

Both tables present, both empty. (You applied the migration; classifier hasn't run in live yet, so no rows.)

---

## 2. Harness change

[scripts/backtest_new_fleet.py](scripts/backtest_new_fleet.py) — two edits.

**Signature:**

```python
def backtest_strategy(strategy, bars_2min, *, name,
                     levels_by_date=None,
                     wyckoff_classifier=None) -> list[dict]:
```

**Per-bar drive (inserted immediately before the existing `strategy.on_bar(...)` call):**

```python
# Drive the Wyckoff classifier BEFORE the strategy call, inject the
# snapshot via strategy.wyckoff_state. Duck-typed: strategies that
# don't consume it ignore the attribute.
if wyckoff_classifier is not None:
    strategy.wyckoff_state = wyckoff_classifier.on_bar(bar)
```

When `wyckoff_classifier` is `None` (the default), the new lines are skipped entirely — **backwards-compatible with every existing strategy backtest**. No existing call site needs to change.

**Lookahead invariant preserved:** the classifier sees only bar T's data when computing the snapshot, then the strategy sees the same bar T plus the snapshot. Any Spring event in the snapshot already has its timestamp set to the *recovery* bar (per Phase 1's arm-on-candidate / confirm-on-recovery design), so the strategy can't fire on the candidate. The Phase 2 Spring Strategy will read `wyckoff_state.new_events` and enter on the *next* bar.

---

## 3. Unrelated bug fix surfaced by the verification

While testing existing-strategy backwards compatibility, the harness crashed with:

```
AttributeError: 'EvalResult' object has no attribute 'size'
  scripts/backtest_new_fleet.py:245
```

**Root cause:** the eval_log Phase 2 work (already-merged PR #33) widened the strategy contract from `Signal | None` to `Signal | EvalResult | None`. The conductor was updated to handle `EvalResult`; the **backtest harness was not**. So every eval_log-instrumented strategy (BOUNDARY, OVERNIGHT_DRIFT, GAP_FILL, GO_NO_GO_LEVELS) crashes the harness today the moment it returns an `EvalResult` instead of `None` — which is on most bars.

**Fix:** treat `EvalResult` the same as `None` in the harness — neither is a tradeable signal.

```python
# OLD (line 245)
if sig is None or sig.size == 0 or sig.bracket is None:
    continue

# NEW
if sig is None or isinstance(sig, EvalResult):
    continue
if sig.size == 0 or sig.bracket is None:
    continue
```

This unblocks backtesting of all four currently-deployed strategies. The harness doesn't write to `eval_log` — that's the conductor's job in live; the backtest just skips non-Signal returns.

**Strictly speaking this is out of Phase 1.5 scope.** It's a one-line fix, it's required for Phase 2 (Spring Strategy backtest will need to call the harness), and leaving the harness broken blocks any future strategy work on this codebase. Including it here for that reason. Flagging it explicitly so it doesn't sneak through review without notice.

---

## 4. Verification — 5 trading days

Used the last 5 trading days of the parquet (2026-04-23 → 2026-04-30, 3,946 2-min bars).

### Test 1 — passive observer with classifier

A tiny inline `Observer` strategy that returns `None` on every bar and records the snapshots received via `self.wyckoff_state`. Confirms the pipe works end-to-end.

```
events captured by observer: {'sc': 16, 'ar': 14, 'st': 7, 'spring': 4, 'sos': 4}
classifier _last_event_kind at end: sc
```

3,946 snapshots passed through; observer recorded 45 bars with new events; counts match what the classifier emitted internally.

### Test 2 — GO/NO-GO LEVELS without classifier (backwards-compat after the EvalResult patch)

```
GNL closes (no crash): 2
```

The eval-log-instrumented strategy now runs cleanly through the harness. Pre-patch: instant crash.

### Test 3 — BOUNDARY with classifier (combined path)

```
BOUNDARY closes (no crash, classifier ran too): 1
classifier saw events: bar_idx at end = 3946
```

Both the classifier and an eval_log-instrumented strategy run in parallel through the harness without conflict.

---

## 5. Files touched

- **`scripts/backtest_new_fleet.py`** — added optional `wyckoff_classifier` kwarg, per-bar drive, and the EvalResult-handling fix. ~15 lines net.

No strategy code, no live runner contact (still unloaded). No new files. No new tests required — existing 609/609 still pass.

---

## 6. Status

Phase 1.5 complete.

- Harness accepts `wyckoff_classifier`: ✅
- Classifier runs before strategy on_bar per spec: ✅
- Snapshot injected via `strategy.wyckoff_state`: ✅
- Backwards-compatible (`wyckoff_classifier=None` is default): ✅
- 5-day verification: ✅
- Bonus EvalResult bug fix: ✅ (out of scope but blocked downstream work)

Phase 2 (Spring Strategy) can now run through this harness with the classifier driving alongside. The Spring Strategy will gate on `self.wyckoff_state.phase == Phase.ACCUMULATION` and consume `Spring` events from `self.wyckoff_state.new_events`, entering on the bar AFTER the recovery bar.

Awaiting direction — commit + PR for Phase 1 + Phase 1.5 together, or hold for next step?
