# v3 multi-variant runtime — forensic performance audit

Read-only audit of `ryan_spec_v3_trades` (paper mode) over the live window
**2026-05-04 07:54 CT → 2026-05-11 20:30 CT** (~7.5 days, 4,752 trades, 16
variants on a shared `LiveBarDeltaBuilder`).

Plan: [`.claude/plans/good-phantom-mode-means-compressed-fox.md`](.claude/plans/good-phantom-mode-means-compressed-fox.md).
Scripts: [`scripts/v3_audit/`](scripts/v3_audit/).

---

## Executive summary (v0 — refines as Sections 1–9 land)

Each answer carries the section that will finalize it.

### 1. Active operational risks? — yes, one (data-integrity, not loss-of-capital)

**10 trade rows in `ryan_spec_v3_trades` are stuck in the "open" state from
2026-05-07 22:08 CT (four days ago)**, even though their variants' heartbeats
report `position_state = "flat"`. Variants affected:
`v3-armor, v3-min2bar, v3-pctile, v3-trail, v4-loose-shorts, v4-overnight-bias,
v4-trend-flip, v4-trend-gate, v4-vol-regime, v5-mtf-anchor`. Entry price
$7,374.50 (one outlier at $7,374.75) — the same anchor referenced in the
prompt.

The runtime crashed or hit a reconciliation gap on 2026-05-07 22:08; the
trade rows never received their exit info. The runtime itself is flat
(only `v4-trend-flip` currently holds a position — see §0.3). **No
phantom losses are accruing in the runtime**, but every read path that
filters `exit_ts IS NULL` will be wrong until these rows are reconciled.
The dashboard's "11+ variants LONG at $7,374.50" framing is reading those
orphaned rows, not actual positions.

There are precedents in the trade log: 5 rows with `exit_reason =
manual_cleanup_2026_05_06_signalr_drop`, 5 with
`manual_cleanup_2026_05_06`, 1 with `manual_cleanup_2026_05_05`. This kind
of reconciliation gap has happened before and is handled by manual
cleanup. Recommend the same here once you've decided whether you want a
real exit price computed against the bars or just a flagging exit reason.

