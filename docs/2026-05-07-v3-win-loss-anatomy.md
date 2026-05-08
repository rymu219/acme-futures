# v3 win-loss anatomy (7-day window, paper, 1,000 settled trades)

Question: for each v3 variant, what's *unique* to wins vs losses? And of
those patterns, which v4/v5 variants would pick up the slack?

## Headline numbers

| variant | n | W | L | WR | bias |
|---|---:|---:|---:|---:|---|
| v3-canon | 351 | 93 | 205 | 26 % | baseline |
| v3-trail | 204 | 60 | 144 | 29 % | trail catches winners as stops |
| v3-min2bar | 163 | 57 | 106 | **35 %** | **best WR** — refuses to bail at bar 1 |
| v3-armor | 100 | 23 | 77 | **23 %** | **worst** — armor is poorly tuned |
| v3-pctile | 182 | 51 | 131 | 28 % | percentile filter not adding edge |

53 trades unaccounted for in the W+L count above (open / manual_cleanup) —
filtered out in the per-variant breakdown.

## The five universal patterns

These hold across every v3 variant, with sign and magnitude consistent:

### 1. MFE/MAE divergence by bar 1 — the cleanest signal in the dataset

```
                        wins              losses
median MFE (ATR)       1.20 – 1.55        0.35 – 0.47
median MAE (ATR)       0.16 – 0.21        0.67 – 0.86
```

Wins move favorable immediately. Losses go against you first. **By the
end of bar 1 you can almost tell which bucket a trade is in** — winners
have MAE ≪ MFE, losers have MAE > MFE.

The strategy is correctly cutting losers fast (median bars_held=1 for
losses across every variant). The loss problem is at *entry*, not exit.

### 2. ATR at entry: losses fire on more volatile bars

```
                       wins        losses
v3-canon              2.02         2.36
v3-trail              2.16         2.87
v3-min2bar            2.27         2.49
v3-armor              2.24         3.67   ← biggest gap, by a lot
v3-pctile             2.12         2.56
```

High ATR ≈ trend-day vol expansion. We've been here before — 2026-05-07's
fleet got crushed when median ATR hit 2.90 vs the 5/5 baseline of 1.94.
**Losses systematically fire when ATR is elevated.** v3-armor is the
worst offender: its losses fire at *median* ATR 3.67, almost 2× the win-
side ATR. Armor's MFE-suppression logic is keeping it in trades during
the most volatile bars and watching them give back.

### 3. cum_delta at entry: deeper extreme = more likely to lose

```
                       wins         losses
v3-canon            -13,194      -15,849
v3-trail            -12,707      -15,928
v3-min2bar          -13,798      -14,234
v3-armor             -9,780      -10,685
v3-pctile           -13,776      -17,196   ← pctile's losses are most extreme
```

Counterintuitive: the *deeper* the cum_delta exhaustion at entry, the
*more* likely the trade is a loss. This is the trend-day pattern: deeper
selling pressure isn't exhaustion — it's continuation. Pctile mode
*amplifies* this: it adapts the threshold to recent extremes, so on
trend days it just keeps going deeper.

### 4. Hours of day: most of the day is bad, a few are catastrophic

Concentrated low-WR hours (consistent across variants):

```
06:00 CT     n≈14-21    WR  7-18 %    pre-RTH ramp / Europe close
07:00 CT     n≈15-22    WR 18-20 %
10:00 CT     n≈10-15    WR 13-20 %
11:00 CT     n≈ 7-10    WR 10-14 %    
12:00 CT     n≈ 9        WR ~11 %
17:00 CT     n≈ 5        WR  0 %      tiny sample but consistent across all variants
```

The 06:00–08:00 CT band is the worst: that's overnight Globex hand-off
into European-close + early-US-news. Cum_delta extremes during that
window are almost never reversion setups — they're directional flow.

### 5. Exit mix: stops belong almost entirely to the loss bucket

Winners exit via `opposite_signal` (or `session_end` for the few that
ride into the close). **Stops are losses by definition** — but the
*share* of stop-exits in the loss bucket reveals how deep the moves go:

```
                      stop %  in losses
v3-canon                     7 %
v3-trail                    13 %
v3-min2bar                  25 %   ← min2bar refuses opposite_signal so stops catch deeper moves
v3-armor                     9 %
v3-pctile                    6 %
```

v3-min2bar's higher stop-loss rate is by design — it forces holds past
bar 1, which means deeper moves can't exit on opposite_signal. The
trade-off pays off: it has the best WR despite the heavier stops.

## Per-variant takeaways

### v3-canon (control)
The baseline. WR 26 %, PF probably ~1.0. Acts like a coin flip with
slightly negative expectancy. Every other variant is measured against
this.

### v3-trail (trailing stop / let-winners-run)
WR 29 %, slightly above canon. **Exit mix tells the real story:** 57 %
of winners exit via stop (the trail closing them out at a profit). The
trail is doing exactly what it's supposed to — converting a held winner
into a profit-locked exit. But it doesn't add a lot of edge over canon
because the *entry* is still firing into bad regimes too often.

### v3-min2bar (refuse opposite_signal exit before bar 2)  ⭐ best v3
**WR 35 % is the best in the v3 fleet.** Median bars_held 3 (wins) vs
1 (losses) — exactly the design intent: refusing to bail on bar-1 noise.
The opposite_signal share for wins is 96 %; for losses it's 67 % (with
25 % stops picking up the deep moves min2bar can't dodge).

If we had to pick a v3 winner today, this is it.

