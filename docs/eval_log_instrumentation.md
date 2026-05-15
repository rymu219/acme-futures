# Eval-log build — Phase 2 instrumentation

`EvalResult` added to `base.py`; all four production strategies now return rich diagnostic objects instead of bare `None` at every gate-failure / wait point. Trade-firing paths unchanged. Full test suite green (551/551), ruff clean.

## EvalResult dataclass

In [src/acme/strategies/base.py](src/acme/strategies/base.py):

```python
@dataclass(frozen=True)
class EvalResult:
    outcome: EvalOutcome              # 'PASS' | 'NEAR' | 'ENTRY' | 'HOLD'
    gate_failed: str | None
    near_miss: bool                   # REQUIRED — no default
    signal_side: Side | None
    gate_values: dict[str, Any]
    reason: str
```

**Every field is required (no defaults).** `near_miss` in particular has no default because Phase 1 verified PostgREST sends explicit `null` for missing JSON keys — that overrides the column default and trips the `NOT NULL` constraint. Making the field required at the dataclass level forces every call site to be explicit. The Strategy Protocol's `on_bar` return type was widened from `Signal | None` to `Signal | EvalResult | None`.

## Test-suite impact

One test failure in `tests/test_boundary.py::test_no_signal_when_already_in_position`. The test was asserting `sig is None` for the in-position case; now the strategy returns `EvalResult(outcome="HOLD")`. Per the spec's "fix the test to handle Signal | EvalResult | None" instruction, the assertion was widened to check that no Signal was emitted (regardless of None vs EvalResult), with an additional check that the EvalResult outcome is "HOLD". Final: **551/551 passed, lint clean**.

The other four `assert sig is None` sites in test_boundary.py pass unchanged because their bars don't produce a valid exhaustion pattern — the warmup-style early return at the top of `on_bar` still emits bare None, which is intentional.

## Per-strategy instrumentation

### Counts

| Strategy           | `return None` sites before | EvalResult returns after | Warmup `None` kept |
|--------------------|----------------------------|--------------------------|--------------------|
| boundary           | 7                          | 5 (HOLD, 3× PASS, 1× NEAR) | 1 (`exh None / levels None`)  |
| overnight_drift    | 9                          | 9 (HOLD, 7× PASS, 1× NEAR) | 0 (none — strategy has no warmup guard)  |
| gap_fill           | 9                          | 8 (HOLD, 6× PASS, 2× NEAR) | 0 (none) |
| go_no_go_levels    | 9                          | 11 (3× HOLD, 4× PASS, 2× NEAR + signal-side-aware HOLD branches) | 1 (`gng None / atr cold`) |

`go_no_go_levels` grew net positive because the flat-path was restructured: instead of short-circuiting on the first gate failure, all gates are evaluated then classified — necessary to detect "all gates passed → NEAR" cleanly. Trade-firing conditions unchanged; only the diagnostic paths got more granular.

### gate_values exposure per strategy (no new calculations — only surface-what-was-being-discarded)

- **boundary**: `exh_direction`, `entry_hour_ct`, `buffer_ticks`, `bar_close/high/low`, `atr`, `nearest_high_name/value/dist_ticks`, `nearest_low_name/value/dist_ticks`
- **overnight_drift**: `ct_minute`, `bias_open/close`, `bias_body_points`, `min_body_points`, `weak_body_threshold_points`, `entry_min`, `bias_window_start_min/end_min`, `day_entered`
- **gap_fill**: `ct_minute`, `entry_min`, `prior_close`, `bar_open/close`, `gap_points`, `abs_gap_points`, `min_gap_points`, `day_entered`, `intended_side`, `entry_price`, `target_distance_points`, `stop_distance_points`
- **go_no_go_levels**: `signal_raw`, `abs_sep`, `sep_thr`, `sep_ok`, `vr`, `vr_thr`, `vr_rising`, `vr_ok`, `slope_fast/slow`, `up_aligned`, `down_aligned`, `ema_fast/slow`, `in_window`, `side_checked`, `nearest_level_name/value`, `level_dist_ticks`, `buffer_ticks`, `level_proximity_ok`

All values come from computations the strategy already performs to decide whether to fire — nothing new was calculated. The instrumentation just makes the throw-away values visible.

---

## Concrete EvalResult examples (real outputs, not pseudocode)

Each example below is the actual output of the new `EvalResult` returned when the indicated strategy was driven through a crafted bar sequence. Values are exactly what the strategy emits; nothing is hand-edited.

### BOUNDARY — PASS (level-proximity miss)

Crafted scenario: warmed-up strategy, top-exhaustion bar at h=7494.00 with ONH=7496.25 — bar.high is 9 ticks below ONH, outside the 4-tick buffer.