**HALT does not block exits.** [§0.2](#02-halt-and-200--what-the-dashboard-labels-actually-mean)
proves this from the code. The "HALT" badges on the dashboard are
**paper-promotion verdicts**, not position locks.

**The "/200" in `303/200` is the promotion-gate trade threshold**, not a
position cap. The actual MES position cap is 50 contracts
([`src/acme/risk.py:60`](src/acme/risk.py:60)) and no variant is anywhere
near it (1 contract per variant in dry-run).

### 2. Verified edge vs tail-driven? — *[v0 from 7d net P&L only, refine in §5]*

Three variants have positive net P&L in the prompt's 7d snapshot:
`v4-overnight-bias` (+$133.05), `v4-vol-regime` (+$33.15),
`v4-trend-gate` (+$33.75), `v5-mtf-anchor` (+$3.70). All four use the
regime-gating wrapper (`regime_classifier_name` set in
[`src/acme/runner.py:223-280`](src/acme/runner.py:223)). The pattern is
strong: every variant **without** a regime gate or v3.1 safeguards lost
money in this window. Whether the gating represents verified edge or
just smaller exposure to losing clusters will be settled by §5
(top-N-removal) and §6 (cluster outcomes).

### 3. When does v3 make money? — *[v0 from anchor-position analysis only, refine in §2]*

The anchor cluster fired at **2026-05-07 22:08 CT** — Globex overnight,
deep negative `cum_delta = -2,417` for a LONG entry. That entry context
(extreme negative delta at the bottom of an overnight move) is the
canonical setup the static -670 cum_delta filter was designed to fire
on. The fact that 9 variants fired in 3 seconds confirms this. Whether
22:08 CT overnight is *systematically* profitable or just gives big MFE
swings that don't close cleanly waits for §2 (hour-of-day buckets).

### 4. Correlation reality — *[v0 from cluster count, refine in §6]*

**495 cluster events** (≥3 variants, same direction, within 5-minute
window) in 7 days. **131 of those (26%) involved 11+ variants** — i.e.
near-fleet-wide entries. The cluster phenomenon is not occasional: it
dominates how the fleet trades. The architectural root cause is
confirmed: all 16 variants share one `LiveBarDeltaBuilder`
([`src/acme/runner.py:75`](src/acme/runner.py:75)). They differ only in
exit rules, entry filters, and regime gates — never in the underlying
signal source.

Three variants consistently abstain from large clusters:
`v4-trend-gate`, `v4-overnight-bias`, `v4-trend-flip`. The regime classifiers
they wrap are the only mechanism currently filtering shared-signal
correlation.

### 5. 2-bar minimum hold? — *[v0 from variant pair only, refine in §3]*

`v3-canon` (no min-hold, PF 0.91 over 627 trades) vs `v3-min2bar`
(min-hold 2 bars, PF 0.96 over 437 trades). The min-hold variant has a
better PF but fewer trades. Not enough to draw a fleet-wide conclusion;
§3's hold-time bucket analysis will quantify the per-trade impact.

### 6. Surviving insights for IGNITION / SESSION / REGIME / BOUNDARY — *[v0, refine in §9]*

- **SESSION**: hour-of-day signal is unanalyzed yet (§2). The anchor
  cluster was 22:08 CT (overnight). Most retail futures wisdom says
  overnight ≠ tradeable — but the data may say otherwise.
- **REGIME**: v4-overnight-bias and v4-trend-gate together represent two
  independent regime classifiers and *both* are net-positive in 7d. This
  is the strongest pre-audit evidence that REGIME's design has a real
  basis. v4-trend-flip (the spicy inversion variant) lost money
  (-$311.60) — flipping signals is harder than gating them.
- **IGNITION**: nothing in the v3 fleet uses PULSE / ECI. v3.1's
  `bar1_fast_fail` is a proxy for the same idea (cut bar-1 if MFE
  inverts to MAE) but isn't the same mechanism. PULSE/ECI needs to come
  from outside the repo before IGNITION can be built.
- **BOUNDARY**: no level data (PDH / PDL / ONH / ONL / ORH / ORL) is in
  the schema. New infrastructure required as called out in the plan.

---

## §0 Operational triage

### 0.1 DB shape

| Metric | Value |
|---|---|
| `ryan_spec_v3_trades` rows (mode=paper) | **4,752** |
| Time range | 2026-05-04 07:54 CT → 2026-05-11 20:30 CT |
| Distinct `strategy_id` | 16 — all present in `acme.runner.VARIANTS` |
| Distinct `exit_reason` | 10 |
| `runtime_heartbeats` services | 16 — 15 `flat`, 1 `short` |

Exit-reason distribution (paper mode, 7.5d):

| Exit reason | Count | % |
|---|---:|---:|
| `opposite_signal` | 3,976 | 83.7% |
| `stop` | 458 | 9.6% |
| `bar1_fast_fail` | 183 | 3.9% |
| `broker_error` | 53 | 1.1% |
| `session_end` | 44 | 0.9% |
| `(open)` | 26 | 0.5% |
| `manual_cleanup_2026_05_06` | 5 | 0.1% |
| `manual_cleanup_2026_05_06_signalr_drop` | 5 | 0.1% |
| `manual_cleanup_2026_05_05` | 1 | <0.1% |
| `time_stop` | 1 | <0.1% |

Trade volume per variant (7.5d, sorted):

| Variant | Trades |
|---|---:|
| v3-canon | 680 |
| v3-trail | 544 |
| v3-pctile | 478 |
| v3-min2bar | 437 |
| v5-mtf-anchor | 311 |
| v4-vol-regime | 310 |
| v4-overnight-bias | 302 |
| v4-trend-flip | 290 |
| v4-loose-shorts | 276 |
| v4-trend-gate | 251 |
| v3.1-trail | 183 |
| v3.1-canon | 181 |
| v3.1-pctile | 173 |
| v3.1-min2bar | 164 |
| v3-armor | 150 |
| v3.1-armor | 22 |

`v3.1-armor` (22 trades) will carry the `[low_sample]` tag throughout the
audit per the plan's `MIN_TRADES_FOR_INFERENCE = 30` rule.

### 0.2 HALT and "/200" — what the dashboard labels actually mean

The dashboard's `HALT` and `LONG 303/200 ⚠ over cap` labels are
**not** runtime states. Both come from a single Streamlit view at
[`web/ryan_spec_v3_view.py`](web/ryan_spec_v3_view.py) and refer to the
**paper-week promotion gate**, not to live positions.

**HALT** is a `Verdict` from
[`src/acme/ryan_spec/v3_promotion.py:19`](src/acme/ryan_spec/v3_promotion.py:19):

```
Verdict = "PROMOTE_LIVE" | "EXTEND_PAPER" | "HALT" | "INVESTIGATE"
```

The gate logic at `evaluate_paper_promotion()`
([`v3_promotion.py:40`](src/acme/ryan_spec/v3_promotion.py:40)) returns:
- `HALT` if paper PF < 1.5 (the floor)
- `HALT` if avg slippage > 1.5 ticks
- `INVESTIGATE` if opposite-signal exit % < 40%
- `PROMOTE_LIVE` if all gates pass
- `EXTEND_PAPER` if fewer than 200 settled trades

**HALT does not call any exit code path.** It just means "do not promote
to live trading." Paper trades continue normally for HALTed variants.
v3-canon and v3-trail are HALTed in your snapshot because their PFs
(0.91 and 0.79 in your dashboard's 7d window) are below the 1.5 floor.

**The "/200" in `LONG 303/200`** is the **settled-trade promotion gate
threshold** at
[`web/ryan_spec_v3_view.py:328`](web/ryan_spec_v3_view.py:328):

```python
GATE_MIN_SETTLED = 200
```

…rendered by `_gate_progress_html()`
([`web/ryan_spec_v3_view.py:401`](web/ryan_spec_v3_view.py:401)):
> "Compact progress bar: filled track + N/target label. Used to make
> the bare `83/200` text on each panel an at-a-glance promotion-gate
> progress indicator."

So `LONG 303/200` reads as: "currently LONG, has 303 settled trades vs
the 200-trade promotion gate." There is **no cap breach** — 303 is the
trade count, not the contract count. The actual MES position cap is **50
contracts**, set in
[`src/acme/risk.py:60`](src/acme/risk.py:60), and in dry-run every
variant is at 1 contract.

**What actually blocks runtime activity:**
- The risk profile blocks new entries that would exceed 50 MES contracts
  ([`risk.py:132`](src/acme/risk.py:132)).
- The runtime self-halt at
  [`v3_runtime.py:424`](src/acme/ryan_spec/v3_runtime.py:424) sets
  `paused=true` after `max_consecutive_errors` broker failures. This
  blocks **new entries** only; existing positions can still exit.
- Manual `runtime_config.paused = true` via the kill-switch table.

None of these are currently active. The runtime is healthy.

### 0.3 Position-state table — current live state

[`docs/v3_audit/position_state.csv`](docs/v3_audit/position_state.csv)

Only **one** variant has a non-flat heartbeat right now:

| Variant | State | HB age | Open trade | Entry | Stop | ATR | Exit rules |
|---|---|---|---|---|---|---|---|
| `v4-trend-flip` | short | 0m | 2026-05-11 20:32 CT @ 7425.0 | 7425.0 | 7428.52 | 2.34 | opposite_signal, stop |

All 15 other variants report `position_state = flat`. **The dashboard
snapshot in your prompt showed 11+ variants LONG — that snapshot is
reading the orphaned trade rows from 2026-05-07, not current runtime
state.** See §0.4.

### 0.4 The "anchor LONG" — orphaned trade rows, not held positions

[`docs/v3_audit/anchor_position.csv`](docs/v3_audit/anchor_position.csv)

Ten trade rows show `exit_ts IS NULL` with entry price within ±$0.50 of
$7,374.50:

| Variant | Entry time | Entry | Direction | `cum_delta_at_entry` | ATR | Regime gate |
|---|---|---:|---|---:|---:|---|
| v3-min2bar | 2026-05-07 22:04:01 CT | 7374.75 | long | -2,268 | 1.85 | — |
| v3-trail | 2026-05-07 22:08:01 CT | 7374.50 | long | -2,417 | 1.63 | — |
| v4-trend-gate | 2026-05-07 22:08:01 CT | 7374.50 | long | -2,417 | 1.63 | trend_ema |
| v4-loose-shorts | 2026-05-07 22:08:02 CT | 7374.50 | long | -2,417 | 1.63 | — |
| v4-trend-flip | 2026-05-07 22:08:02 CT | 7374.50 | long | -2,417 | 1.63 | trend_ema |
| v4-overnight-bias | 2026-05-07 22:08:02 CT | 7374.50 | long | -2,417 | 1.63 | overnight_bias |
| v5-mtf-anchor | 2026-05-07 22:08:03 CT | 7374.50 | long | -2,417 | 1.63 | higher_tf_alignment |
| v3-armor | 2026-05-07 22:08:03 CT | 7374.50 | long | -2,417 | 1.63 | — |
| v4-vol-regime | 2026-05-07 22:08:03 CT | 7374.50 | long | -2,417 | 1.63 | vol_regime |
| v3-pctile | 2026-05-07 22:08:04 CT | 7374.50 | long | -2,417 | 1.63 | — |

Nine of these landed inside a 3-second window at 22:08:01–04 — a
9-variant cluster firing on the same bar. The same cluster also caught
all four regime-gated variants (`v4-trend-gate`, `v4-trend-flip`,
`v4-overnight-bias`, `v4-vol-regime`, plus `v5-mtf-anchor`) — meaning
on 2026-05-07 22:08 CT, every regime classifier read the regime as
**aligned with the LONG entry**. That's plausible: deep negative
cum_delta during a strong-down overnight can read as a "trend-down
exhaustion" → fade signal that all four classifiers agreed with.

Notable absentees from the cluster:
- `v3-canon` — the static -670 filter would have fired here too, so its
  absence suggests it was already in a position from an earlier entry
  (it cannot stack).
- The entire **v3.1-*** family. The v3.1 ATR-ceiling is 2.5 (this trade
  was at ATR 1.63 so the ceiling wouldn't have blocked it), and the
  hour blacklist is (6,7,8,11,12) CT, which doesn't include 22 CT. So
  v3.1 *should have fired*. The fact that it didn't suggests the v3.1
  family wasn't yet running on 2026-05-07 — the migration that adds
  v3.1 variants ran later.

**These rows are not actively held positions.** Heartbeats report `flat`
for all 10 variants. The runtime crashed, restarted, or hit a
reconciliation gap on 2026-05-07 22:08 — the in-memory position state
was reset to flat but the DB rows never received their exit info.

**Recommendation (manual operator action, not auto-fix):** mark these 10
rows with `exit_reason = 'manual_cleanup_2026_05_11_audit'`,
`exit_ts = NULL → some chosen ts`, and either:
- (a) compute `exit_price` from the Databento bars at the crash time
  and write real P&L, or
- (b) set `exit_price = entry_price`, `pnl_dollars = 0` (treat as
  voided trades), per the pattern in the existing
  `manual_cleanup_*` rows.

I am not executing this cleanup — it's a write to production data and
the plan says read-only until you say go.

### 0.5 `bar1_fast_fail` — definition and frequency

[`docs/v3_audit/bar1_fast_fail.csv`](docs/v3_audit/bar1_fast_fail.csv)

Defined at
[`src/acme/ryan_spec/v3_engine.py:343`](src/acme/ryan_spec/v3_engine.py:343):

> Exit a bar-1 position when `MAE > MFE × bar1_fast_fail_mae_mfe_ratio`
> (default ratio 1.5). Opt-in via `enable_bar1_fast_fail`; only the
> v3.1-* variants enable it
> ([`runner.py:166,176,184,192,202`](src/acme/runner.py:166)). Added in
> PR-G after the 2026-05-07 win-loss anatomy showed losses had MAE ≫ MFE
> by bar 1 while wins did not.

Per-variant trigger rate (7d):

| Variant | Enabled | Trades | `bar1_fast_fail` exits | % |
|---|:-:|---:|---:|---:|
| v3.1-armor | yes | 22 | 7 | 31.8% **[low_sample]** |
| v3.1-min2bar | yes | 164 | 42 | 25.6% |
| v3.1-pctile | yes | 173 | 44 | 25.4% |
| v3.1-canon | yes | 181 | 45 | 24.9% |
| v3.1-trail | yes | 183 | 45 | 24.6% |
| (all non-v3.1 variants) | no | — | 0 | 0% |

≈25% trigger rate across the v3.1 family. Whether the cut prevents
losses or kills future winners is a §3 (hold-time) question.

### 0.6 Cluster confirmation

[`docs/v3_audit/cluster_event_log.csv`](docs/v3_audit/cluster_event_log.csv)

Definition: ≥3 distinct `strategy_id` opening positions in the same
direction within a 5-minute window. Non-overlapping windows (each entry
consumed by at most one cluster).

**Last 7 days:**

| Cluster size | Event count | % |
|---|---:|---:|
| 3–5 variants | 253 | 51% |
| 6–10 variants | 111 | 22% |
| **11+ variants** | **131** | **26%** |
| **Total** | **495** | **100%** |

**Target-day cluster events (2026-05-11, after 18:00 CT):** the
dashboard times you cited (19:44, 19:54, 20:00, 20:06 CT) correspond to
clusters at 19:46, 19:56, 20:02, 20:08 CT in `entry_ts` (the 2-minute
bar offset — dashboard reads `bar_ts`, the bar open; trade rows fill at
`bar_ts + ~2min`).

Every evening cluster on 2026-05-11 has the **same 13-variant LONG
signature**:
`v3-armor, v3-canon, v3-min2bar, v3-pctile, v3-trail, v3.1-armor,
v3.1-canon, v3.1-min2bar, v3.1-pctile, v3.1-trail, v4-loose-shorts,
v4-vol-regime, v5-mtf-anchor`.

The **three variants consistently absent**: `v4-trend-gate`,
`v4-overnight-bias`, `v4-trend-flip`. All three wrap the trend / overnight
classifiers. Either they classified today's overnight as DOWN (so they
blocked or flipped the LONG signal) or they had already entered earlier
and couldn't stack.

`v4-trend-flip` did enter SHORT at 20:32 CT today (see §0.3) — confirming
it inverted the same LONG signal the others fired on. This is the
designed-in differentiation working in real time.

| Time (entry_ts CT) | Direction | # variants | Notes |
|---|---|---:|---|
| 2026-05-11 18:22:01 | long | 14 | first evening cluster; includes v4-overnight-bias |
| 2026-05-11 18:30:00 | long | 12 | v4-loose-shorts dropped |
| 2026-05-11 18:40:04 | long | 16 | **whole fleet LONG** |
| 2026-05-11 18:50:01 | long | 16 | whole fleet again |
| 2026-05-11 19:00:00 | long | 14 | trend-flip & trend-gate dropped |
| 2026-05-11 19:08:02 | long | 14 | |
| 2026-05-11 19:16:00 | long | 14 | |
| 2026-05-11 19:26:02 | long | 13 | overnight-bias drops out for the rest of the night |
| **2026-05-11 19:46:02** | **long** | **13** | matches dashboard's "19:44" |
| **2026-05-11 19:56:00** | **long** | **13** | matches dashboard's "19:54" |
| **2026-05-11 20:02:00** | **long** | **13** | matches dashboard's "20:00" |
| **2026-05-11 20:08:02** | **long** | **13** | matches dashboard's "20:06" |
| 2026-05-11 20:32:02 | long | 13 | most recent; same 13 variants |

This is the cluster-failure phenomenon you wanted measured. 13 variants
firing the same direction within 5 seconds, every 6–10 minutes for two
straight hours.

### 0.7 Surfacing rule check

The plan says: "if Section 0 reveals anything actively dangerous, the
audit pauses." Nothing dangerous is happening in the runtime right now:

- HALT does not block exits.
- The runtime self-halt isn't active.
- The 50-contract risk cap isn't near being hit.
- The only currently-held position (v4-trend-flip short @ $7,425) has a
  stop at $7,428.52 — risk is bounded.
- The orphaned trade rows from 2026-05-07 are a data-integrity issue,
  not a loss-of-capital issue (dry-run + heartbeats are flat).

So **§§1–9 can proceed once you approve**.


---

## §1 Per-variant performance

[`docs/v3_audit/variant_summary.csv`](docs/v3_audit/variant_summary.csv)
· [`docs/v3_audit/variant_exit_distribution.csv`](docs/v3_audit/variant_exit_distribution.csv)

Universe: settled paper trades only (open / orphaned rows excluded).
16 variants, 4,675 settled trades,
fleet net P&L **$-2,179.50**, fleet PF **0.88**.

| strategy_id | n_trades | net_pnl | gross_win | gross_loss | profit_factor | win_rate | avg_win | avg_loss | win_loss_ratio | max_win | max_loss | max_drawdown_dollars | max_drawdown_pct_of_peak | sharpe_daily_annualized | avg_bars_held | mean_mfe_atr | mean_mae_atr |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| v4-overnight-bias | 301 | $133.05 | $1,190.75 | $1,057.70 | 1.13 | 38.2% | $10.35 | $-5.69 | 1.82 | $61.80 | $-30.70 | $175.45 | 56.9% | 14.18 | 2.38 | 1.02 | 0.65 |
| v4-trend-gate | 250 | $33.75 | $913.25 | $879.50 | 1.04 | 36.0% | $10.15 | $-5.50 | 1.85 | $61.80 | $-30.70 | $165.40 | 106.9% | 4.46 | 2.34 | 1.00 | 0.60 |
| v4-vol-regime | 309 | $22.45 | $1,144.50 | $1,122.05 | 1.02 | 37.2% | $9.95 | $-5.78 | 1.72 | $61.80 | $-30.70 | $199.65 | 89.9% | 2.10 | 2.27 | 0.99 | 0.64 |
| v5-mtf-anchor | 310 | $-7.00 | $1,125.90 | $1,132.90 | 0.99 | 36.5% | $9.96 | $-5.75 | 1.73 | $61.80 | $-30.70 | $199.65 | 103.6% | -0.57 | 2.23 | 0.97 | 0.63 |
| v3.1-armor [low_sample] | 20 | $-62.75 | $14.70 | $77.45 | 0.19 | 20.0% | $3.68 | $-4.84 | 0.76 | $9.30 | $-19.45 | $73.85 | 665.3% | -13.30 | 1.75 | 0.54 | 0.70 |
| v3.1-min2bar | 163 | $-65.35 | $419.90 | $485.25 | 0.87 | 41.7% | $6.17 | $-5.11 | 1.21 | $53.05 | $-30.70 | $177.30 | 158.4% | -2.53 | 2.68 | 1.01 | 0.71 |
| v3.1-trail | 183 | $-89.35 | $398.90 | $488.25 | 0.82 | 39.9% | $5.46 | $-4.44 | 1.23 | $55.55 | $-30.70 | $181.20 | 197.3% | -4.26 | 1.99 | 0.84 | 0.62 |
| v3-min2bar | 433 | $-104.50 | $2,076.90 | $2,181.40 | 0.95 | 42.3% | $11.35 | $-8.73 | 1.30 | $126.80 | $-78.20 | $430.05 | 189.1% | -1.74 | 2.83 | 1.11 | 0.79 |
| v3.1-pctile | 173 | $-119.85 | $386.75 | $506.60 | 0.76 | 34.7% | $6.45 | $-4.48 | 1.44 | $53.05 | $-30.70 | $205.35 | 240.2% | -4.93 | 2.17 | 0.94 | 0.64 |
| v4-loose-shorts | 274 | $-125.55 | $826.40 | $951.95 | 0.87 | 35.8% | $8.43 | $-5.41 | 1.56 | $49.30 | $-30.70 | $284.90 | 178.8% | -6.26 | 2.09 | 0.95 | 0.63 |
| v3.1-canon | 181 | $-139.20 | $389.65 | $528.85 | 0.74 | 34.8% | $6.18 | $-4.48 | 1.38 | $53.05 | $-30.70 | $217.45 | 277.9% | -5.49 | 2.09 | 0.94 | 0.62 |
| v3-canon | 625 | $-246.40 | $2,377.15 | $2,623.55 | 0.91 | 34.1% | $11.16 | $-6.37 | 1.75 | $126.80 | $-38.20 | $497.20 | 218.5% | -3.97 | 2.06 | 0.94 | 0.63 |
| v4-trend-flip | 289 | $-258.55 | $1,037.65 | $1,296.20 | 0.80 | 33.9% | $10.59 | $-6.79 | 1.56 | $61.80 | $-63.20 | $495.80 | 465.8% | -6.13 | 2.57 | 1.06 | 0.69 |
| v3-pctile | 476 | $-267.10 | $1,699.00 | $1,966.10 | 0.86 | 32.6% | $10.96 | $-6.12 | 1.79 | $126.80 | $-38.20 | $474.90 | 236.6% | -4.63 | 2.15 | 0.96 | 0.64 |
| v3-armor | 145 | $-404.15 | $420.10 | $824.25 | 0.51 | 22.1% | $13.13 | $-7.29 | 1.80 | $204.30 | $-31.95 | $578.60 | 331.7% | -8.65 | 4.52 | 0.95 | 0.71 |
| v3-trail | 543 | $-479.00 | $1,821.15 | $2,300.15 | 0.79 | 35.5% | $9.44 | $-6.57 | 1.44 | $61.80 | $-38.20 | $599.45 | 585.4% | -7.81 | 1.76 | 0.79 | 0.60 |

**Net winners (7d):** v4-overnight-bias, v4-trend-gate, v4-vol-regime
**Net losers:** v5-mtf-anchor, v3.1-armor, v3.1-min2bar, v3.1-trail, v3-min2bar, v3.1-pctile, v4-loose-shorts, v3.1-canon, v3-canon, v4-trend-flip, v3-pctile, v3-armor, v3-trail

**Best PF:** `v4-overnight-bias` at 1.13
(301 trades).
**Worst PF:** `v3.1-armor` at 0.19
(20 trades).

**Fleet exit-reason distribution (settled only):**

- `opposite_signal` — 3,977 (85.1%)
- `stop` — 459 (9.8%)
- `bar1_fast_fail` — 183 (3.9%)
- `session_end` — 44 (0.9%)
- `manual_cleanup_2026_05_06` — 5 (0.1%)
- `manual_cleanup_2026_05_06_signalr_drop` — 5 (0.1%)
- `manual_cleanup_2026_05_05` — 1 (0.0%)
- `time_stop` — 1 (0.0%)

`opposite_signal` dominates at >80% across nearly every variant — this is
the system's defining behavior. Whether that's a feature or a bug is
the central §3 question (1-bar churn) and §6 question (correlated
exits causing correlated whipsaws).


---

## §2 Time-of-day (US Central)

[`docs/v3_audit/time_buckets.csv`](docs/v3_audit/time_buckets.csv) (variant × hour)
· [`docs/v3_audit/time_buckets_fleet.csv`](docs/v3_audit/time_buckets_fleet.csv) (fleet × hour)

Hours are entry hour (CT) of each settled trade.

### Fleet, by entry hour

| hour | n | net P&L | WR | PF | avg/trade |
| --- | --- | --- | --- | --- | --- |
| 00:00 CT | 276 | $-230.70 | 28.3% | 0.66 | $-0.84 |
| 01:00 CT | 236 | $124.80 | 55.1% | 1.20 | $0.53 |
| 02:00 CT | 321 | $50.30 | 38.6% | 1.07 | $0.16 |
| 03:00 CT | 345 | $818.50 | 44.9% | 1.98 | $2.37 |
| 04:00 CT | 221 | $555.30 | 55.2% | 2.02 | $2.51 |
| 05:00 CT | 226 | $115.55 | 34.5% | 1.21 | $0.51 |
| 06:00 CT | 161 | $-421.45 | 31.1% | 0.44 | $-2.62 |
| 07:00 CT | 184 | $171.20 | 33.2% | 1.14 | $0.93 |
| 08:00 CT | 126 | $395.55 | 29.4% | 1.57 | $3.14 |
| 09:00 CT | 165 | $583.80 | 56.4% | 1.73 | $3.54 |
| 10:00 CT | 239 | $-35.10 | 42.7% | 0.97 | $-0.15 |
| 11:00 CT | 180 | $-1,067.25 | 22.2% | 0.23 | $-5.93 |
| 12:00 CT | 183 | $-281.85 | 39.9% | 0.74 | $-1.54 |
| 13:00 CT | 281 | $-1,335.45 | 14.2% | 0.30 | $-4.75 |
| 14:00 CT | 223 | $-471.10 | 33.2% | 0.51 | $-2.11 |
| 15:00 CT | 208 | $-1,048.10 | 24.0% | 0.24 | $-5.04 |
| 17:00 CT | 53 | $494.40 | 24.5% | 3.47 | $9.33 |
| 18:00 CT | 93 | $54.90 | 35.5% | 1.30 | $0.59 |
| 19:00 CT | 165 | $-541.75 | 28.5% | 0.31 | $-3.28 |
| 20:00 CT | 116 | $-34.95 | 34.5% | 0.93 | $-0.30 |
| 21:00 CT | 136 | $-140.20 | 27.9% | 0.49 | $-1.03 |
| 22:00 CT | 202 | $297.35 | 55.9% | 2.07 | $1.47 |
| 23:00 CT | 341 | $-139.95 | 25.8% | 0.83 | $-0.41 |

### Best & worst 2-hour windows (fleet-aggregate)

**Best:**
- 03:00–05:00 CT → $+1,373.80
- 08:00–10:00 CT → $+979.35
- 02:00–04:00 CT → $+868.80

**Worst:**
- 13:00–15:00 CT → $-1,806.55
- 12:00–14:00 CT → $-1,617.30
- 14:00–16:00 CT → $-1,519.20

### Variant-level best/worst hour (n ≥ 5 trades per hour, variant n ≥ 30)

| variant | best_hour | worst_hour |
| --- | --- | --- |
| v3-armor | 03:00 CT ($+201.50, n=5) | 11:00 CT ($-112.55, n=9) |
| v3-canon | 03:00 CT ($+115.35, n=37) | 11:00 CT ($-162.80, n=29) |
| v3-min2bar | 03:00 CT ($+194.15, n=28) | 11:00 CT ($-179.95, n=16) |
| v3-pctile | 03:00 CT ($+118.30, n=31) | 11:00 CT ($-166.65, n=22) |
| v3-trail | 09:00 CT ($+107.65, n=23) | 13:00 CT ($-145.85, n=28) |
| v3.1-canon | 04:00 CT ($+45.35, n=12) | 13:00 CT ($-77.85, n=13) |
| v3.1-min2bar | 04:00 CT ($+58.55, n=11) | 13:00 CT ($-74.50, n=10) |
| v3.1-pctile | 04:00 CT ($+45.35, n=12) | 13:00 CT ($-77.85, n=13) |
| v3.1-trail | 01:00 CT ($+45.90, n=13) | 13:00 CT ($-72.85, n=13) |
| v4-loose-shorts | 07:00 CT ($+56.60, n=12) | 13:00 CT ($-105.95, n=21) |
| v4-overnight-bias | 07:00 CT ($+121.60, n=12) | 13:00 CT ($-105.95, n=21) |
| v4-trend-flip | 08:00 CT ($+81.35, n=7) | 10:00 CT ($-103.85, n=18) |
| v4-trend-gate | 08:00 CT ($+87.05, n=6) | 15:00 CT ($-70.35, n=13) |
| v4-vol-regime | 08:00 CT ($+81.90, n=8) | 13:00 CT ($-105.95, n=21) |
| v5-mtf-anchor | 08:00 CT ($+81.90, n=8) | 13:00 CT ($-105.95, n=21) |

### SESSION-window assessment

The plan's draft SESSION windows are **8:30–10:00 CT** (morning) and
**13:30–15:00 CT** (afternoon). Mapping those to my 1-hour entry buckets:

| Window | Hours included | n trades | Net P&L |
|---|---|---:|---:|
| Morning RTH (8:30–10:00) | 08, 09, 10 CT | 530 | $+944.25 |
| Afternoon RTH (13:30–15:00) | 13, 14 CT | 504 | $-1,806.55 |
| Full RTH (8–15 CT) | 08–14 CT | 1,397 | $-2,211.40 |
| Overnight (18–07 CT) | 18–23 + 00–06 CT | 2,839 | $+507.70 |

**Interpretation:** the fleet's P&L is concentrated in the overnight
hours (where 84% of `opposite_signal` exits also live, per §1). The
draft SESSION windows captured only a small slice of fleet activity —
the fleet trades almost continuously, and the bulk of its losses
accumulate outside the morning/afternoon RTH windows. This challenges
the SESSION-as-9:30-to-3:00 framing in the original plan. Whether
*positive* edge lives in the morning or afternoon RTH windows
specifically — independent of variant — is the practical question for
the SESSION strategy and is best answered with the per-hour
profit-factor column above.


---

## §3 Hold-time analysis

[`docs/v3_audit/hold_time.csv`](docs/v3_audit/hold_time.csv) (variant + fleet detail)

Buckets: 1, 2, 3, 4–6, 7–10, 11+ bars (each bar = 2 min).

### Fleet, by bars-held bucket

| bars held | n | net P&L | WR | PF | avg/trade |
| --- | --- | --- | --- | --- | --- |
| 1 | 2941 | $-12,458.70 | 18.1% | 0.19 | $-4.24 |
| 2 | 667 | $509.35 | 54.0% | 1.34 | $0.76 |
| 3 | 422 | $1,959.60 | 59.2% | 3.96 | $4.64 |
| 4-6 | 415 | $4,142.00 | 83.4% | 13.02 | $9.98 |
| 7-10 | 154 | $2,538.45 | 94.2% | 36.60 | $16.48 |
| 11+ | 71 | $1,237.80 | 64.8% | 4.36 | $17.43 |

### 1-bar opposite-signal hypothesis

| Cohort | n | Net P&L | WR | PF |
|---|---:|---:|---:|---:|
| 1-bar `opposite_signal` exits | 2,366 | $-8,858.70 | 16.5% | 0.18 |
| ≥2-bar `opposite_signal` exits | 1,617 | $+10,541.85 | 68.3% | 6.14 |

The 1-bar churn hypothesis is **confirmed** at the fleet level.

### Counterfactual: strict 2-bar minimum hold (upper bound)

| Metric | Actual | If 1-bar opp-sig exits skipped |
|---|---:|---:|
| Fleet net P&L | $-2,086.20 | $+6,772.50 |

Upper bound — assumes the bar-2 outcome would have been P&L-neutral.
Real bar-2 outcome depends on what the price did next. A proper
estimate would replay each skipped trade against the bars; the CSV
captures the data needed for that follow-up.

### Variant pair test: v3-canon vs v3-min2bar

`v3-min2bar` is the only variant whose engine enables a strict 2-bar
minimum hold (`min_bars_before_opposite_exit = 2`). v3-canon is the
control.

| Variant | n | Net P&L | PF | WR |
|---|---:|---:|---:|---:|
| v3-canon | 626 | $-230.85 | 0.91 | 34.2% |
| v3-min2bar | 434 | $-88.95 | 0.96 | 42.4% |

The min-hold variant is **better** on net P&L despite
fewer trades. This corroborates the fleet 1-bar finding above.


---

## §4 Direction and instrument

[`docs/v3_audit/direction_split.csv`](docs/v3_audit/direction_split.csv)

Instrument is MES across all variants. Direction (long/short) is the
only axis to split.

### Fleet

| Direction | n | Net P&L | PF |
|---|---:|---:|---:|
| long | 4,638 | $-1,837.35 | 0.90 |
| short | 43 | $-248.85 | 0.40 |
| **total** | **4,681** | **$-2,086.20** | |
| % short | | 0.9% | |

The fleet is **1% short**. The 2026-05-07 post-mortem
noted the static cum-delta filter was structurally long-biased (1,063
longs vs 1 short over 4 days); the v4 asymmetric and regime variants
have moved the fleet partly off that bias.

### Per-variant skew (sorted by % short)

| variant | n longs | long net | n shorts | short net | % short |
| --- | --- | --- | --- | --- | --- |
| v4-trend-flip | 247 | $-0.40 | 42 | $-258.15 | 14.5% |
| v3-pctile | 476 | $-260.85 | 1 | $+9.30 | 0.2% |
| v3-canon | 626 | $-230.85 | 0 | $+0.00 | 0.0% |
| v3-armor | 146 | $-388.60 | 0 | $+0.00 | 0.0% |
| v3-trail | 543 | $-479.00 | 0 | $+0.00 | 0.0% |
| v3.1-armor | 20 | $-62.75 | 0 | $+0.00 | 0.0% |
| v3.1-canon | 181 | $-139.20 | 0 | $+0.00 | 0.0% |
| v3-min2bar | 434 | $-88.95 | 0 | $+0.00 | 0.0% |
| v3.1-min2bar | 163 | $-65.35 | 0 | $+0.00 | 0.0% |
| v3.1-pctile | 173 | $-119.85 | 0 | $+0.00 | 0.0% |
| v4-loose-shorts | 275 | $-110.00 | 0 | $+0.00 | 0.0% |
| v3.1-trail | 183 | $-89.35 | 0 | $+0.00 | 0.0% |
| v4-overnight-bias | 301 | $+133.05 | 0 | $+0.00 | 0.0% |
| v4-trend-gate | 250 | $+33.75 | 0 | $+0.00 | 0.0% |
| v4-vol-regime | 309 | $+22.45 | 0 | $+0.00 | 0.0% |
| v5-mtf-anchor | 311 | $+8.55 | 0 | $+0.00 | 0.0% |

### v4-loose-shorts deep-dive

The "loose shorts" variant uses asymmetric percentile gating (top 12%
short, bottom 5% long) specifically to fix the long-bias problem.

| Direction | n | Net P&L | PF |
|---|---:|---:|---:|
| long | 275 | $-110.00 | 0.88 |
| short | 0 | $+0.00 | 0.00 |

**The asymmetric gate did not fire any shorts in this window.** The top-12%
threshold required cum_delta to spike positive enough to qualify a
short, and over the 7d window that didn't happen on MES. The 2026-05-07
long-bias problem hasn't been fixed by `v4-loose-shorts`; the asymmetric
gate just sits there.

### v4-trend-flip (the only spicy inverter)

Inverts counter-trend entries into following entries via the EMA(20)
trend classifier. Its short trades are the trend-down classification
flipping a LONG signal into a SHORT entry.

| Direction | n | Net P&L |
|---|---:|---:|
| long | 247 | $-0.40 |
| short | 42 | $-258.15 |

The inverter is taking real shorts, but as §1 showed v4-trend-flip lost
money overall in this window. The inversion direction *is* aligned with
the regime call; the *exit policy* (which it inherits from the v3 base)
still suffers the same 1-bar churn problem identified in §3.


---

## §5 Concentration and tail dependence

[`docs/v3_audit/tail_dependence.csv`](docs/v3_audit/tail_dependence.csv)

For each variant: net P&L with extremes removed, plus largest single
trading-day P&L (the metric the Topstep 50K consistency rule cares about
— best single day must stay below $1,500).

| variant | n | net P&L | minus top5 | minus bot5 | top5 / net | max day | tail? |
| --- | --- | --- | --- | --- | --- | --- | --- |
| v4-overnight-bias | 301 | $+133.05 | $-117.20 | $+252.80 | +188% | $+69.65 | yes |
| v4-trend-gate | 250 | $+33.75 | $-206.50 | $+153.50 | +712% | $+29.30 | yes |
| v4-vol-regime | 309 | $+22.45 | $-225.30 | $+144.70 | +1104% | $+56.90 | yes |
| v5-mtf-anchor | 312 | $+2.85 | $-244.90 | $+125.10 | +8693% | $+56.90 | yes |
| v3.1-armor | 20 | $-62.75 | $-76.75 | $-10.50 | -22% | $-4.90 | no |
| v3.1-min2bar | 163 | $-65.35 | $-208.10 | $+33.15 | -218% | $+53.25 | ambiguous |
| v3-min2bar | 434 | $-88.95 | $-437.95 | $+145.80 | -392% | $+171.10 | ambiguous |
| v3.1-trail | 183 | $-89.35 | $-199.60 | $+9.15 | -123% | $+40.25 | ambiguous |
| v4-loose-shorts | 276 | $-115.70 | $-315.95 | $-2.20 | -173% | $+15.45 | no |
| v3.1-pctile | 173 | $-119.85 | $-262.60 | $-21.35 | -119% | $+32.20 | no |
| v3.1-canon | 181 | $-139.20 | $-281.95 | $-40.70 | -103% | $+34.35 | no |
| v3-canon | 627 | $-236.55 | $-591.80 | $-76.80 | -150% | $+109.35 | no |
| v3-pctile | 478 | $-257.25 | $-577.50 | $-97.50 | -124% | $+86.50 | no |
| v4-trend-flip | 289 | $-258.55 | $-531.30 | $-42.55 | -105% | $+36.65 | no |
| v3-armor | 147 | $-394.30 | $-698.30 | $-255.80 | -77% | $+43.15 | no |
| v3-trail | 544 | $-484.70 | $-732.45 | $-324.95 | -51% | $+64.95 | no |

**Tail-driven (positive net depends on top-5 wins):** v4-overnight-bias, v4-trend-gate, v4-vol-regime, v5-mtf-anchor

**Ambiguous (negative net but removing top-5 makes it worse):** v3.1-min2bar, v3-min2bar, v3.1-trail

### Topstep $1,500 consistency-rule check

Best single trading-day per variant (max_day_pnl) is in the table above.
At the fleet level over the 7d window:

| Direction | Day | Net P&L |
|---|---|---:|
| Best fleet day | 2026-05-10 | $+621.80 |
| Worst fleet day | 2026-05-07 | $-1,439.70 |

| Rule | Status |
|---|---|
| Best-day-up < $1,500 (consistency rule) | **PASS** |
| Best-day-down > -$1,500 (informal) | **PASS** |

This is paper P&L for the *entire fleet running simultaneously*; the
real-money 50K rule applies to one strategy. The per-variant max_day
column is the relevant view for sizing the future fleet.


---

## §6 Inter-variant correlation and cluster outcomes

[`docs/v3_audit/correlation_matrix.csv`](docs/v3_audit/correlation_matrix.csv)
· [`docs/v3_audit/cluster_outcomes.csv`](docs/v3_audit/cluster_outcomes.csv)

### Pairwise daily-P&L correlation

Variants with `r ≥ 0.7` are functionally one strategy
for diversification purposes.

**Pairs at or above r = 0.7** (32):

- `v3.1-canon` ↔ `v3.1-pctile` → r = +1.00
- `v3.1-pctile` ↔ `v3.1-trail` → r = +0.99
- `v3.1-canon` ↔ `v3.1-min2bar` → r = +0.99
- `v3.1-min2bar` ↔ `v3.1-pctile` → r = +0.99
- `v3.1-armor` ↔ `v4-loose-shorts` → r = +0.98
- `v3.1-pctile` ↔ `v4-loose-shorts` → r = +0.98
- `v3-canon` ↔ `v3-min2bar` → r = +0.98
- `v3.1-canon` ↔ `v3.1-trail` → r = +0.98
- `v3.1-min2bar` ↔ `v3.1-trail` → r = +0.98
- `v3-canon` ↔ `v3-trail` → r = +0.97
- `v3.1-trail` ↔ `v4-loose-shorts` → r = +0.97
- `v3.1-canon` ↔ `v4-loose-shorts` → r = +0.97
- `v3-armor` ↔ `v3-canon` → r = +0.97
- `v3.1-armor` ↔ `v3.1-trail` → r = +0.96
- `v3.1-armor` ↔ `v3.1-pctile` → r = +0.96
- `v3-armor` ↔ `v3-trail` → r = +0.96
- `v3-canon` ↔ `v3-pctile` → r = +0.95
- `v3.1-min2bar` ↔ `v4-loose-shorts` → r = +0.95
- `v3-min2bar` ↔ `v3-pctile` → r = +0.95
- `v3.1-armor` ↔ `v3.1-canon` → r = +0.94
- `v3-armor` ↔ `v3-min2bar` → r = +0.93
- `v3-armor` ↔ `v3-pctile` → r = +0.93
- `v4-vol-regime` ↔ `v5-mtf-anchor` → r = +0.92
- `v3-min2bar` ↔ `v3-trail` → r = +0.92
- `v3.1-armor` ↔ `v3.1-min2bar` → r = +0.91
- `v3-pctile` ↔ `v3-trail` → r = +0.91
- `v4-trend-flip` ↔ `v4-trend-gate` → r = +0.85
- `v3.1-min2bar` ↔ `v4-vol-regime` → r = +0.83
- `v3.1-canon` ↔ `v4-vol-regime` → r = +0.79
- `v3.1-trail` ↔ `v4-vol-regime` → r = +0.78
- `v3.1-pctile` ↔ `v4-vol-regime` → r = +0.76
- `v4-trend-gate` ↔ `v5-mtf-anchor` → r = +0.76

### Full matrix

| variant | v3-armor | v3-canon | v3-min2bar | v3-pctile | v3-trail | v3.1-armor | v3.1-canon | v3.1-min2bar | v3.1-pctile | v3.1-trail | v4-loose-shorts | v4-overnight-bias | v4-trend-flip | v4-trend-gate | v4-vol-regime | v5-mtf-anchor |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| v3-armor | +1.00 | +0.97 | +0.93 | +0.93 | +0.96 | +0.08 | -0.17 | -0.23 | -0.13 | -0.04 | -0.10 | +0.05 | -0.23 | -0.39 | -0.18 | -0.19 |
| v3-canon | +0.97 | +1.00 | +0.98 | +0.95 | +0.97 | +0.21 | +0.00 | -0.06 | +0.03 | +0.12 | +0.04 | +0.04 | -0.12 | -0.27 | +0.01 | +0.01 |
| v3-min2bar | +0.93 | +0.98 | +1.00 | +0.95 | +0.92 | +0.20 | +0.00 | -0.05 | +0.04 | +0.12 | +0.04 | +0.10 | -0.16 | -0.27 | +0.02 | -0.00 |
| v3-pctile | +0.93 | +0.95 | +0.95 | +1.00 | +0.91 | +0.41 | +0.17 | +0.12 | +0.22 | +0.31 | +0.25 | +0.30 | -0.37 | -0.46 | +0.08 | -0.04 |
| v3-trail | +0.96 | +0.97 | +0.92 | +0.91 | +1.00 | +0.14 | -0.04 | -0.10 | -0.02 | +0.07 | -0.04 | +0.04 | -0.04 | -0.15 | +0.06 | +0.09 |
| v3.1-armor | +0.08 | +0.21 | +0.20 | +0.41 | +0.14 | +1.00 | +0.94 | +0.91 | +0.96 | +0.96 | +0.98 | +0.44 | -0.28 | -0.32 | +0.60 | +0.35 |
| v3.1-canon | -0.17 | +0.00 | +0.00 | +0.17 | -0.04 | +0.94 | +1.00 | +0.99 | +1.00 | +0.98 | +0.97 | +0.39 | -0.05 | -0.01 | +0.79 | +0.59 |
| v3.1-min2bar | -0.23 | -0.06 | -0.05 | +0.12 | -0.10 | +0.91 | +0.99 | +1.00 | +0.99 | +0.98 | +0.95 | +0.48 | -0.09 | +0.03 | +0.83 | +0.61 |
| v3.1-pctile | -0.13 | +0.03 | +0.04 | +0.22 | -0.02 | +0.96 | +1.00 | +0.99 | +1.00 | +0.99 | +0.98 | +0.46 | -0.15 | -0.09 | +0.76 | +0.53 |
| v3.1-trail | -0.04 | +0.12 | +0.12 | +0.31 | +0.07 | +0.96 | +0.98 | +0.98 | +0.99 | +1.00 | +0.97 | +0.54 | -0.20 | -0.12 | +0.78 | +0.54 |
| v4-loose-shorts | -0.10 | +0.04 | +0.04 | +0.25 | -0.04 | +0.98 | +0.97 | +0.95 | +0.98 | +0.97 | +1.00 | +0.46 | -0.27 | -0.26 | +0.63 | +0.37 |
| v4-overnight-bias | +0.05 | +0.04 | +0.10 | +0.30 | +0.04 | +0.44 | +0.39 | +0.48 | +0.46 | +0.54 | +0.46 | +1.00 | -0.73 | -0.32 | +0.43 | +0.10 |
| v4-trend-flip | -0.23 | -0.12 | -0.16 | -0.37 | -0.04 | -0.28 | -0.05 | -0.09 | -0.15 | -0.20 | -0.27 | -0.73 | +1.00 | +0.85 | +0.23 | +0.58 |
| v4-trend-gate | -0.39 | -0.27 | -0.27 | -0.46 | -0.15 | -0.32 | -0.01 | +0.03 | -0.09 | -0.12 | -0.26 | -0.32 | +0.85 | +1.00 | +0.49 | +0.76 |
| v4-vol-regime | -0.18 | +0.01 | +0.02 | +0.08 | +0.06 | +0.60 | +0.79 | +0.83 | +0.76 | +0.78 | +0.63 | +0.43 | +0.23 | +0.49 | +1.00 | +0.92 |
| v5-mtf-anchor | -0.19 | +0.01 | -0.00 | -0.04 | +0.09 | +0.35 | +0.59 | +0.61 | +0.53 | +0.54 | +0.37 | +0.10 | +0.58 | +0.76 | +0.92 | +1.00 |


### Cluster outcomes — what happens when ≥3 variants fire same direction

The §0.6 cluster log identified 495 non-overlapping cluster events.
This rollup matches each cluster to the trades settled inside its
5-minute window and aggregates P&L.

| cluster size | n clusters | total cluster P&L | avg / cluster | avg matched trades | mean WR |
| --- | --- | --- | --- | --- | --- |
| 11+ | 131 | $-2,317.45 | $-17.69 | 20.2 | 34.5% |
| 3-5 | 253 | $-1,050.10 | $-4.15 | 4.9 | 35.2% |
| 6-10 | 111 | $+21.65 | $+0.20 | 13.3 | 41.0% |

The cluster-size column maps to the hypothesis: the larger the
cluster, the more correlated the bet, the more swing in either
direction.


---

## §7 Drawdown analysis

[`docs/v3_audit/drawdowns.csv`](docs/v3_audit/drawdowns.csv)

Per-variant maximum drawdown in dollar terms, with the trade slice that
caused it. "Ongoing" = the variant has not yet recovered to its prior
peak as of the audit window's end.

| variant | MDD ($) | MDD (% peak) | peak (CT) | trough (CT) | duration (d) | n DD | WR DD | WR life | regime | ongoing |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| v3-trail | $599.45 | 585.4% | 2026-05-06 05:52:01.502015-05:00 | 2026-05-11 20:32:05.544444-05:00 | 5.610000 | 500 | 35.0% | 35.5% | vol-expansion-chop | no |
| v3-armor | $578.60 | 331.7% | 2026-05-06 03:42:01.307479-05:00 | 2026-05-11 20:32:03.634068-05:00 | 5.700000 | 122 | 19.7% | 22.4% | vol-expansion-chop | no |
| v3-canon | $497.20 | 218.5% | 2026-05-06 05:52:00.804452-05:00 | 2026-05-07 18:34:01.641387-05:00 | 1.530000 | 170 | 24.7% | 34.1% | vol-expansion-chop | no |
| v4-trend-flip | $495.80 | 465.8% | 2026-05-08 07:28:04.561327-05:00 | 2026-05-11 08:22:05.509567-05:00 | 3.040000 | 145 | 29.0% | 33.9% | chop | no |
| v3-pctile | $474.90 | 236.6% | 2026-05-06 05:52:01.840448-05:00 | 2026-05-07 18:34:02.003698-05:00 | 1.530000 | 156 | 24.4% | 32.6% | vol-expansion-chop | no |
| v3-min2bar | $430.05 | 189.1% | 2026-05-06 10:00:01.448791-05:00 | 2026-05-07 17:40:02.217481-05:00 | 1.320000 | 108 | 33.3% | 42.4% | vol-expansion-trending-down | no |
| v4-loose-shorts | $284.90 | 178.8% | 2026-05-08 10:46:05.383426-05:00 | 2026-05-11 20:32:05.922132-05:00 | 3.410000 | 183 | 30.6% | 35.9% | chop | no |
| v3.1-canon | $217.45 | 277.9% | 2026-05-08 04:18:02.694764-05:00 | 2026-05-11 20:32:06.721903-05:00 (ongoing) | 3.680000 | 117 | 28.2% | 34.8% | chop | yes |
| v3.1-pctile | $205.35 | 240.2% | 2026-05-10 19:30:06.054203-05:00 | 2026-05-11 20:32:07.101493-05:00 (ongoing) | 1.040000 | 89 | 25.8% | 34.7% | chop | yes |
| v5-mtf-anchor | $199.65 | 103.6% | 2026-05-11 12:42:04.389707-05:00 | 2026-05-11 20:32:06.340031-05:00 | 0.330000 | 38 | 15.8% | 36.5% | trending-down | no |
| v4-vol-regime | $199.65 | 89.9% | 2026-05-11 12:42:05.187361-05:00 | 2026-05-11 20:32:07.482670-05:00 (ongoing) | 0.330000 | 38 | 15.8% | 37.2% | trending-down | yes |
| v3.1-trail | $181.20 | 197.3% | 2026-05-10 19:30:03.990507-05:00 | 2026-05-11 20:32:02.854076-05:00 (ongoing) | 1.040000 | 92 | 29.3% | 39.9% | chop | yes |
| v3.1-min2bar | $177.30 | 158.4% | 2026-05-10 22:40:01.947655-05:00 | 2026-05-11 20:32:04.785507-05:00 (ongoing) | 0.910000 | 65 | 29.2% | 41.7% | chop | yes |
| v4-overnight-bias | $175.45 | 56.9% | 2026-05-11 12:42:05.981725-05:00 | 2026-05-11 19:16:04.957973-05:00 (ongoing) | 0.270000 | 32 | 15.6% | 38.2% | trending-down | yes |
| v4-trend-gate | $165.40 | 106.9% | 2026-05-08 07:28:04.153911-05:00 | 2026-05-11 08:22:04.707298-05:00 | 3.040000 | 123 | 33.3% | 36.0% | chop | no |
| v3.1-armor | $73.85 | 665.3% | 2026-05-07 22:44:04.950916-05:00 | 2026-05-11 20:32:05.160693-05:00 (ongoing) | 3.910000 | 19 | 15.8% | 20.0% | trending-down | yes |

**Variants still in their max-drawdown:** v3.1-canon, v3.1-pctile, v4-vol-regime, v3.1-trail, v3.1-min2bar, v4-overnight-bias, v3.1-armor

**Largest WR collapse during DD:** `v4-overnight-bias` —
lifetime WR 38.2% vs WR during DD
15.6%.

**Most stable WR during DD:** `v3-trail` —
lifetime WR 35.5% vs DD WR
35.0%.

### Regime distribution during max-drawdowns

- `chop` — 7 variants
- `vol-expansion-chop` — 4 variants
- `trending-down` — 4 variants
- `vol-expansion-trending-down` — 1 variants

Regime label is a coarse proxy: derived from average ATR-at-entry and
average (exit - entry) price move across the DD slice. It's not from a
calibrated regime classifier.

**Concentration test:** if all DDs land in the same regime label, the
fleet has a regime-specific weakness that REGIME's switching logic
could address. If DDs span every regime, the entry signal itself
needs fixing, not just regime gating.
