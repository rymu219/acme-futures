# IGNITION-on-PULSE — fresh backtest

Phase 3 deliverable. Two configurations of `IgnitionPulseTestStrategy` (IGNITION wrapper, PULSE filter swap, `edge > 0.5` entry rule) driven through the same harness, same data, same window as Phase 2.

- **Data window:** 2024-04-01 → 2026-04-30 (25 months, 736K 1-min → 368K 2-min)
- **Strategy class:** `acme.strategies.ignition_pulse_test.IgnitionPulseTestStrategy`
- **Filter:** `PulseFeatureEngine` (`pulse_features.py`) — entry rule per spec: `edge > 0.5 AND p_long > p_short → LONG`; `edge > 0.5 AND p_short > p_long → SHORT (if allowed)`
- **Wrapper identical to IGNITION:** long-only by default, min-2-bar opposite-exit gate, 1.5/2.5 × ATR brackets, $25 risk per trade

---

## RUN C — PULSE-IGNITION default (long-only, any hour)

| Metric                  | Value                       |
|-------------------------|-----------------------------|
| Total trades            | **20,175**  (7,319W / 12,856L) |
| Date range              | 2024-04-01 → 2026-04-30     |
| Trading days            | 535                         |
| Trades / day            | **37.71**                   |
| Net P&L                 | **-$55,660.25**             |
| Win rate                | **36.30%**                  |
| Profit factor           | **0.780**                   |
| Avg win                 | $26.88                      |
| Avg loss                | -$19.63                     |
| W/L size ratio          | 1.369                       |
| Max win                 | $38.76                      |
| Max loss                | -$26.22                     |
| Avg P&L / trade         | -$2.76                      |
| **Max drawdown**        | **-$55,773.00**             |
| **Sharpe (annualised)** | **-13.458**                 |

### P&L distribution

```
[-1000, -100):       0
[ -100,  -50):       0
[  -50,  -25):     198   ▏tail (deep stops on big-ATR bars)
[  -25,  -10):  12,658   ████████████████████████████████████████████████████████████ ← all losses
[  -10,    0):       0
[    0,   10):       0
[   10,   25):   2,486   ████████████████████████████████████████████████████████████ ← small wins
[   25,   50):   4,833   ████████████████████████████████████████████████████████████ ← target fills
[   50,  100):       0
[  100, 1000):       0
```

---

## RUN D — PULSE-IGNITION 08:00–12:00 CT only

| Metric                  | Value                       |
|-------------------------|-----------------------------|
| Total trades            | **2,102**  (773W / 1,329L)  |
| Date range              | 2024-04-01 → 2026-04-30     |
| Trading days            | 421                         |
| Trades / day            | **4.99**                    |
| Net P&L                 | **-$3,897.13**              |
| Win rate                | **36.80%**                  |
| Profit factor           | **0.850**                   |
| Avg win                 | $28.88                      |
| Avg loss                | -$19.73                     |
| W/L size ratio          | 1.464                       |
| Max win                 | $38.76                      |
| Max loss                | -$26.22                     |
| Avg P&L / trade         | -$1.85                      |
| **Max drawdown**        | **-$3,944.87**              |
| **Sharpe (annualised)** | **-3.111**                  |

### P&L distribution

```
[-1000, -100):       0
[ -100,  -50):       0
[  -50,  -25):       3   ▏tail
[  -25,  -10):   1,326   ████████████████████████████████████████████████████████████ ← all losses
[  -10,    0):       0
[    0,   10):       0
[   10,   25):     210   ███████████████████████████████████████ ← small wins
[   25,   50):     563   ████████████████████████████████████████████████████████████ ← target fills
[   50,  100):       0
[  100, 1000):       0
```

---

## Bonkers-good test — PULSE vs GO/NO-GO

Comparison set per Phase 3 spec: RUN C vs Phase 2's RUN A (any hour), RUN D vs Phase 2's RUN B (08-12 CT).

### Any-hour comparison: RUN C vs RUN A

