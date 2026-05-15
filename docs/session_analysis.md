# Acme Futures — Deployed-State Analysis

Read-only forensic pass. No code changes, no rebuilds, no restarts.

**Date of analysis:** 2026-05-14
**Branch examined:** `claude/peaceful-knuth-f5d3c6` (worktree, master parity)
**Live runner:** `com.acme-futures.fleet-runner` (PID 35991, spawned 2026-05-13 20:17:12 CT, running continuously)

---

## Phase 0 — Deployed state vs. the documented plan

### Headline

**The build diverged from the four-strategy plan.** The originally-documented fleet (IGNITION + SESSION + REGIME + BOUNDARY) was replaced — not renamed — by a new three-keeper fleet (BOUNDARY + OVERNIGHT_DRIFT + GAP_FILL). The old strategies still exist as code on disk but are no longer registered, no longer have rows in the `strategies` table, and are not bound by the live runner.

The dashboard shows the three new keepers as "SHADOW" because the redesigned UI derives the badge from `score`, not from the `state` field. Their actual DB state is **PILOT**. This is the cosmetic ticker bug deferred 2026-05-13.

### Active vs. archived strategy table

Source of truth: `scripts/register_new_fleet.py` (registration), `src/acme/fleet_runner.py:51-74` (instance binding), Supabase `strategies` table snapshot taken 2026-05-14.

| Class                       | File                                      | DB row?     | DB `state`  | Bound by runner? | Dashboard shows | Notes |
|-----------------------------|-------------------------------------------|-------------|-------------|-------------------|-----------------|-------|
| `BoundaryStrategy`          | `src/acme/strategies/boundary.py`         | ✅ yes      | **PILOT**   | ✅ yes (live)     | SHADOW          | Active keeper. Bidirectional. |
| `OvernightDriftStrategy`    | `src/acme/strategies/overnight_drift.py`  | ✅ yes      | **PILOT**   | ✅ yes (live)     | SHADOW          | Active keeper. Long-only. |
| `GapFillStrategy`           | `src/acme/strategies/gap_fill.py`         | ✅ yes      | **PILOT**   | ✅ yes (live)     | SHADOW          | Active keeper. Both directions. |
| `IgnitionStrategy`          | `src/acme/strategies/ignition.py`         | ❌ no       | —           | ❌ no             | not shown       | Code exists, never registered. |
| `SessionStrategy`           | `src/acme/strategies/session.py`          | ❌ no       | —           | ❌ no             | not shown       | Code exists, last shadowed 2026-05-13. |
| `RegimeStrategy`            | `src/acme/strategies/regime.py`           | ❌ no       | —           | ❌ no             | not shown       | Code exists, never bound. Imports `VolRegimeClassifier`. |
| `AntiStrategy`              | `anti.py`                                 | ✅ yes      | SHADOW      | ❌ no             | not shown       | Tier 1, score 0.81. Highest-scored unbound strategy. |
| `TurtleSoupStrategy`        | `turtle_soup.py`                          | ✅ yes      | SHADOW      | ❌ no             | not shown       | Score 0.258. |
| `DonchianStrategy`          | `donchian.py`                             | ✅ yes      | SHADOW      | ❌ no             | not shown       | Score 0.132. |
| `EmaCrossStrategy`          | `ema_cross.py`                            | ✅ yes      | SHADOW      | ❌ no             | not shown       | Demoted PILOT→SHADOW (C-3 evidence). |
| `BbMrStrategy` / `OrbStrategy` / `SupertrendStrategy` / `Turtles2` / `DonchianCalibratedR1` | various | ✅ yes      | SHADOW      | ❌ no             | not shown       | Tier 2/3 explorations, score 0. |

Codebase contains 18 strategy files in `src/acme/strategies/` (excluding `__init__.py`, `base.py`, `params.py`, `pulse_features.py`, `pulse_gates.py`, `go_no_go.py`, `exhaustion.py`, `pulse_features.py` helpers).

### Renamed-vs-replaced check

**OVERNIGHT_DRIFT is NOT a renamed SESSION.** Different load-bearing logic:

| Aspect           | SESSION (`session.py`)                                                                                         | OVERNIGHT_DRIFT (`overnight_drift.py`)                                                  |
|------------------|----------------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------|
| Signal source    | 12h overnight-bias classifier (`classify_overnight_bias` over 360 2-min bars) returning trend_up/down/chop     | Single 30-min bias bar (15:30-16:00 CT). Direction = sign of body, magnitude must be weak |
| Entry trigger    | First bar where bias is clear (and bias-decay guard passes)                                                    | Fires once at 17:00 CT exactly, if bias bar's body is in `(min_body, weak_body_threshold]` |
| Time window      | Default empty (any hour) — `time_windows = ()`                                                                 | Hardcoded 17:00 CT entry, 08:00 CT hard-close (overnight session only)                   |
| Direction        | Long-only by default (`allow_shorts=False`)                                                                    | Long-only by design — only weak-bullish bias is tradeable                                |
| Stop / target    | ATR-multiple bracket: stop=1.5×ATR, target=2.5×ATR (~1.67R)                                                    | Catastrophic 20pt stop, **no target**, 250pt unreachable bracket to satisfy the interface |
| Exit             | Window-close exit (only if windows configured) or bracket stop/target                                          | Bracket stop OR `wants_force_flat` at 08:00 CT                                            |

**GAP_FILL is NOT a renamed REGIME.** Entirely different inputs:

| Aspect           | REGIME (`regime.py`)                                                                                            | GAP_FILL (`gap_fill.py`)                                                                  |
|------------------|-----------------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------|
| Signal source    | `VolRegimeClassifier` (high/normal/low ATR-ratio) + `classify_trend_ema` (trend_up/down/chop)                   | Open-of-08:30 CT bar minus prior 15:58 CT close; sign determines side                      |
| Entry trigger    | Vol must be EXPANSION AND trend not chop. Direction follows trend.                                              | `|gap| >= min_gap_points` (default 12.0). Direction fades the gap.                          |
| Time window      | Any time when vol/trend conditions hold                                                                         | Single bar at 08:30 CT; 13:00 CT hard close                                                |
| Stop / target    | ATR-multiple bracket: stop=1.5×ATR, target=2.5×ATR                                                              | Stop = `stop_gap_multiple × |gap|`. Target = the prior close itself (the actual fill).      |
| Direction        | Long-only by default                                                                                            | Bidirectional (both sides of the gap)                                                      |

**Diverged, not renamed.** The new fleet was built fresh based on 2-year backtest filtering — `fleet_runner.py:13-15` explicitly states the prior three were dropped: "IGNITION / SESSION / REGIME — no edge after 2-year filtering, were emitting heartbeats but generating no realised P&L".

### Why the dashboard says SHADOW

