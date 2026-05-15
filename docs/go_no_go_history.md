# GO/NO-GO — extraction and historical audit

Phase 1 deliverable.

---

## 1. Standalone strategy extracted

New file: `src/acme/strategies/go_no_go_strategy.py` (analysis-only copy).

- Class: `GoNoGoStrategy` (name=`"go_no_go"`, version=`"1"`, timeframe=2-min).
- Composes `GoNoGoEngine` (`go_no_go.py`) + `ATR(4)`. Same primitives as IGNITION.
- Bracket: stop = 1.5 × ATR, target = 2.5 × ATR (1.67R reward). **Identical to IGNITION defaults** per spec.
- Sizing: `dollars_to_contracts(risk=$25, stop_distance, point_value, fee)` — identical to IGNITION.

### Differences from IGNITION (intentional)

| Trait                           | IGNITION                                   | GoNoGoStrategy (standalone)            |
|---------------------------------|--------------------------------------------|----------------------------------------|
| Direction                       | long-only (`allow_shorts=False`)           | **bidirectional** (long + short)       |
| Opposite-signal exit            | min-2-bar gate (audit §3 noise filter)     | **none** — reverse immediately          |
| Time windows                    | empty (any hour) — same default            | empty (any hour) — same default         |
| All other knobs                 | —                                           | identical to `IgnitionConfig` defaults |

Direction was changed from long-only because IGNITION's `allow_shorts=False` comes from v3 audit §4 — a finding about a *different cluster of strategies*, not about GO/NO-GO itself. Carrying it over would bake in a prior decision. Phase 2 RUN A will show whether shorts are worth keeping.

Opposite-signal exit min-2-bar gate was dropped because the user's spec is explicit: "when all 4 gates pass, take the trade in the direction the gates indicate" — that means raw signal, not a smoothed version.

Module is **not** imported by `fleet_runner.py` and **not** registered in the `strategies` table. It exists solely as a backtest target. `uv run ruff check` passes; `uv run python -c "from acme.strategies.go_no_go_strategy import GoNoGoStrategy; GoNoGoStrategy()"` instantiates cleanly.

---

## 2. Baseline reference — IGNITION backtest (prior run)

Source: `docs/backtest_new_fleet/ignition.csv` (8,740 closed-trade rows, produced by the same harness that filtered the current fleet).

| Metric            | Value                                |
|-------------------|--------------------------------------|
| **Trade count**   | **8,740**                            |
| **Net P&L**       | **-$16,409.93**                      |
| **Win rate**      | **36.95%** (3,229W / 5,511L)         |
| **Profit factor** | **0.846**                            |
| Avg win           | $27.83                               |
| Avg loss          | -$19.28                              |
| W/L size ratio    | 1.443                                |
| Date range        | 2024-04-01 → 2026-04-30 (25 months)  |

**This is GO/NO-GO with IGNITION's wrapper.** Not the standalone we just extracted — the wrapper adds long-only direction and min-2-bar opposite-exit. So the standalone (bidirectional, immediate-reverse) will differ from this baseline in ways Phase 2 will quantify.

Key observation from the baseline: **the bracket math is fine** (1.44× reward ratio on wins vs losses). The shortfall is in the **hit rate** — at 36.95% WR with a 1.44× reward, expected value per trade is `0.3695 × 27.83 − 0.6305 × 19.28 = −1.87` per trade. Multiply by 8,740 → matches the −$16K net to within rounding. The wrapper is bleeding because the signal doesn't hit often enough to overcome 1:1.44 economics; it needs ~41% WR to break even with these brackets.

For comparison context, SESSION's baseline (from the prior forensic) was 47.6% WR / PF ≈ 1.06 over 42 trades and 2 trading days. SESSION is the floor any new strategy must beat. IGNITION's wrapper at scale is **worse than SESSION** — it doesn't beat SESSION's near-zero net once you have 8,000 trades of statistical mass.

---

## 3. Live historical audit

What I searched, where, and what I found.

### `broker_events` table — Supabase

Query: `select kind, count(*) from broker_events where strategy in ('ignition', 'go_no_go') group by kind`

| Strategy   | Total rows | Breakdown                                                          |
|------------|------------|--------------------------------------------------------------------|
| `ignition` | **8**      | 2× `signal_emitted`, 2× `dry_run_signal`, 2× `signal_arbitrated`, 2× `dry_run_close` |
| `go_no_go` | **0**      | —                                                                  |

Eight events total for IGNITION — that's two complete phantom-trade cycles (signal → arbitration → emission → close). Probably from an early development sanity run, not real shadow data. **Two closed trades is not statistically interpretable** — well below the 20-trade threshold for forensic metrics.

### `shadow_events` table

Query: `select id from shadow_events limit 1` → `Could not find the table 'public.shadow_events' in the schema cache`

**The table does not exist.** No separate shadow-event log. All event logging in this codebase goes through `broker_events` regardless of mode (live vs dry-run); the `kind` column distinguishes (`dry_run_*` vs `signal_*`).

### `ryan_spec_v3_trades` table

This is the v3-era trade table (from the predecessor system). 4,923 rows. Column schema: `id, mode, bar_ts, direction, entry_ts, entry_price, stop_price, cum_delta_at_entry, atr_at_entry, exit_ts, exit_price, exit_reason, pnl_dollars, bars_held, mfe_atr, mae_atr, slippage_ticks, created_at, strategy_id`.

The `strategy_id` column ties rows to v3 variants (v3-canon, v3-armor, etc.) — see the retired keepers in your last health check. **No IGNITION or go_no_go rows live here**; this is a v3 archive table, not the v4 fleet's storage.

### Verdict

**There is no live history for either IGNITION or GO/NO-GO worth reporting forensic metrics on.** Two complete phantom trades in the entire broker_events table for IGNITION. Zero for go_no_go. No alternate shadow log. The audit ends here.

This is the expected outcome given Phase 0's reconciliation — IGNITION was never registered in the `strategies` table and never bound by `fleet_runner._build_keeper_instances()`. It has no live datapath; the two phantom trades were almost certainly from a one-off dev test before the fleet pivot.

---

## 4. Status

- Standalone `go_no_go_strategy.py` extracted and lint-clean. Not registered, not bound by runner. No live runner contact.
- Baseline reference: IGNITION-wrapper backtest at -$16,410 / PF 0.846 / 36.95% WR over 8,740 trades / 25 months.
- Live history: effectively none (2 phantom trades for IGNITION, 0 for go_no_go). Forensic metrics not computable.
- Phase 2 is now the real data source — fresh backtest of the standalone strategy on the same 25-month window.

Phase 1 complete. Awaiting explicit go for Phase 2.
