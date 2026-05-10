# Backtest report — generated 2026-05-10T02:18:18.178115+00:00

- Window: beginning → end
- 2-min bars processed: 368,007
- Elapsed: 247.3s
- Variants: 16

## Per-variant summary

| variant            |     n |    WR |    PF |        net |     avg |     best |    worst |        MDD |
|---|---|---|---|---|---|---|---|---|
| v3-canon           | 66597 |   31% |  0.82 | $   -62937 | $ -0.95 | $   1108 | $   -252 | $    63296 |
| v3-trail           | 68485 |   19% |  0.47 | $  -184670 | $ -2.70 | $    633 | $   -333 | $   184812 |
| v3-min2bar         | 51968 |   41% |  0.86 | $   -47636 | $ -0.92 | $   1108 | $   -620 | $    47901 |
| v3-armor           |   538 |   22% |  0.33 | $    -3453 | $ -6.42 | $    128 | $   -161 | $     3453 |
| v3-pctile          | 18018 |   32% |  0.83 | $   -16978 | $ -0.94 | $    621 | $   -333 | $    17405 |
| v3.1-canon         | 31839 |   29% |  0.67 | $   -31799 | $ -1.00 | $    331 | $   -223 | $    32021 |
| v3.1-trail         | 32785 |   18% |  0.37 | $   -61354 | $ -1.87 | $    331 | $   -223 | $    61356 |
| v3.1-min2bar       | 30143 |   31% |  0.69 | $   -30065 | $ -1.00 | $    331 | $   -223 | $    30285 |
| v3.1-armor         |   482 |   18% |  0.13 | $    -2146 | $ -4.45 | $     14 | $   -223 | $     2146 |
| v3.1-pctile        |  7871 |   30% |  0.66 | $    -7797 | $ -0.99 | $    118 | $    -49 | $     7840 |
| v4-loose-shorts    | 26262 |   31% |  0.80 | $   -27195 | $ -1.04 | $    621 | $   -333 | $    27551 |
| v4-trend-gate      | 37017 |   31% |  0.83 | $   -32854 | $ -0.89 | $   1108 | $   -200 | $    33304 |
| v4-overnight-bias  | 24598 |   31% |  0.82 | $   -22718 | $ -0.92 | $   1108 | $   -171 | $    23118 |
| v4-vol-regime      | 59043 |   31% |  0.82 | $   -53028 | $ -0.90 | $   1108 | $   -200 | $    53323 |
| v4-trend-flip      | 54482 |   35% |  0.86 | $   -44588 | $ -0.82 | $   1108 | $   -596 | $    45122 |
| v5-mtf-anchor      | 33751 |   31% |  0.82 | $   -31683 | $ -0.94 | $   1108 | $   -200 | $    31819 |

## Per-family rollup

| family |  n_trades |        net |    WR |
|---|---|---|---|
| v3     |    205606 | $  -315674 |   30% |
| v3.1   |    103120 | $  -133162 |   26% |
| v4     |    201402 | $  -180383 |   32% |
| v5     |     33751 | $   -31683 |   31% |

## Headline

- **Best variant:** `v3.1-armor` net $-2146, PF 0.13
- **Worst variant:** `v3-trail` net $-184670

## Caveats

- `cum_delta_session` is synthesized from each bar's close-position-in-range scaled by volume. The live runner uses quote-tick reconstruction; this is a proxy. Cross-variant *comparisons* are reliable; absolute thresholds (e.g. the static -670) may not transfer cleanly between live and backtest.
- 2-min bars are aggregated from cached 1-min Databento bars. Aligned to even minutes, partial trailing groups dropped.
- Slippage is **not** modeled. Entry / exit fills assume bar close; stops fill at the stop level. Real fills will be worse — subtract ~1 tick / leg when interpreting.
