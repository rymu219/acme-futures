# 2026-05-05 trading day — analysis

Topstep session: 2026-05-04 17:00 CT → 2026-05-05 14:36 CT (final exit)
Generated: 2026-05-06T01:50:42.673875+00:00

## Headline

- **Trades**: 100
- **Total P&L**: $28.75
- **Win rate**: 39.0% (39W / 61L)
- **Profit factor**: 1.08  (OOS quote-mode baseline 2.21, gate floor 1.50)
- **Expectancy**: $0.29 per trade
- **Direction mix**: {'long': 100}

## Exit-reason mix

| Exit reason | Count | % | Sum P&L | Avg P&L |
|---|---:|---:|---:|---:|
| `opposite_signal` | 88 | 88.0% | $112.15 | $1.27 |
| `session_end` | 6 | 6.0% | $13.30 | $2.22 |
| `stop` | 5 | 5.0% | $-96.00 | $-19.20 |
| `manual_cleanup_2026_05_05` | 1 | 1.0% | $-0.70 | $-0.70 |

OOS baseline opposite_signal share: ~53%.

## Cum_delta at entry — distribution

Filter threshold (quote mode): **-670**. Long entries fire when `cum_delta_in_dir < -670`.

- min: -41,601
- p25: -22,743
- median: -14,702
- p75: -6,105
- max: -1,125
- mean(|cum_delta|): 16,119

Median |cum_delta| at entry is **16119× the filter threshold magnitude**. The filter is firing at extremes far beyond what OOS calibrated for, which means it's barely filtering — virtually any 2-bar reversal with sell-leaning flow passes.

## ATR at entry — distribution

- min: 0.92
- median: 1.94
- max: 5.96
- mean: 2.20

## Bars held — distribution

- min: 1  (= 2m)
- median: 1  (= 2m)
- max: 9  (= 18m)
- mean: 1.9

Bars-held buckets:
| Bars | Trades | Avg P&L |
|---:|---:|---:|
| 1 | 64 | $-4.43 |
| 2-3 | 24 | $3.88 |
| 4-9 | 11 | $19.98 |

## Per-hour pattern (CT)

| Hour CT | Trades | Sum P&L | Win rate |
|---:|---:|---:|---:|
| 00 | 8 | $-10.60 | 38% |
| 01 | 7 | $6.35 | 43% |
| 02 | 5 | $42.75 | 40% |
| 03 | 5 | $-1.00 | 40% |
| 04 | 5 | $10.25 | 60% |
| 05 | 7 | $0.10 | 57% |
| 06 | 7 | $0.10 | 29% |
| 07 | 7 | $-9.90 | 14% |
| 08 | 6 | $-11.70 | 50% |
| 09 | 8 | $-23.10 | 25% |
| 10 | 5 | $-11.00 | 0% |
| 11 | 7 | $3.85 | 43% |
| 12 | 7 | $6.35 | 43% |
| 13 | 6 | $23.30 | 50% |
| 14 | 4 | $-10.30 | 25% |
| 22 | 2 | $7.35 | 100% |
| 23 | 4 | $5.95 | 50% |

## Top 5 winners

| ID | bar_ts CT | bars | exit reason | entry | exit | P&L | MFE atr | MAE atr |
|---:|---|---:|---|---:|---:|---:|---:|---:|
| 168 | 09:20 | 2 | `opposite_signal` | 7271.5 | 7281.0 | $46.80 | 2.52 | 0.04 |
| 128 | 02:56 | 8 | `opposite_signal` | 7246.75 | 7254.75 | $39.30 | 4.27 | 0.22 |
| 153 | 07:02 | 4 | `opposite_signal` | 7257.25 | 7263.0 | $28.05 | 7.40 | 0.62 |
| 148 | 06:18 | 6 | `opposite_signal` | 7250.0 | 7255.75 | $28.05 | 3.95 | 0.12 |
| 173 | 09:52 | 7 | `opposite_signal` | 7276.75 | 7282.25 | $26.80 | 2.60 | 0.14 |

## Top 5 losers

| ID | bar_ts CT | bars | exit reason | entry | exit | P&L | MFE atr | MAE atr |
|---:|---|---:|---|---:|---:|---:|---:|---:|
| 162 | 08:40 | 1 | `stop` | 7272.75 | 7267.5 | $-26.95 | 0.55 | 1.66 |
| 167 | 09:16 | 1 | `stop` | 7275.75 | 7270.5 | $-26.95 | 0.22 | 1.61 |
| 169 | 09:28 | 1 | `opposite_signal` | 7282.25 | 7278.75 | $-18.20 | 0.22 | 0.90 |
| 171 | 09:40 | 1 | `opposite_signal` | 7278.75 | 7275.25 | $-18.20 | 0.32 | 1.47 |
| 166 | 09:06 | 2 | `stop` | 7278.75 | 7275.5 | $-16.95 | 1.44 | 2.95 |

## Give-back analysis

- Trades with MFE/MAE coverage: **100 / 100**
- **Losers that were green at some point (MFE ≥ 1 ATR before going red): 5** (8% of losers). These are the clearest trailing-stop candidates.
- Losers that were +2 ATR or more at some point: **1**.

### Top 5 winners by give-back (MFE − exit P&L, ATR units)
Trades that captured a large move but exited at much less. The bigger the give-back, the more a trailing stop or thesis-complete exit would have helped.

| ID | bar_ts CT | exit reason | exit P&L atr | MFE atr | Give-back atr |
|---:|---|---|---:|---:|---:|
| 153 | 07:02 | `opposite_signal` | 3.54 | 7.40 | **3.85** |
| 116 | 00:54 | `opposite_signal` | 0.21 | 3.82 | **3.61** |
| 143 | 05:30 | `opposite_signal` | 0.90 | 3.80 | **2.89** |
| 161 | 08:24 | `opposite_signal` | 1.07 | 3.53 | **2.46** |
| 127 | 02:30 | `opposite_signal` | 2.19 | 4.38 | **2.19** |

## What this looks like

1. **Filter is functionally inert.** Mean |cum_delta| at entry was ~16119 vs threshold -670 — that's 24× too extreme. The filter only blocks entries when |cum_delta| < 670, but live cum_delta hasn't been near that range all session. Entries are firing on every two-bar reversal that has any sell-leaning flow.
2. **All trades long.** Today was a one-sided session — sell-flow accumulated all day, never built up to the +670 threshold needed for shorts.
3. **Opposite-signal exits at 88%** vs OOS baseline ~53%. Mechanism is intact — most trades complete via reversal, not stop or time-stop.
4. **PF 1.08 vs OOS 2.21** (49% of OOS). Edge is degraded but not negative. Gate floor is 1.50.
5. **Give-back: 5 of 61 losers (8%) were green ≥1 ATR at some point.** Trailing stop at break-even after +1 ATR would have rescued them.