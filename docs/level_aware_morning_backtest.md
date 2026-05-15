# Level-aware morning strategy — backtest

Phase 5 deliverable. New test variant `GoNoGoLevelsStrategy` driven through the same harness, same 25-month window.

- **Data window:** 2024-04-01 → 2026-04-30 (368K 2-min bars, 539 trading days)
- **Strategy:** GO/NO-GO three-AND gate + direction-correlated level proximity + 08-12 CT window
- **Levels:** PDH / PDL / ONH / ONL / ORH / ORL, computed per trading-day rollover (same path BOUNDARY uses)
- **Level rule:** LONG signal requires `bar.low` within `level_buffer_ticks` (=4) of any low-side level (PDL, ONL, ORL); SHORT signal requires `bar.high` within 4 ticks of any high-side level (PDH, ONH, ORH)
- **Everything else identical to GoNoGoStrategy:** bidirectional, no min-2-bar opposite-exit, 1.5/2.5 × ATR bracket, $25 risk per trade

---

## RUN E — GO/NO-GO + levels, 08:00-12:00 CT

| Metric                  | Value                       |
|-------------------------|-----------------------------|
| Total trades            | **260**  (134W / 126L)      |
| Date range              | 2024-04-02 → 2026-04-30     |
| Trading days w/ entry   | 180 of 539 (33%)            |
| Trades / day            | **1.44**                    |
| Net P&L                 | **+$1,279.11**              |
| Win rate                | **51.50%**                  |
| Profit factor           | **1.490**                   |
| Avg win                 | $28.91                      |
| Avg loss                | -$20.59                     |
| W/L size ratio          | 1.404                       |
| Max win                 | $38.76                      |
| Max loss                | -$24.99                     |
| Avg P&L / trade         | **+$4.92**                  |
| **Max drawdown**        | **-$138.61**                |
| **Sharpe (annualised)** | **+3.866**                  |

### P&L distribution

```
[-1000, -100):       0
[ -100,  -50):       0
[  -50,  -25):       0
[  -25,  -10):     126   ████████████████████████████████████████████████████████████ ← losses
[  -10,    0):       0
[    0,   10):       0
[   10,   25):      35   ████████████████████████████████████ ← small wins
[   25,   50):      99   ████████████████████████████████████████████████████████████ ← target fills
[   50,  100):       0
[  100, 1000):       0
```

Bracket is doing its job. Loss tail is clean (no trades below -$25, no outliers). Wins concentrate at target.

---

## Bonkers-good test vs RUN B (no-levels morning baseline)

This is the comparison the Phase 4 recommendation called for: does adding level proximity to GO/NO-GO's morning configuration produce materially better results than morning alone?

| Criterion         | RUN B (no levels) | RUN E (with levels) | Delta              | Required | Pass? |
|-------------------|-------------------|---------------------|--------------------|----------|-------|
| Profit factor     | 0.950             | **1.490**           | **+0.540**         | ≥ 1.4    | ✅    |
| Win rate          | 38.90%            | **51.50%**          | **+12.6 pp**       | +3.0 pp  | ✅    |
| Max drawdown      | $1,908            | **$139**            | **$1,769 better**  | ≤        | ✅    |

**3-for-3.** Every criterion clears its threshold, several by wide margins.

### Secondary: PF ≥ 1.0 break-even

RUN E **clears the break-even line at PF 1.49** — the first run in this entire analysis to do so. All four prior runs (A/B/C/D) plus IGNITION's existing backtest baseline (PF 0.85) sat below 1.0; the level filter is the discriminator that moves the strategy from systematically losing to systematically (modestly) winning.

---

## Where the edge actually comes from

Comparing RUN E to RUN B (same window, same bracket, same direction policy, same GO/NO-GO config — *only* the level filter is added):

| Movement              | RUN B → RUN E            | Mechanism                                                                 |
|-----------------------|--------------------------|----------------------------------------------------------------------------|
| Trades                | 2,148 → 260              | **8× fewer trades.** Level proximity rejects ~88% of morning signals.      |
| Win rate              | 38.9% → 51.5%            | **+12.6 pp** — the cut-out trades were disproportionately losers.          |
| Avg win               | $30.11 → $28.91          | Roughly unchanged.                                                         |
| Avg loss              | -$20.21 → -$20.59        | Roughly unchanged.                                                         |
| Max DD                | $1,908 → $139            | **14× shrinkage** — the equity curve is markedly smoother.                 |

The level filter isn't changing trade *quality* (avg win and avg loss barely move). It's changing trade *selection* — rejecting setups where price isn't near a tracked level, which turns out to disproportionately catch the losing cases. The two signal sources (EMA-momentum + raw-OHLC levels) are doing what Phase 4 hypothesised: compounding rather than overlapping.

---

## Caveats worth surfacing

1. **Sample size: 260 trades.** Substantially larger than SESSION's 42 (so statistically interpretable), but ~8× smaller than RUN B's 2,148 (so noise bands are wider). Confidence interval on the 51.5% win rate at n=260 is roughly ±6 pp. The PF 1.49 result is unlikely to be noise — but the precise number is.
2. **Sharpe is computed over active days only** (180 of 539). Days with zero trades are excluded — same convention as Phases 2 and 3, so the comparison is apples-to-apples. Including all 539 days would shrink Sharpe somewhat (more zero-P&L mass).
3. **No out-of-sample test.** RUN E was tuned on the same 25 months that filtered the current fleet. There's no held-out window. Before any shadow deployment, splitting the window — e.g. fit on 2024-04 → 2025-12, validate on 2026-01 → 2026-04 — would be prudent.
4. **Levels-precompute is expensive.** `compute_levels_for_all_days(bars)` took 392 seconds on the full 2-min stream (it iterates the bar list once per trading day). The backtest itself was 3.3 seconds. If this gets re-run frequently, that's the bottleneck to optimise; for now it's a one-shot cost.
5. **The hypothesis tested was one specific level rule** — direction-correlated proximity (BOUNDARY-style). Alternative rules (any-side proximity, opposite-side proximity for fades, momentum-into-level vs reversion-from-level) weren't tested. The 1.49 PF is for *this* rule; sweep-style exploration could find better or worse variants.

---

## Verdict

**Level-aware morning strategy clears every bar this analysis set.** 3-of-3 bonkers test, PF above 1.0 break-even, max DD a fraction of any other run, Sharpe positive. It's the only candidate in this entire five-phase analysis worth a shadow deployment conversation.

Recommended next step (not part of this phase — read-only deliverable ends here): build the out-of-sample split, re-run on the held-out window, and *if* the held-out PF still clears 1.0, register `go_no_go_levels` v1 in the `strategies` table at SHADOW state and let `fleet_runner._build_keeper_instances()` pick it up. That puts it alongside BOUNDARY / OVERNIGHT_DRIFT / GAP_FILL as a fourth shadowed keeper with no live exposure, where PerfTracker can accumulate real promotion-ladder evidence over real bars.

Phase 5 complete. Live runner untouched, no strategies registered, no fleet binding changed. Five new files total across the five phases: three test-variant strategies and four markdown docs.
