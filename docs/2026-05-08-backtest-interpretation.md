# 2-year backtest — interpretation

Companion to [`2026-05-08-2yr-backtest-results.md`](2026-05-08-2yr-backtest-results.md).
The raw numbers are sobering and I want to be clear about what they do and
do not say.

## Headline

**Every one of the 16 variants lost money over 2 years (Apr 2024 → Apr
2026) on the cached MES bar data, with PF clustered around 0.7–0.9 across
the entire fleet.** The "least-bad" was `v3.1-armor` at -$2,146 over
2 years, but it only fired 482 trades — the survivor of an extremely
restrictive filter, not an edge.

Aggregated by family:

```
v3      n=205,606   net  -$315,674   WR 30%
v3.1    n=103,120   net  -$133,162   WR 26%
v4      n=201,402   net  -$180,383   WR 32%
v5       n=33,751   net   -$31,683   WR 31%
```

The 2-year backtest **does not validate any of the v3-class variants as
having historical edge.**

## What this DOES say

1. **The v3 thesis (cum_delta extreme + 2-bar reversal → mean revert)
   does not survive 2 years of out-of-sample data.** The OOS validation
   that produced PF 2.30 was on a 78-day window. Generalizing from 78
   days to 730 days, the edge disappears.

2. **The v3.1 safeguards do not generalize.** They were derived from the
   2026-05-07 win-loss anatomy. On that one day they would have flipped
   the fleet from -$1,739 to +$321. Over 2 years they produce the same
   negative-expectancy outcome as base v3 — they were over-fit to one
   bad day.

3. **The v4 regime gates do not rescue it.** Skip-mode (gate, overnight,
   vol) lower trade count but PF stays around 0.82 — the gates aren't
   actually picking better trades, just fewer.

4. **The v5 multi-timeframe anchor does not rescue it.** Same PF 0.82,
   same loss profile, lower trade count.

5. **`v3-trail` is the worst.** PF 0.47 over 2 years. The trailing-stop
   variant ratchets stops up and gets repeatedly stopped out at BE on
   trades that would otherwise have closed via opposite_signal at small
   profits. **Trail logic is destroying value at this signal cadence.**

## What this DOES NOT say

The backtest has known fidelity gaps. None of them is large enough to
turn -$315k into +$30k, but they're real and the user should weigh them:

1. **`cum_delta_session` is synthesized, not real.** The live runner
   reconstructs cum_delta from quote ticks. The backtest synthesizes it
   from each bar's close-position-in-range × volume. The OOS validation
   that produced PF 2.30 used real quote-tick data; we don't have that
   in the cache (Databento has it but it's expensive).

   **This is the single biggest unknown.** It's possible the strategy
   has edge with real cum_delta and our synthesis just doesn't capture
   the same distribution. We can't disprove that without buying tick
   data.

2. **No slippage modeled.** Real fills are 1–2 ticks worse per leg.
   At ~67k trades × 2 legs × 1.5 ticks × $1.25/tick, that's another
   ~$250k of cost we're not counting. Real-world losses would be
   bigger, not smaller.

3. **2-min bars aggregated from cached 1-min bars.** Aligned to even
   minutes, trailing partial groups dropped. Should match the live
   runner's bar cadence well.

4. **No daily drawdown / Topstep eval rules applied.** The backtest is
   raw-strategy P&L. Live, you'd hit max-loss limits and stop trading
   on bad days. So real-world numbers might be less negative *not
   because of edge*, but because Topstep would shut the bot off after
   it lost too much on a bad day.

## Reading the headline number honestly

PF 0.82 across the v3 / v4 / v5 fleet means **for every $100 the
strategies make on winners, they lose $122 on losers.** That ratio is
remarkably stable across configurations — it's a property of the signal
class, not the variant tuning. No amount of trail/armor/min2bar/regime-
gate fiddling moves it materially.

That's the diagnosis: **the underlying entry signal lacks edge.**

## What this means for the user's path

Given the user said "I'm running out of time and need profits relatively
quickly," this is a fork in the road:

### Path A — Buy tick data, retest with real cum_delta

Databento tick data on MES for 2 years is $200–500. If we re-run the
backtest with REAL cum_delta and the result flips from PF 0.82 to
something positive, we know the signal works and the synthesis was the
problem. If it stays at 0.82, the signal is broken and synthesis was a
fair proxy.

- **Cost:** ~$200–500 one-time
- **Time:** 1–2 days
- **Outcome:** definitive answer on whether v3 has edge

### Path B — Pivot to a different signal class entirely

Documented edges in the literature:
- **Opening Range Breakout (ORB)** on indices — multiple papers.
- **Volatility breakout** with trend filter — Hurst, Carver.
- **Session-bias trades** at known mean-reversion windows — well-studied.

Replace v3 family entirely. We already have an `acme.strategies` folder
with seed-fleet implementations of EMA cross, ORB, Donchian, BB
mean-reversion, etc. The backtest harness for those is partly built
(`bar_replay.py`). Could probably get one of them validated in 1–2
weeks.

- **Cost:** $0 in money, ~1–2 weeks of work
- **Outcome:** different strategy with different edge characteristics

### Path C — Keep running paper, hope live edge is real

We know live paper has been losing too (this week was -$1k across the
fleet). Continuing to run the existing fleet is unlikely to produce
edge if 2 years of historical replay says it doesn't have any.

This is the path I'd argue **against** unless we have strong reason to
believe the synthesis is wildly off.

## My honest read

**Path A first.** $200 of Databento tick data settles the most important
unknown — does v3 have edge or not? The answer in 1–2 days replaces
weeks of paper-trading uncertainty.

If A says "no edge with real ticks either," go to **Path B** with
conviction. The seed fleet (ORB, EMA cross, etc.) has documented
historical edge from public research and is the right next bet.

If A says "real ticks change the picture," then we know the synthesis
was the gap and we can keep building on v3. Live shadow data over the
next month becomes the validator.

## What this PR ships

- [`src/acme/backtest/v3_replay.py`](src/acme/backtest/v3_replay.py) —
  replay harness for the v3-style engine class, separate from the
  existing `bar_replay.py` (which serves the seed-fleet `Strategy`
  interface).
- [`src/acme/backtest/v3_run.py`](src/acme/backtest/v3_run.py) — CLI
  driver that runs all 16 variants from `acme.runner.VARIANTS` against
  the cached parquet.
- [`docs/2026-05-08-2yr-backtest-results.md`](2026-05-08-2yr-backtest-results.md) —
  the raw numbers.
- This file — interpretation.

The harness is generalizable: any future variant added to
`acme.runner.VARIANTS` automatically gets included in the next backtest
run. ~5 minutes for the full 2-year window across 16 variants.
