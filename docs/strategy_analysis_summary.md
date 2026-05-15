# Strategy analysis — summary

Synthesis of Phases 0–3. Data window: 2024-04-01 → 2026-04-30 (25 months, same data that filtered the current three-keeper fleet).

---

## 1. GO/NO-GO standalone

`GoNoGoStrategy` — three-gate filter (separation + volume-ratio + slope-alignment), 1.5/2.5 × ATR bracket, $25 risk per trade. Two configs tested.

| Run                  | Trades  | Net P&L     | Win rate | PF    | Max DD   | Beats SESSION? |
|----------------------|---------|-------------|----------|-------|----------|----------------|
| RUN A — any hour     | 14,801  | −$27,429    | 37.00%   | 0.850 | $27,641  | No (catastrophic) |
| RUN B — 08-12 CT     |  2,148  |  −$1,402    | 38.90%   | 0.950 | $1,908   | No (close, but no) |
| *SESSION baseline*   | *42*    | *+$56*      | *47.62%* | *~1.06* | *n/a* | *(reference)* |

**Verdict — not worth deploying in shadow.** Neither run clears the SESSION baseline on net P&L, win rate, or profit factor. Neither crosses PF ≥ 1.0 in 25 months of data.

**The one thing that would make it better if it falls short:** stack one more high-leverage filter on top of the morning window. The trajectory from RUN A → RUN B (any hour → 08-12 CT) cut trade count 7× while lifting PF from 0.85 to 0.95 and shrinking max DD 14× — *more aggressive filtering produced monotonically better economics across every metric*. At PF 0.95, the residual gap to break-even is roughly 1.3 pp of win rate (the bracket implies 40.2% WR break-even; RUN B is 38.9%). The single most actionable next discriminator, given what the data showed, is something that meaningfully shrinks the morning-window entry set further — see Section 3.

---

## 2. IGNITION-on-PULSE

`IgnitionPulseTestStrategy` — IGNITION wrapper unchanged, PULSE swapped in for GO/NO-GO, entry rule `edge > 0.5`. Same two configurations.

| Run                  | Trades  | Net P&L     | Win rate | PF    | Max DD   |
|----------------------|---------|-------------|----------|-------|----------|
| RUN C — any hour     | 20,175  | −$55,660    | 36.30%   | 0.780 | $55,773  |
| RUN D — 08-12 CT     |  2,102  |  −$3,897    | 36.80%   | 0.850 | $3,945   |

Bonkers-good test required PF ≥ 1.4, win-rate +3 pp, max DD no worse — all three relative to the matching GO/NO-GO config.

| Comparison           | PF criterion        | WR criterion        | Max DD criterion       | Pass? |
|----------------------|---------------------|---------------------|------------------------|-------|
| Any-hour (C vs A)    | 0.78 vs 0.85 ❌     | 36.3% vs 37.0% ❌   | $55,773 vs $27,641 ❌  | **0-for-3** |
| Morning (D vs B)     | 0.85 vs 0.95 ❌     | 36.8% vs 38.9% ❌   | $3,945 vs $1,908 ❌    | **0-for-3** |

**Verdict: go/no-go stands alone, PULSE stays on shelf.**

Not close. PULSE-IGNITION trades 36% more than the bidirectional GO/NO-GO baseline despite being long-only — its `edge > 0.5` threshold is structurally more permissive than GO/NO-GO's three-AND boolean gate — and each marginal trade is on average a loser. The `PulseFeatureEngine` Python port (229 LOC, unit-tested) stays unused.

---

## 3. Recommended next move

**Register nothing from this analysis in shadow. The active fleet (BOUNDARY + OVERNIGHT_DRIFT + GAP_FILL) stands; GO/NO-GO and IGNITION-on-PULSE both fail to clear SESSION, and SESSION itself was already retired for being marginal — so neither candidate clears the floor that's already been judged insufficient.**

### Level-aware morning candidate — is it logical, and what data is needed?

**Yes, it is logical.** Three data points support it:

1. **Filtering on time alone moved the needle.** GO/NO-GO PF improved 0.85 → 0.95 just from restricting to 08-12 CT. The residual gap to break-even is ~1.3 pp of win rate. Stacking *one more* independent discriminator on top of the morning window is the obvious next experiment.
2. **Level proximity is a structurally independent edge.** `BoundaryStrategy` — which uses *only* day-levels (PDH/PDL/ONH/ONL/ORH/ORL) plus an exhaustion pattern, with no EMA/volume features — produced PF ~3.80 in the same 25-month window (per `scripts/register_new_fleet.py`'s docstring). That's PF nearly 3× the bonkers threshold, on data that overlaps with our morning window. Levels carry information that EMA(9/14) + volume don't see.
3. **The two signal sources don't share inputs.** GO/NO-GO and PULSE both lean on EMA(9/14) + SMA(volume, 20). BOUNDARY's levels are computed from raw OHLC over the prior RTH + overnight session — disjoint from EMA-momentum primitives. A combined filter (`GO/NO-GO gates pass AND price within N ticks of a tracked level AND morning window`) is testing a hypothesis the current analysis can't reach: that two structurally independent edges *compound* rather than overlap.

**Data needed: none beyond what already exists.** The infrastructure is plumbed:

- **Bar data:** the existing parquet cache (2024-04-01 → 2026-04-30) is exactly what's needed. Same window as everything above. No re-pull required.
- **Day-levels:** `acme.levels.compute_day_levels()` + `compute_levels_for_all_days(bars_2min)` already exist; the harness already builds the per-trade-date `levels_by_date` dict and passes it via `strategy.set_levels()`. BOUNDARY uses this exact path.
- **Level-proximity helper:** `acme.levels.nearest_level_distance(price, levels, tick_size)` already exists and is what BOUNDARY itself calls.
- **Harness:** `scripts/backtest_new_fleet.py` requires no changes — adding `set_levels()` to a new strategy class makes the harness pass levels through automatically.

The new file would be `src/acme/strategies/go_no_go_levels_strategy.py` — a copy of the existing `go_no_go_strategy.py` that (a) accepts a `level_buffer_ticks` knob, (b) implements `set_levels(DayLevels)` like BOUNDARY does, (c) gates entries by *bar.high or bar.low* being within `level_buffer_ticks` of a tracked level *in addition to* the three GO/NO-GO gates, with the morning window applied. No live runner contact, no registration — same backtest-only test pattern Phase 1 and Phase 3 used.

If that test produces PF ≥ 1.4 with positive net P&L over the 25-month window, it earns a real conversation about shadow deployment. If it doesn't, the broader candidate space (EMA/volume + level proximity in the morning) is also closed and the search needs different primitives — order-flow, regime-conditioning, or HTF alignment.