```
outcome:        PASS
gate_failed:    'level_proximity'
near_miss:      False
signal_side:    'sell'
reason:         Level gate miss — nearest ONH -9.0t away (buffer 4t)
gate_values:
  exh_direction:             'top'
  entry_hour_ct:             7
  buffer_ticks:              4
  bar_close:                 7493.25
  bar_high:                  7494.0
  bar_low:                   7493.0
  atr:                       1.8622
  nearest_high_name:         'onh'
  nearest_high_value:        7496.25
  nearest_high_dist_ticks:   -9.0
  nearest_low_name:          'onl'
  nearest_low_value:         7471.0
  nearest_low_dist_ticks:    -88.0
```

### BOUNDARY — NEAR (entry hour blacklisted, ★ invisible save)

Crafted scenario: top-exhaustion bar with bar.high *exactly* at ONH=7496.25 (0 ticks away — well inside the 4-tick buffer), but the bar is at 10:00 CT which sits inside `entry_hour_blacklist_ct = (9, 10, 11, 12, 13)`.

```
outcome:        NEAR
gate_failed:    None
near_miss:      True       ★
signal_side:    'sell'
reason:         All gates PASS · ONH 0.0t — hour 10 CT blacklisted
gate_values:
  exh_direction:             'top'
  entry_hour_ct:             10
  buffer_ticks:              4
  bar_close:                 7495.25
  bar_high:                  7496.25
  bar_low:                   7495.25
  atr:                       1.0829
  nearest_high_name:         'onh'
  nearest_high_value:        7496.25
  nearest_high_dist_ticks:   0.0
  nearest_low_name:          'onl'
  nearest_low_value:         7471.0
  nearest_low_dist_ticks:    -97.0
```

### OVERNIGHT_DRIFT — PASS (bias body too strong)

Crafted scenario: bias bar runs 15:30→16:00 CT with open=7480.00, close=7484.25 — body=+4.25pt, above the 3.0pt weak-bullish ceiling. Strategy evaluates at 17:00 CT and stands down.

```
outcome:        PASS
gate_failed:    'bias_body_too_strong'
near_miss:      False
signal_side:    'buy'
reason:         Bias body too strong — +4.25pt, needed <= 3.0pt
                (over by 1.25pt — conviction bullish, not weak bullish)
gate_values:
  ct_minute:                    1020          # 17:00 CT
  bias_open:                    7480.0
  bias_close:                   7484.25
  bias_body_points:             4.25
  min_body_points:              2.0
  weak_body_threshold_points:   3.0
  entry_min:                    1020
  bias_window_start_min:        930           # 15:30
  bias_window_end_min:          960           # 16:00
  day_entered:                  False
```

### OVERNIGHT_DRIFT — NEAR (★ daily slot already consumed)

Crafted scenario: bias body=+2.5pt is in the weak-bullish band (gates pass). The strategy fires at 17:00 CT, then on the very next bar (17:02 CT) re-evaluates — bracket has not yet been exited so position is flat per the harness, but `day.entered=True` blocks re-entry.

```
outcome:        NEAR
gate_failed:    None
near_miss:      True       ★
signal_side:    'buy'
reason:         All gates PASS — daily entry slot already consumed today
gate_values:
  ct_minute:                    1022          # 17:02 CT
  bias_open:                    7480.0
  bias_close:                   7482.5
  bias_body_points:             2.5
  min_body_points:              2.0
  weak_body_threshold_points:   3.0
  entry_min:                    1020
  bias_window_start_min:        930
  bias_window_end_min:          960
  day_entered:                  True
```

### GAP_FILL — PASS (gap too small)

Crafted scenario: prior 15:58 CT close=7488.00, today's 08:30 CT open=7495.20 → gap=+7.20pt, short of the 12.0pt threshold by 4.8pt.

```
outcome:        PASS
gate_failed:    'gap_too_small'
near_miss:      False
signal_side:    None
reason:         Gap too small — +7.20pt, needed |gap| >= 12.0pt (short 4.80pt)
gate_values:
  ct_minute:                    510          # 08:30 CT
  entry_min:                    510
  prior_close:                  7488.0
  bar_open:                     7495.2
  bar_close:                    7495.0
  gap_points:                   7.2
  abs_gap_points:               7.2
  min_gap_points:               12.0
  day_entered:                  False
```

### GAP_FILL — NEAR (★ daily slot already consumed)

Crafted scenario: prior_close=7488.00, today's 08:30 bar opens at 7502.50 → gap=+14.50pt (above 12.0pt threshold, qualifies). First evaluation fires the Signal; the second evaluation in the same minute finds `day.entered=True`.