`web/fleet_view.py:598-603` (Ghost Dog redesign, merged in PR #31):

```python
if m["score"] >= LIVE_THRESHOLD:        # 0.65
    badge_label = "LIVE-ELIGIBLE"
elif m["score"] >= PILOT_THRESHOLD:     # 0.55
    badge_label = "PILOT"
else:
    badge_label = "SHADOW"
```

All three keepers have `score=0.0` in the DB (they have no realised P&L yet — PerfTracker hasn't accumulated enough data). Score-derived badge says SHADOW regardless of the `state` column being PILOT. Logic change from the pre-redesign UI.

---

## Phase 1 — SESSION forensic

### Location

- Code: `src/acme/strategies/session.py` (271 lines, 10881 bytes)
- Trade log: `broker_events` rows where `kind='dry_run_close' AND strategy='session'`
- Note: SESSION has been **decommissioned** in the active fleet — no row in `strategies` table, no instance bound by `fleet_runner.py`. The trade history below is from before it was dropped.

### The numbers

Pulled directly from Supabase 2026-05-14, query: `select * from broker_events where kind='dry_run_close' and strategy='session'`.

| Metric                  | Value                                       |
|-------------------------|---------------------------------------------|
| Total trades            | **42**                                      |
| Date range              | 2026-05-12 03:49 UTC → 2026-05-13 05:25 UTC |
| Span                    | ~26 hours / 2 trading days                  |
| Trades / day            | 21.0 avg (range 18–24)                      |
| Net P&L                 | **+$55.86** across 42 trades                |
| Win rate                | 47.6% (20W / 22L, 0 BE)                     |
| Avg win                 | $25.27 (max $30.03)                         |
| Avg loss                | -$20.43 (worst -$24.99)                     |
| Win/loss size ratio     | 1.24                                        |
| Avg hold                | 10 min (range 3–71 min)                     |
| Outcomes                | 22 `stop`, 20 `target`                      |

### P&L distribution

```
[-1000 .. -100 ):    0
[ -100 ..  -50 ):    0
[  -50 ..  -25 ):    0
[  -25 ..  -10 ):   22  ██████████████████████   ← every loss lives here
[  -10 ..    0 ):    0
[    0 ..   10 ):    0
[   10 ..   25 ):    8  ████████
[   25 ..   50 ):   12  ████████████              ← every win lives here
[   50 ..  100 ):    0
[  100 .. 1000 ):    0
```

### Loss-shape verdict

**Not death-by-a-thousand-cuts. Not a few big losers either.** Every loss clusters in a narrow `-$25..-$10` band — that's the ATR-multiple stop firing cleanly. Every win clusters in `+$10..+$50` — that's the ATR-multiple target. The bracket is doing exactly what it's told to do. The problem isn't risk management; it's that the signal generator produces 47.6% winners at a 1.24:1 reward ratio — net edge per trade ≈ $1.33, statistically indistinguishable from zero, dominated by transaction costs and slippage in any larger sample.

The high firing frequency (21/day) is consistent with the empty default time window — SESSION fires whenever the 12h bias classifier returns trend_up and the bias-decay guard passes, which is most of the time.

### Entry condition — quoted from the code

`session.py:171-203`:

```python
# ────── Entry path: we're flat ──────
if not in_window:
    return None
if len(self._buffer) < self.config.bias_min_bars:
    return None

bias = classify_overnight_bias(
    list(self._buffer),
    threshold_atr=self.config.bias_threshold_atr,
    min_bars=self.config.bias_min_bars,
)
if bias == "chop":
    return None

# Bias-decay guard: if the last N bars moved AGAINST the bias by
# more than `bias_decay_atr_thresh` x ATR, suppress.
if self._recent_move_opposes_bias(bias):
    return None

if bias == "trend_up" and self.config.allow_longs:
    side = "buy"
elif bias == "trend_down" and self.config.allow_shorts:
    side = "sell"
else:
    return None
```

**The clock window is NOT the only gate.** SESSION requires:
1. Inside the configured time window (default: any hour, since `time_windows = ()`)
2. Buffer has at least 360 2-min bars (12 hours)
3. `classify_overnight_bias()` returns `trend_up` or `trend_down` (not `chop`)
4. Bias-decay guard passes (recent 15 bars must not have moved against the classifier by ≥ 0.5×ATR)
5. The bias direction matches an allowed direction (default: longs only)

The "real signal trigger" is the **overnight-bias classifier** from `acme.ryan_spec.v4_regime.classify_overnight_bias` — comparing 12h-buffer momentum to ATR. The clock window only narrows where that signal is allowed to fire.

### Exit condition — quoted from the code

`session.py:156-168`:

```python
# ────── Exit path: in position, just left the window ──────
if current_position != 0:
    if was_in_window and not in_window:
        # Window-close exit. Emit opposite-direction Signal so the
        # conductor's flat-first FSM closes.
        side = "sell" if current_position > 0 else "buy"
        return self._build_signal(..., reason="session_window_close")
    return None
```

**Plus** the ATR bracket — defined at signal construction (`session.py:236-242`):

```python
stop_distance   = atr_val * self.config.atr_stop_multiple    # default 1.5
target_distance = atr_val * self.config.atr_target_multiple  # default 2.5
```

With the default empty `time_windows`, the window-close exit never fires (you're always in the window). So in practice the exits are stop / target only — matching the trade record (22 stops, 20 targets, nothing else).

---

## Phase 2 — PULSE / Energy Composite location

### What exists in production Python

| File                                            | Lines | Purpose                                                                              |
|-------------------------------------------------|-------|--------------------------------------------------------------------------------------|
| `src/acme/strategies/pulse_features.py`         | 229   | **PULSE 4-Bar PRO core scoring engine** — full port of the Pine math. EMA fast/slow separation, 4-bar weighted slope, RVOL, logistic probability, projected-move. Unit-tested. |
| `src/acme/strategies/pulse_gates.py`            | ~440  | **PULSE entry-gate stack** — Python port of the Pine "Pro Filters": `VolRegimeClassifier`, `ZoneClassifier`, `PullbackRiskFilter`, `ExhaustionFilter`, `LockoutManager`, `HTFAlignmentEngine`. Each gate exported separately. Unit-tested. |
| `tests/test_pulse_features.py`                  | —     | Unit tests for the feature engine.                                                   |
| `tests/test_pulse_gates.py`                     | —     | Unit tests for the gate stack.                                                        |

This is production code, not a notebook or Pine fragment. From `pulse_features.py:1-2`:

> *PULSE 4-Bar PRO core feature engine — Python port of the Pine math.*
> *Faithful port of the core scoring engine from "PULSE 4-Bar PRO PACK v1.1 [MES]" Pine indicator.*

### What it's wired to

```
$ grep -rln "PulseFeatureEngine\|PulseFeatureConfig\|PulseFeatures(" src/
src/acme/strategies/pulse_features.py
                                          ← self-references only

$ grep -rln "from acme.strategies.pulse" src/
src/acme/strategies/regime.py
src/acme/strategies/pulse_features.py
src/acme/strategies/pulse_gates.py
```

- `PulseFeatureEngine` (the 4-bar scoring model — the core of PULSE) is **not imported by any strategy**. It is fully implemented, unit-tested, and unconsumed.
- The only PULSE-derived class wired to a strategy is `VolRegimeClassifier` (one of six gates in `pulse_gates.py`), imported by `regime.py:32`. **REGIME itself is not in the active fleet** (not registered, not bound by runner), so even this single wiring point is unreachable through the live data path.

### IGNITION exists. Is it blocked on PULSE?

**No, it is not blocked.** `src/acme/strategies/ignition.py:36-37`:

```python
from acme.strategies.go_no_go import GoNoGoConfig, GoNoGoEngine
```

IGNITION composes a **different** entry filter — `go_no_go.py` — which is its own 4-gate Pine port (EMA9/14 separation, volume ratio, slope alignment, time window). Quoting `go_no_go.py:1-3`:

> *GO/NO-GO Box — 4-gate hard-binary entry filter.*
> *Python port of the user-pasted Pine indicator "GO/NO-GO Box (EMA9/14 · Sep · Volume · Time)".*

This is **not the PULSE indicator**. The original plan (per `pulse_gates.py:22`: *"The IGNITION strategy (Phase 2c) composes pulse_features + the gates it cares about"*) was for IGNITION to consume the full PULSE feature stack. The as-built IGNITION uses the simpler `go_no_go` engine instead. PULSE was ported, IGNITION was built — but the two were never connected.

### Where Ignition actually is

- **Code:** fully implemented, unit-tested (`tests/test_ignition.py`).
- **Registered in Supabase `strategies`:** ❌ no row.
- **Bound by `fleet_runner._build_keeper_instances()`:** ❌ no.
- **Active in any live data path:** ❌ no.

IGNITION's blocker is not PULSE — it's that no production runner ever picked it up. It's complete code sitting on the shelf.

---

## Summary — three things that matter most

1. **The four-strategy plan is dead. The live fleet is BOUNDARY + OVERNIGHT_DRIFT + GAP_FILL — all three new strategies, none of them renames of IGNITION/SESSION/REGIME.** IGNITION, SESSION, and REGIME are code on disk with no DB row, no runner binding, and no path to firing. They are decommissioned, not paused. Any document still calling them "the four classic strategies" is stale.

2. **SESSION had no edge — 42 trades, +$55 net, 47.6% win rate, every loss clustered cleanly inside the ATR-stop band.** The brackets executed perfectly; the signal generator (the 12h overnight-bias classifier) just doesn't beat its own transaction costs. The high firing rate (21/day) plus tight P&L distribution makes it the textbook "no-edge but well-risk-managed scalper" — the same diagnosis the runner-archival comment gave it.

3. **PULSE is fully ported to production Python (both the scoring engine and the gate stack), unit-tested, and almost entirely unused. Only one piece — `VolRegimeClassifier` — is wired to anything (`REGIME`), and REGIME itself isn't in the active fleet.** IGNITION was supposed to consume PULSE per the plan; the as-built IGNITION uses the simpler `go_no_go` engine instead. PULSE wasn't the blocker for IGNITION — they were just never wired together. The full 4-bar scoring engine in `pulse_features.py` is dead code today.
