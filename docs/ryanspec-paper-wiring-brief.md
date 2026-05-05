# Ryan-Spec Paper-Wiring Brief

How the v3 strategy moves from paper to live trading on a Topstep account.

## Step 7 — paper-week promotion gate

**Implementation**: [`src/acme/ryan_spec/v3_promotion.py`](../src/acme/ryan_spec/v3_promotion.py)
**Test coverage**: [`tests/test_ryan_spec_v3_promotion.py`](../tests/test_ryan_spec_v3_promotion.py)
**Watcher view**: [`web/ryan_spec_v3_view.py`](../web/ryan_spec_v3_view.py)

### Purpose

Decide whether v3 is allowed to graduate from paper to live trading on a real Topstep account. Reads the `ryan_spec_v3_trades` table (filtered to `mode='paper'`, typically the most recent ~5 trading days) and emits one of four verdicts.

### Inputs

The Supabase `ryan_spec_v3_trades` table, where each settled row carries:

- `pnl_dollars` — realized P&L (commission-adjusted)
- `slippage_ticks` — entry slippage vs the modeled fill (signed; positive = adverse). Populated by `_record_entry_fill` once the broker confirms the entry; may be NULL during early paper trading.
- `exit_reason` — `opposite_signal` | `session_end` | `stop` | `time_stop`
- `bar_ts` — used to break the window into months for stability checks

### Verdicts

| Verdict           | Meaning                                                                   |
| ----------------- | ------------------------------------------------------------------------- |
| `PROMOTE_LIVE`    | All gates passed. Safe to flip `ACME_MODE=live` and restart the runtime.  |
| `EXTEND_PAPER`    | Not enough settled trades to evaluate. Keep running paper and re-check.   |
| `HALT`            | A hard floor was breached. Do not promote; investigate root cause.        |
| `INVESTIGATE`     | Distribution shift detected. Review before promoting.                     |

### The four hard gates

Evaluated in this order — first failing gate determines the verdict.

1. **Settled trade count ≥ 200** → otherwise `EXTEND_PAPER`.
   ~5 trading days at the OOS-validated frequency (~89 trades/day) gives roughly 200 round-trips. Lower n risks attribution noise drowning out signal.

2. **Profit factor ≥ 1.5** → otherwise `HALT`.
   OOS PF was 2.30. The 1.5 floor tolerates ~35% live-vs-OOS degradation before halting. Below this, paper is meaningfully worse than backtest — promotion would just compound the gap with broker friction.

3. **Average slippage ≤ 1.5 ticks** → otherwise `HALT`.
   OOS modeled fills at 1.0 tick adverse. If real fills average more than 1.5 ticks, fill quality is materially worse than the model and PnL won't match expectations.
   Skipped when the column is missing or all-NULL (early paper, before entry-fill reconciliation has caught up).

4. **Opposite-signal exit share ≥ 40%** → otherwise `INVESTIGATE`.
   OOS exit distribution had ~53% opposite-signal exits (the strategy normally gets out via the next opposite trigger, not via stop or time). A drop below 40% suggests the trigger pattern itself is shifting (more stops, more time-stops, more session-end exits) — `INVESTIGATE` rather than `HALT` because PF could still be acceptable but the *mechanism* has changed.

### Manual mode flip is intentional

The gate emits a verdict; flipping `ACME_MODE=live` is a deliberate human action. Promotion to a live Topstep account is high-stakes and benefits from a manual review of the supporting numbers (monthly PF stability, exit distribution, slippage histogram) that the verdict alone doesn't surface. The watcher dashboard renders these.

### Operating it

```bash
uv run python -m acme.ryan_spec.v3_promotion --mode paper --since 2026-04-29T00:00:00
```

Prints the verdict, the supporting numbers, the per-month PF breakdown, and the exit-reason distribution.