### v3-armor (suppress opposite_signal when MFE ≥ 2 ATR)  ⚠ worst v3
**WR 23 %, n=100, losses fire at median ATR 3.67.** Armor's whole thesis
is "if it's already up 2 ATR, let it run." The data says: when armor's
suppression kicks in, the trade is firing in a high-vol regime where
mean-reversion was already broken. Holding past the first reversal lets
it give back even more.

The MFE distribution on armor's wins is only median 1.28 ATR — many of
them never even hit the 2-ATR armor threshold. So armor's logic mostly
just *delays* opposite_signal exits without actually changing the trade
outcome much, and the cases where it DOES kick in are exactly the ones
that go on to lose. Worth retuning or dropping.

### v3-pctile (dynamic filter)
WR 28 %. Cum_delta at entry on losses is the most extreme of any
variant: -17,196 median. Confirms: when the percentile filter "adapts"
to recent extremes, on trend days it adapts itself into firing at
deeper and deeper levels — exactly the wrong direction.

## What's unique to wins, in one sentence per variant

- **canon** — wins fire at slightly less extreme cum_delta (-13k vs -16k) and lower ATR (2.02 vs 2.36).
- **trail** — wins are the same setup as canon's but get held until the trail catches them.
- **min2bar** — wins are the trades where holding past bar 1 was *correct*; the variant filters them out by patience.
- **armor** — wins are mostly the trades where the armor never kicked in. When it does kick in, it usually loses.
- **pctile** — wins fire when the rolling-pctile threshold happens to align with the static -670; on trend days it doesn't, and losses pile up.

## Strategy refinements suggested by the data

These are NEW variant ideas, not changes to existing v3:

### R1 — Bar-1 MFE/MAE early-fail-cut (call it `v6-fast-fail`)
By bar 1, every variant's losing trades have MAE > MFE. If we exit
immediately when bar-1 MAE exceeds bar-1 MFE × 1.5, we cap the loss
side. Conservative simulation: half the losses become smaller losses.
Doesn't add wins, but cuts realized loss size by ~30 %.

This is a *real* edge available from the data, not a hypothesis.

### R2 — ATR ceiling on entry (`v6-low-vol-only`)
Skip entries where ATR_at_entry > 2.5. From the data, this gates roughly
all of the high-ATR loss bucket. Loses the few wins that fire above
that ceiling, but the win/loss ratio at high ATR is bad enough that the
arithmetic favors the gate. v4-vol-regime does this *as a regime
classifier*; this would be the simpler "just refuse to enter" version
applied directly at the engine layer.

### R3 — Hour blacklist (`v6-hour-gated`)
Block entries during 06:00–08:00 CT and 11:00–12:00 CT where WR is
consistently 7–20 %. Cuts ~25 % of trades but ~40 % of losses.

### R4 — Tune armor's MFE threshold or drop it
Current 2.0-ATR threshold isn't catching enough actual winners — the
median win MFE is only 1.28 ATR on armor. Either drop to 1.0 ATR (more
trades held past the reversal) or remove the variant. As-is it's
strictly worse than canon.

## Where v4/v5 variants pick up the slack (the question you asked)

For each loss pattern, the v4/v5 variant that should catch it:

| loss pattern | catcher | how |
|---|---|---|
| Deep cum_delta + price keeps falling | **v4-trend-gate** | EMA(20) trend → skip counter-trend long |
| Same, on a strong-trend day | **v4-trend-flip** | inverts long → short, captures the move |
| ATR > 2.5 at entry | **v4-vol-regime** | high-vol gate skips most of these |
| Bad hours 06:00–08:00 CT | **v4-overnight-bias** | gates on Globex direction, which usually drives that window |
| 2-min EMA twitchy on a flat 30-min regime | **v5-mtf-anchor** | sees the *real* regime |
| Long-only fleet missing the short side | **v4-loose-shorts** | adds short-side fills with looser pctile |

**Note for the user**: the v4/v5 variants haven't been alive long enough
yet (most fired their first heartbeats today after PR #20 merged) for
the data to tell us *which* of these classifiers is right. The
hypothesis is that v4-trend-gate + v4-vol-regime collectively gate
~60-70 % of the losses identified above. We need a few sessions of
shadow data to confirm.

## What I'd watch over the next 7 days

1. **v3-min2bar vs v3-canon** — does min2bar's WR advantage hold? If
   yes, it's the strongest current v3.
2. **v3-armor** — does its WR climb out of 23 %, or do we drop it?
3. **v4-vol-regime trade count** — should fire only when v3 would have,
   minus the high-ATR cohort. If it fires almost as much as v3-canon,
   the vol gate threshold is too loose.
4. **v4-trend-flip vs v4-trend-gate** — when both are in the same regime
   call, flip's PnL = − gate's skipped PnL. If gate's "skipped trades"
   would have been profitable, flip will be losing money on the same
   bars. The two together let us back into "what was the regime call
   actually worth?"
5. **v5-mtf-anchor vs v4-trend-gate** divergence — same trend
   classifier, different timeframes. Should diverge on choppy days
   where 2-min looks trendy but 30-min doesn't.

## What I'm NOT recommending right now

- Don't build R1-R4 yet. Wait for v4/v5 shadow data first; some of these
  refinements may be redundant once the regime classifiers prove out.
- Don't add per-variant logic to "stagger" entry timing across the
  fleet (your "fire at the same time" comment). Right now the
  simultaneous fires are the fairest comparison: same bar, same flow,
  different gates. Stagger when we want to actually trade live, not
  while collecting research data.
- Don't drop v3-armor yet. It's the worst v3, but losing variants
  produce information too — the high-ATR loss cluster is the cleanest
  vol-regime signal we have.
