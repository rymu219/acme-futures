# GO/NO-GO standalone — fresh backtest

Phase 2 deliverable. Two configurations of `GoNoGoStrategy` driven through `scripts/backtest_new_fleet.py`'s harness against the full parquet cache.

- **Data window:** 2024-04-01 → 2026-04-30 (same 25 months that filtered the current fleet)
- **Bars:** 736,039 1-min → 368,441 2-min, avg range 2.57 pt
- **Harness:** `stream_2min_bars()` + `backtest_strategy()` imported from `scripts/backtest_new_fleet.py`. No harness modifications. No new files outside `docs/` and the `_strategy.py` we extracted in Phase 1.
- **Strategy class:** `acme.strategies.go_no_go_strategy.GoNoGoStrategy`, defaults match IGNITION except: bidirectional, no min-2-bar opposite exit.

---

## RUN A — default config (bidirectional, any hour)

| Metric                 | Value                          |
|------------------------|--------------------------------|
| Total trades           | **14,801**                     |
| Date range             | 2024-04-01 → 2026-04-30        |
| Trading days           | 535                            |
| Trades / day           | **27.67**                      |
| Net P&L                | **-$27,429.08**                |
| Win rate               | **37.00%** (5,470W / 9,331L)   |
| Profit factor          | **0.850**                      |
| Avg win                | $27.97                         |
| Avg loss               | -$19.34                        |
| W/L size ratio         | 1.447                          |
| Max win                | $38.76                         |
| Max loss               | -$26.22                        |
| Avg P&L / trade        | -$1.85                         |
| **Max drawdown**       | **-$27,640.80**                |
| **Sharpe (annualised)**| **-6.691**                     |

### P&L distribution

```
[-1000, -100):       0
[ -100,  -50):       0
[  -50,  -25):      57   ▏tail (deep stops on big-ATR bars)
[  -25,  -10):   9,274   ████████████████████████████████████████████████████████████ ← all losses
[  -10,    0):       0
[    0,   10):       0
[   10,   25):   1,637   ███████████████████████████████████████████ ← partial fills / small wins
[   25,   50):   3,833   ████████████████████████████████████████████████████████████ ← target fills
[   50,  100):       0
[  100, 1000):       0
```

Loss shape is clean: ATR-multiple stop firing at -$10 to -$25 with 57 deep-tail stops on bars where ATR was unusually large. Wins concentrate at the bracket target. Bracket math is mechanical and correct.

### SESSION baseline comparison — RUN A

| Metric             | SESSION baseline           | RUN A                  | Delta              |
|--------------------|----------------------------|------------------------|--------------------|
| Trades             | 42                         | 14,801                 | +352× sample       |
| Net P&L            | **+$55.86**                | **-$27,429.08**        | **-$27,485**       |
| Win rate           | 47.62%                     | 37.00%                 | **-10.6 pp**       |
| Profit factor      | ~1.06                      | 0.850                  | -0.21              |
| W/L size ratio     | 1.24                       | 1.45                   | +0.21 (richer wins)|

**Verdict — RUN A: does it beat the SESSION baseline?**

