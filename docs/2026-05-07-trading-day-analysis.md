# 2026-05-07 trading day — analysis & new strategy proposals

Topstep session: 2026-05-06 17:00 CT → 2026-05-07 14:55 CT (live runner is in 24-hour shadow per PR #17, so trades fired across the entire 24h window).

## Headline

- **Trades**: 481 (across 5 variants)
- **Total fleet P&L**: **-$1,825.45** — single worst day on record
- **Win rate**: 29% (140W / 343L)
- **Direction mix**: long 480, short **1**
- **Profit factor**: ~0.7 (well below the 1.50 promotion gate)

Per variant:

| variant | n | W | L | WR | PnL | top loss |
|---|---:|---:|---:|---:|---:|---|
| v3-canon | 113 | 30 | 83 | 27% | -$392.85 | -$38.20 stop @ 13:42 |
| v3-trail | 118 | 35 | 83 | 30% | -$377.60 | -$38.20 stop @ 13:42 |
| v3-min2bar | 88 | 33 | 55 | 38% | -$355.35 | **-$78.20 stop @ 12:22** |
| v3-pctile | 111 | 31 | 80 | 28% | -$347.70 | -$38.20 stop @ 13:42 |
| v3-armor | 51 | 9 | 42 | 18% | -$351.95 | -$31.95 opp_signal @ 11:16 |

`v3-armor` only fired 51 entries because it spent most of the day holding a single long that's *still open* as of writing — its day's outcome will swing materially when that closes.

## Hourly fleet P&L (CT)

```
00:00   23 trades  $   -1.10   $    -1.10
01:00   31 trades  $    0.80   $    -0.30
02:00   21 trades  $  279.05   $   278.75   ← peak (+$279)
03:00   20 trades  $  -94.00   $   184.75
04:00   31 trades  $ -130.45   $    54.30
05:00   16 trades  $   46.30   $   100.60
06:00   26 trades  $  -65.70   $    34.90
07:00   27 trades  $   -7.65   $    27.25
08:00   18 trades  $  -82.60   $   -55.35
10:00   24 trades  $ -175.55   $  -230.90
11:00   47 trades  $ -610.40   $  -841.30   ← single worst hour, -$610 / 44 opp_signal exits
12:00   42 trades  $ -311.90   $-1153.20
13:00   27 trades  $ -128.90   $-1282.10
14:00   44 trades  $ -197.05   $-1479.15
15:00   42 trades  $ -236.90   $-1716.05   ← Topstep flatten window (14:55 deadline disabled in shadow)
17:00   14 trades  $ -142.30   $-1858.35   ← Topstep blackout 15:10-17:00 (disabled in shadow)
18:00   28 trades  $   32.90   $-1825.45
```

## Distribution stats (vs 2026-05-05)

| metric | today (5/7) | 5/5 baseline |
|---|---:|---:|
| ATR median | **2.90** | 1.94 |
| ATR mean | **3.43** | 2.20 |
| ATR max | **10.39** | 5.96 |
| `\|cum_delta_at_entry\|` mean | **20,401** | 16,119 |
| Bars held median | 1 | 1 |
| Win rate | 29% | 39% |

**Today was a +50% volatility expansion vs the 5/5 baseline.** Combined with `cum_delta` extremes that were ~25% deeper, the strategy was firing entries at much louder noise levels than its OOS calibration ever saw, in a market that was decidedly *not* mean-reverting.

## Multi-day context

```
day      n trades   total      WR    longs  shorts
Mon 5/4    59      +$13.30      7%     59      0
Tue 5/5   133      -$41.85     34%    133      0
Wed 5/6   391      +$429.30    29%    391      0
Thu 5/7   481      -$1825.45   29%    480      1
```

**The strategy has fired 1,063 longs and 1 short over 4 days.** The "symmetric" 2-bar-reversal + cum_delta filter is structurally biased long because cum_delta drifts negative on MES (retail and hedger flow). The short leg almost never qualifies under the static -670 threshold.

## What actually happened today

The v3 thesis: when `cum_delta_in_dir < -670` (selling pressure exhaustion), bet on a mean-reversion bounce by going long. The OOS data the filter was tuned on showed PF 2.30 across 78 days.

Today, **selling pressure was real continuation, not exhaustion.** Every dip was bought, every bounce was sold. The filter kept firing long signals at deeper and deeper cum_delta extremes (median -18,593, max -46,531) and the longs got run over. 230 of today's 481 trades exited via `opposite_signal` at a loss.

The 11:00 CT hour alone took 44 opposite-signal losses for -$506 — that's 44 long entries each averaging an $11.50 loss. The bot was buying lower lows for an entire hour as price kept falling.

## The "I knew today was a short day" intuition

Quantifiable cues that *could* have been observed before the bleeding started:

1. **Overnight Globex direction (17:00 CT prev → 08:30 CT)** — if red, today's bias is short.
2. **Open vs prior-day close gap** — gap down with no fill in the first 30 min = trend down day.
3. **First 30-min RTH bar direction & range** — large red bar = bias short, low range = chop.
4. **Realized volatility expansion** — first hour ATR > 1.5× recent average = trend day, regardless of direction.
5. **VIX at open** — high VIX days are typically directional, low VIX days are mean-reverting.
6. **Macro calendar** — FOMC, CPI, NFP days are a-priori "directional risk on/off."

Of these, **#1, #2, #3, #4 are derivable from MES bar data alone** (no external feed needed) and could be evaluated by 09:00 CT to set the day's bias.

---

## Proposed new strategies

The goal: **stop trading the same direction on every regime**, and add a short side that actually fires.

### S1 — `v4-trend-gate` (skip-only, conservative)

Daily bias filter on top of v3. Compute a regime tag at 09:00 CT (or rolling) and *only allow* signals aligned with the day's direction. Doesn't add shorts; just refuses counter-trend longs on trend-down days.

- **Regime input**: 30-min EMA(20) slope, OR overnight Globex close vs prior-day RTH close.
- **Action**: if regime = down-trend → block all v3 long signals. If regime = up-trend → block all v3 short signals. If regime = chop → take both.
- **Today's hypothetical PnL**: had this been live at 09:00 CT and the regime been correctly tagged, the fleet would have skipped most of the 11:00–17:00 CT bleeding. **Approx improvement: +$1,400** (back-of-envelope, needs backtest).

This is the lowest-risk addition: it can only *reduce* trade count, never invert direction. Failure mode is "we sit out a profitable mean-reversion day on a misclassified regime" — bounded downside.

### S2 — `v4-trend-flip` (active short, aggressive)

Same regime classifier, but on trend-down days **invert** v3's long signals into shorts. The thesis: when cum_delta hits an extreme in a strong trend, that's confirmation of momentum continuation, not exhaustion.

- **Regime input**: same as S1.
- **Action**: in down-trend → v3 long signal → enter SHORT instead. In up-trend → v3 short signal → enter LONG. In chop → take v3 as-is.
- **Today's hypothetical PnL**: 1st-order inversion of today's losses = **+$1,825 instead of -$1,825** (a $3,650 swing). Real number after slippage and exit-asymmetry would be lower but materially positive.

Bigger upside, bigger downside. Misclassifying a chop day as trend = active wrong-side trades. Should ride S1 as the safer cousin.

### S3 — `v4-overnight-bias`

Pure pre-market signal: at 08:30 CT (RTH open), look at overnight Globex direction. Negative overnight = today's bias = short-only. Positive = long-only. Fire v3 entries gated by today's bias.

- **Regime input**: `(rth_open - prior_rth_close) / atr_daily`. Threshold: ≥ +0.5 = long bias, ≤ -0.5 = short bias, else both.
- **Why interesting**: it's the most mechanizable version of "I knew today was a short day" — the user's intuition presumably came from seeing overnight action / open print.

### S4 — `v4-vol-regime`

ATR-based regime gate. Compute first-hour ATR and compare to a rolling 30-day ATR baseline.

- High vol regime (1st hour ATR > 1.3× baseline) = trend day → either S2's flip behavior, or just go flat.
- Low vol regime (1st hour ATR < 0.8× baseline) = chop day → fire v3 normally.

Today's first-hour ATR mean was **3.43**, ~75% above 5/5 baseline 1.94 → would have been correctly classified high-vol.

### S5 — `v4-symmetric-loosened`

Don't change the thesis at all — just loosen the short-side filter so it actually fires. The static -670 threshold is the same magnitude for both sides, but cum_delta's natural bias means longs get many trigger candidates and shorts almost none.

- **Action**: use a per-direction percentile threshold (already supported by `v3-pctile`!) — bottom 10% for longs, top 10% for shorts. The `v3-pctile` variant could be reconfigured to do this asymmetrically.
- **Lowest-effort change** — no new strategy, just a parameter sweep on the existing one.

---

## Validation plan

The B4 backtest harness isn't built yet (per README phase status). Before any of these go live we need:

1. **B4 — backtest harness on Databento cached data.** The cleanest dependency.
2. **Regime classifier prototype** — implement candidate regime tags (S1's EMA, S3's overnight, S4's ATR) and label every historical day. Sanity-check: the 78-day OOS PF 2.30 cohort should mostly be tagged "chop"; today (5/7) should be tagged "trend". If the classifier doesn't separate them cleanly, it's the wrong classifier.
3. **Replay each new strategy** on the OOS window + the 5 days of paper-trading data. Need PF ≥ 1.5 to qualify per the gate.
4. **Shadow-mode rollout** as new variants alongside v3-canon. Same registry, same supervision chain.

## Recommended starting point

**Implement S1 (`v4-trend-gate`) first.** Lowest blast radius — only ever reduces trade count, never inverts direction. If the regime tagger is a good idea, S1's PF should be visibly higher than v3-canon's on the same window. If S1 doesn't outperform, the regime tagger is broken and S2 is unsafe to ship.

Once S1 is validated, consider S2 (`v4-trend-flip`) — same classifier, more aggressive action.

S5 (`v4-symmetric-loosened`) can ship in parallel because it's purely a parameter change to the existing v3-pctile variant.

## Open questions for Ryan

1. What specifically did you observe pre-market that gave you the "short day" intuition? That's the most important signal to mechanize. (Was it overnight action? A news event? A chart pattern?)
2. Is `v3-armor`'s still-open trade going to be manually flattened, or let it ride to stop?
3. Priority order: regime gate (S1) first, or symmetric filter loosen (S5) first?
4. Are we OK adding 1-2 more variants to the live shadow runner, or do we want to rotate them into v3-armor's slot since it's the worst-performing?