```
outcome:        NEAR
gate_failed:    None
near_miss:      True       ★
signal_side:    None
reason:         All gates PASS — daily entry slot already consumed today
gate_values:
  ct_minute:                    510
  entry_min:                    510
  prior_close:                  7488.0
  bar_open:                     7502.5
  bar_close:                    7501.5
  gap_points:                   14.5
  abs_gap_points:               14.5
  min_gap_points:               12.0
  day_entered:                  True
```

### GO/NO-GO LEVELS — PASS (GO/NO-GO sep below threshold)

Crafted scenario: 40 warmup bars of low-amplitude drift around 7479, then a test bar inside the 08:00–12:00 CT window. EMA fast/slow are 7479.02 / 7479.018 — separation 0.0065, well below the 0.35 threshold. Even though level proximity is fine (ORL at 7478.50, only 0.8t away), the GO/NO-GO gate fails first.

```
outcome:        PASS
gate_failed:    'sep_ok'
near_miss:      False
signal_side:    None
reason:         GO/NO-GO WAIT — EMA sep 0.01 below threshold 0.35
gate_values:
  signal_raw:               0
  abs_sep:                  0.0065
  sep_thr:                  0.35
  sep_ok:                   False
  vr:                       1.0
  vr_thr:                   0.85
  vr_rising:                False
  vr_ok:                    False
  slope_fast:               0.018889
  slope_slow:               0.012621
  up_aligned:               True
  down_aligned:             False
  ema_fast:                 7479.0244
  ema_slow:                 7479.018
  in_window:                True
  side_checked:             'buy'
  nearest_level_name:       'ORL'
  nearest_level_value:      7478.5
  level_dist_ticks:         0.8
  buffer_ticks:             4
  level_proximity_ok:       True
```

### GO/NO-GO LEVELS — NEAR (★ outside 08-12 CT window)

Crafted scenario: 40 warmup bars of steady uptrend at 14:00 CT (outside the morning window). On the test bar: EMA sep 0.64 (above 0.35), RVOL 1.20 rising (above 0.85), slopes up-aligned, bar.low touches PDL exactly (0 ticks) → every gate of GO/NO-GO + level proximity passes. The only thing blocking entry is the time window.

```
outcome:        NEAR
gate_failed:    None
near_miss:      True       ★
signal_side:    'buy'
reason:         All gates PASS · PDL 0.0t — outside 08-12 CT trading window
gate_values:
  signal_raw:               1
  abs_sep:                  0.6398
  sep_thr:                  0.35
  sep_ok:                   True
  vr:                       1.1994
  vr_thr:                   0.85
  vr_rising:                True
  vr_ok:                    True
  slope_fast:               0.309967
  slope_slow:               0.289183
  up_aligned:               True
  down_aligned:             False
  ema_fast:                 7489.2601
  ema_slow:                 7488.6203
  in_window:                False
  side_checked:             'buy'
  nearest_level_name:       'PDL'
  nearest_level_value:      7480.0
  level_dist_ticks:         0.0
  buffer_ticks:             4
  level_proximity_ok:       True
```

---

## What changed, what didn't

**Changed:**
- `src/acme/strategies/base.py` — added `EvalResult` dataclass + `EvalOutcome` literal; widened Protocol return type to `Signal | EvalResult | None`
- `src/acme/strategies/boundary.py` — flat-path restructured to evaluate all gates before classifying; `_levels is None / exh is None` warmup guard kept as bare `None`
- `src/acme/strategies/overnight_drift.py` — every non-fire branch now returns a typed `EvalResult`
- `src/acme/strategies/gap_fill.py` — every non-fire branch now returns a typed `EvalResult`
- `src/acme/strategies/go_no_go_levels.py` — flat-path restructured (internal-gate failures → PASS; external-gate failures → NEAR); HOLD branches now report opposite-signal-but-no-level-proximity, opposite-signal-but-outside-window, etc.
- `tests/test_boundary.py` — single assertion widened to accept EvalResult in the HOLD case

**Not changed:**
- Trade-firing logic: every Signal that previously fired still fires under identical conditions. No bracket math, no sizing math, no direction policy was modified.
- `Db.log_event` and the conductor's bar loop: unchanged. The new EvalResults are returned but the conductor still ignores them (drops past the `if sig is None: continue` line — Phase 3 will wire the eval_log write).
- The live runner (PID 71130 if still running): doesn't see these changes until the next git pull + restart. Phase 3 must land before any restart, otherwise the conductor will misbehave when it gets an EvalResult.

## Status

Phase 2 complete. EvalResult on the wire from all four strategies; tests green; lint clean; concrete examples captured from real runs. Awaiting explicit go for Phase 3 (conductor write path + charge_pct).