**No. Worse on every dimension that matters.** Net P&L is catastrophically negative (-$27K vs SESSION's +$55), win rate trails by 10.6 percentage points, and profit factor is well under 1.0. The W/L size ratio is actually *better* than SESSION's (1.45 vs 1.24) — the bracket pays well when you win — but the hit rate is so poor that the favourable reward doesn't carry it. **RUN A is materially worse than a dead strategy.**

---

## RUN B — 08:00–12:00 CT only (morning window)

`time_windows = ((08:00 CT, 12:00 CT),)`. Same strategy, same bracket, same direction policy. The gate now denies entries outside the morning window.

| Metric                 | Value                          |
|------------------------|--------------------------------|
| Total trades           | **2,148**                      |
| Date range             | 2024-04-01 → 2026-04-30        |
| Trading days           | 448 (some days had 0 entries)  |
| Trades / day           | **4.79**                       |
| Net P&L                | **-$1,402.31**                 |
| Win rate               | **38.90%** (835W / 1,313L)     |
| Profit factor          | **0.950**                      |
| Avg win                | $30.11                         |
| Avg loss               | -$20.21                        |
| W/L size ratio         | 1.490                          |
| Max win                | $38.76                         |
| Max loss               | -$26.22                        |
| Avg P&L / trade        | -$0.65                         |
| **Max drawdown**       | **-$1,907.53**                 |
| **Sharpe (annualised)**| **-0.999**                     |

### P&L distribution

```
[-1000, -100):       0
[ -100,  -50):       0
[  -50,  -25):       1   ▏rare deep-stop event
[  -25,  -10):   1,312   ████████████████████████████████████████████████████████████ ← all losses
[  -10,    0):       0
[    0,   10):       0
[   10,   25):     149   █████████████████████████████ ← small wins
[   25,   50):     686   ████████████████████████████████████████████████████████████ ← target fills
[   50,  100):       0
[  100, 1000):       0
```

Same shape, scaled down ~7×. Slightly improved economics across the board — wins are larger (avg $30 vs $28), reward ratio creeps up (1.49 vs 1.45), and the deep-stop tail effectively disappears (1 trade vs 57). The morning window does what restricting time of day usually does: cuts overnight-thin-market noise.

### SESSION baseline comparison — RUN B

| Metric             | SESSION baseline           | RUN B                  | Delta              |
|--------------------|----------------------------|------------------------|--------------------|
| Trades             | 42                         | 2,148                  | +51× sample        |
| Net P&L            | **+$55.86**                | **-$1,402.31**         | **-$1,458**        |
| Win rate           | 47.62%                     | 38.90%                 | **-8.7 pp**        |
| Profit factor      | ~1.06                      | 0.950                  | -0.11              |
| W/L size ratio     | 1.24                       | 1.49                   | +0.25              |

**Verdict — RUN B: does it beat the SESSION baseline?**

**No.** RUN B is *materially closer* to break-even than RUN A — profit factor crosses from 0.85 → 0.95, max drawdown shrinks from $27,641 → $1,908, Sharpe improves from -6.69 → -1.00 — but it still trails SESSION on net P&L, win rate, and profit factor. The morning window helps; it doesn't fix the underlying win-rate problem. **RUN B is worse than SESSION's baseline, just not by a catastrophic margin.**

For RUN B to break even at its current 1.49 reward ratio, win rate would need to climb to ~40.2% (= 1 / (1 + 1.49)). It's at 38.9% — within striking distance arithmetically, but the strategy has 109 weeks of data and didn't get there.

---

## Both-runs comparison table

| Metric            | RUN A (any hour)        | RUN B (08-12 CT)       | SESSION baseline       |
|-------------------|-------------------------|------------------------|------------------------|
| Trades            | 14,801                  | 2,148                  | 42                     |
| Trades / day      | 27.67                   | 4.79                   | 21.0                   |
| Net P&L           | -$27,429                | -$1,402                | +$56                   |
| Win rate          | 37.00%                  | 38.90%                 | 47.62%                 |
| Profit factor     | 0.850                   | 0.950                  | ~1.06                  |
| W/L ratio         | 1.45                    | 1.49                   | 1.24                   |
| Max drawdown      | $27,641                 | $1,908                 | (n/a — only 42 trades) |
| Sharpe            | -6.69                   | -1.00                  | (n/a — too few days)   |
| **Beats SESSION?**| **No (catastrophic)**   | **No (close, but no)** | baseline               |

---

## What this implies for Phase 3

GO/NO-GO standalone produces ~37% WR with rich brackets (1.45–1.49× reward ratio). That's the floor PULSE has to beat. Phase 3's "bonkers-good" test requires PULSE-on-IGNITION to deliver:

- **Profit factor ≥ 1.4** (vs RUN A's 0.85 and RUN B's 0.95)
- **Win rate +3 pp** (vs RUN A's 37.0% → ≥40.0%, RUN B's 38.9% → ≥41.9%)
- **Max drawdown no worse than go/no-go** (vs RUN A's $27K, RUN B's $1.9K)

The relevant comparison set for Phase 3 is RUN A vs PULSE-any-hour, and RUN B vs PULSE-morning-only.

Phase 2 complete. Awaiting explicit go for Phase 3.