| Metric            | RUN C (PULSE) | RUN A (GoNoGo) | Delta              | Required for "bonkers" | Pass? |
|-------------------|---------------|----------------|--------------------|------------------------|-------|
| Profit factor     | 0.780         | 0.850          | **-0.07**          | ≥ 1.4                  | ❌    |
| Win rate          | 36.30%        | 37.00%         | **-0.7 pp**        | +3.0 pp                | ❌    |
| Max drawdown      | $55,773       | $27,641        | **+$28,132 worse** | ≤ go/no-go             | ❌    |
| Net P&L           | -$55,660      | -$27,429       | -$28,231           | (informational)        | —     |
| Trades            | 20,175        | 14,801         | +36% trade count   | (informational)        | —     |

**All three criteria fail.** PULSE-IGNITION is *worse than* GO/NO-GO standalone on every measure, in both directions. PULSE's threshold (`edge > 0.5`) is more permissive than GO/NO-GO's three-AND gate stack — it fires 36% more trades despite being long-only — and each marginal trade is on average a loser.

### Morning-window comparison: RUN D vs RUN B

| Metric            | RUN D (PULSE) | RUN B (GoNoGo) | Delta              | Required for "bonkers" | Pass? |
|-------------------|---------------|----------------|--------------------|------------------------|-------|
| Profit factor     | 0.850         | 0.950          | **-0.10**          | ≥ 1.4                  | ❌    |
| Win rate          | 36.80%        | 38.90%         | **-2.1 pp**        | +3.0 pp                | ❌    |
| Max drawdown      | $3,945        | $1,908         | **+$2,037 worse**  | ≤ go/no-go             | ❌    |
| Net P&L           | -$3,897       | -$1,402        | -$2,495            | (informational)        | —     |
| Trades            | 2,102         | 2,148          | -2% (similar)      | (informational)        | —     |

**All three criteria fail.** Even in the morning window — the one Phase 2 showed was *structurally* the better window — PULSE underperforms GO/NO-GO across the board.

### Verdict

**go/no-go stands alone, PULSE stays on shelf.**

No hedging. The "bonkers good" test was a 3-of-3 ALL gate; PULSE-IGNITION goes 0-for-3 on the any-hour comparison and 0-for-3 on the morning-window comparison. The result isn't close — every single metric moved in the wrong direction.

---

## Secondary finding: profit-factor break-even

Per your spec, also reporting whether PF clears 1.0 — the real break-even line. **None of the four backtests in Phases 2 and 3 do.**

| Run                              | Profit factor | Clears PF ≥ 1.0? |
|----------------------------------|---------------|-------------------|
| RUN A — GoNoGo, any hour         | 0.850         | ❌                |
| RUN B — GoNoGo, 08-12 CT         | 0.950         | ❌ (close)        |
| RUN C — PULSE-IGNITION, any hour | 0.780         | ❌                |
| RUN D — PULSE-IGNITION, 08-12 CT | 0.850         | ❌                |
| SESSION baseline (reference)     | ~1.06         | ✅ (barely)        |

The closest any of these comes to break-even is RUN B at PF 0.950 — and 25 months of data didn't lift it across the line. The whole filter-design space being tested here (PULSE 4-bar weighted slope + RVOL + tanh-magnitude; GO/NO-GO sep+vr+slope) is producing **sub-break-even profit factors on the 1.5/2.5 × ATR bracket**.

Three structurally different things could change that outcome — none of them are part of this phase:
1. **Different bracket math.** The bracket pays 1.4-1.5× the loss size; needs ~40-42% WR to break even. None of the filters hit 40% WR. A different stop/target ratio would re-anchor the break-even point — but that's a different study.
2. **A meaningfully different signal source.** Both PULSE and GO/NO-GO use the same EMA(9/14) + SMA(volume, 20) primitives. They've both been tested. The candidate space here is exhausted; anything materially better would need different inputs (e.g., order-flow, level-aware, regime-conditional).
3. **Re-filtering existing signals.** Time gating moved PF 0.85 → 0.95 (RUN A → RUN B) for GoNoGo. Compounding filters — time AND regime AND volume floor AND key-level proximity — could potentially get PF over 1.0 by shrinking trade count further. But again, different study.

---

## Status

Phase 3 complete. No live runner contact. No registered strategies modified. No new files outside `docs/` and the two `_test.py` / `_strategy.py` variants under `src/acme/strategies/`.

**Verdict:** wire PULSE → **NO.** PULSE-IGNITION fails the bonkers test 0-for-3 on both comparisons, and neither it nor GO/NO-GO clears profit factor 1.0 on any configuration tested.

Awaiting explicit go for Phase 4 synthesis.
