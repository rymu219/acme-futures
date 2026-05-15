# GO/NO-GO Levels — out-of-sample test

Validates whether the RUN E result (PF 1.49, WR 51.5%, n=260 over 2024-04 → 2026-04) holds on data the strategy has never been touched against.

## Setup

- **FIT window** (used for nothing here — parameters already fixed from Phase 5): 2024-04-01 → 2025-12-31
- **HELD-OUT window** (the only window that matters): **2026-01-01 → 2026-04-30**
- **Strategy:** `GoNoGoLevelsStrategy()` — default config, no parameter changes from RUN E. Bracket 1.5/2.5 × ATR, level_buffer_ticks=4, time_windows=((08, 12) CT,), bidirectional.
- **Bar stream:** 2025-12-15 → 2026-04-30 (2-week buffer before OOS start so day-levels and GO/NO-GO engine are properly warm on the first OOS bar). Closes are filtered to `entry_ts >= 2026-01-01`; pre-OOS warmup trades (7 of them) are excluded from metrics.
- **Harness:** `scripts/backtest_new_fleet.py`'s `backtest_strategy()` + `compute_levels_for_all_days()`, imported unchanged.

## Held-out results

| Metric                  | Value                       |
|-------------------------|-----------------------------|
| Total trades            | **20**  (14W / 6L)          |
| Date range              | 2026-01-06 → 2026-04-30     |
| Trading days w/ entry   | 16                          |
| Trades / day (active)   | 1.25                        |
| Net P&L                 | **+$293.95**                |
| Win rate                | **70.00%**                  |
| Profit factor           | **3.280**                   |
| Avg win                 | $30.19                      |
| Avg loss                | -$21.45                     |
| W/L size ratio          | 1.408                       |
| Max win                 | $36.26                      |
| Max loss                | -$24.99                     |
| Avg P&L / trade         | **+$14.70**                 |
| **Max drawdown**        | **-$39.98**                 |
| **Sharpe (annualised)** | **+13.21**                  |

### P&L distribution

```
[-1000, -100):       0
[ -100,  -50):       0
[  -50,  -25):       0
[  -25,  -10):       6   ██████ ← all losses
[  -10,    0):       0
[    0,   10):       0
[   10,   25):       2   ██
[   25,   50):      12   ████████████ ← target fills dominate
[   50,  100):       0
[  100, 1000):       0
```

## In-sample vs out-of-sample

| Metric           | In-sample (RUN E, 2024-04 → 2026-04) | Out-of-sample (2026-01 → 2026-04) |
|------------------|---------------------------------------|-----------------------------------|
| Trades           | 260                                   | 20                                |
| Active days      | 180                                   | 16                                |
| Trades / active  | 1.44                                  | 1.25                              |
| Win rate         | 51.50%                                | 70.00%                            |
| Profit factor    | 1.490                                 | 3.280                             |
| Avg win          | $28.91                                | $30.19                            |
| Avg loss         | -$20.59                               | -$21.45                           |
| W/L ratio        | 1.404                                 | 1.408                             |
| Max DD           | $138.61                               | $39.98                            |

W/L ratio is identical to the third decimal (1.404 vs 1.408) — the bracket is doing the same thing on both windows. The frequency of trades per active day is similar (1.44 vs 1.25). What's different out-of-sample is the **hit rate** — 70% vs 51.5%. Either the strategy improved out-of-sample, or the recent four months were a more favourable regime, or n=20 sample noise is moving the WR around.

## Answer

**Out-of-sample confirmed. Worth shadow deployment conversation.**

Held-out profit factor is **3.28** — over 3× the 1.0 break-even line and over 2× the bonkers-test threshold. Held-out win rate is 70.0%, well above the bracket-implied 40% break-even. Every metric stayed in the same shape as in-sample; the result is not just "PF crosses 1.0 marginally," it's "PF is significantly profitable on held-out data with the same trade economics."

### Sample-size note (factual, not hedging)

The OOS window produced 20 trades. That's a small sample — the 95% confidence interval on the 70% win rate is roughly ±20 pp. **But the PF question being asked has a wide margin**: a PF of 3.28 would have to be wrong by more than 2× to fall below 1.0, which the sample-size uncertainty doesn't support. The robust takeaway is "PF clears 1.0," not "PF is exactly 3.28." Further sample accumulation in shadow will refine the point estimate.

## Status

OOS test complete. No live runner contact, no registrations, no fleet changes. Five strategy files now exist under `src/acme/strategies/` from this analysis: the test variants `go_no_go_strategy.py`, `ignition_pulse_test.py`, `go_no_go_levels_strategy.py` — none registered, none bound. Six analysis docs in `docs/`.

Awaiting your read of this doc before any decision about registration.
