# Eval-log build — Phase 1 migration

## What was added

**File:** `migrations/2026_05_14_eval_log.sql`

Mirrors the existing five-migration convention found in Phase 0 — header comment block documenting intent + every column, idempotent `create table if not exists` / `create index if not exists`, dated filename. The descriptive `near_miss` comment is set in place above the column per your spec.

## Schema

```
eval_log
├── id            bigserial primary key
├── created_at    timestamptz not null default now()
├── bar_ts        timestamptz not null
├── strategy      text not null
├── outcome       text not null            -- 'PASS' | 'NEAR' | 'ENTRY' | 'HOLD'
├── gate_failed   text                     -- nullable
├── near_miss     boolean not null default false
├── signal_side   text                     -- nullable, 'buy' | 'sell'
├── gate_values   jsonb                    -- nullable, strategy-specific shape
└── reason        text                     -- nullable, max 120 chars by convention
```

**Indexes:**
- `eval_log_bar_ts_idx` on `(bar_ts desc)` — chronological scans for "what happened today"
- `eval_log_strategy_bar_ts_idx` on `(strategy, bar_ts desc)` — per-strategy activity feeds
- `eval_log_near_miss_idx` on `(near_miss, bar_ts desc) where near_miss = true` — partial index since `near_miss=true` is the read-heavy slice and most rows will be `false`

## The `near_miss` column — comment in full

The migration includes this comment block above the column per your spec:

> `near_miss` is the PRIMARY INVISIBLE-WORK SIGNAL of this table. True means every entry gate the strategy defines passed cleanly, and yet the strategy stood down — because an *external* condition blocked it: outside its time window, position already open, direction conflicts with the level being approached, daily loss limit hit, etc. These are the moments the system was right at the door, evaluated correctly, and chose not to act for a valid reason. They are the "invisible saves" — the work that prevents bad trades from happening, work the dashboard now exposes so the operator can see the system thinking even on no-trade days.

## Apply

**Programmatic application is not possible** with the credentials in `.env`. I tried:

- `POST <project>.supabase.co/pg-meta/v1/query` — 404, pg-meta isn't exposed publicly
- `POST /rest/v1/rpc/exec_sql` — 404, no `exec_sql` RPC installed in this project

Only `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are in `.env`. PostgREST (which the service-role key authenticates against) supports table-level CRUD but not DDL. No direct postgres connection string is available. **This matches the convention found in Phase 0** — all five prior migrations were applied manually via Supabase Studio.

**Apply manually:**

1. Open Supabase Studio → SQL Editor: https://supabase.com/dashboard/project/wjzzkshyqhvqyohuxbbl/sql/new
2. Paste the contents of `migrations/2026_05_14_eval_log.sql`
3. Run

The migration is idempotent — safe to re-run if anything else changes mid-flight.

## Verification — post-apply

Confirmed via four-row probe insert + read-back + NOT NULL guard tests. All probes inserted and read back cleanly; cleanup left the table empty.

```
[OK] Inserted 4 probe rows
                outcome=PASS   gate_failed=sep_ok   side=-     GO/NO-GO sep 0.28 below threshold 0.35
  ★ NEAR-MISS   outcome=NEAR   gate_failed=-        side=buy   All gates PASS · ORL 1.5t — outside 08-12 CT window
                outcome=ENTRY  gate_failed=-        side=buy   ENTRY at PDL — gates clear, in window
                outcome=HOLD   gate_failed=-        side=-     HOLD — long position, no opposite signal

[NOT NULL guards]
  OK — missing bar_ts   → rejected
  OK — missing strategy → rejected
  OK — missing outcome  → rejected

Final row count after cleanup: 0
```

All four outcome variants round-trip correctly. JSONB `gate_values` accepts the nested dict shape. NOT NULL constraints reject missing `bar_ts` / `strategy` / `outcome` as expected.

## ⚠️ Finding worth carrying into Phase 2

**`near_miss` cannot rely on the SQL `default false`.** PostgREST sends explicit `null` for any JSON key absent from the insert payload, which overrides the column default and triggers the `NOT NULL` constraint. Caught this on the first probe attempt:

```
postgrest.exceptions.APIError: null value in column "near_miss" of relation
"eval_log" violates not-null constraint
```

**Action for Phase 2 instrumentation:** every `EvalResult` write from the conductor must set `near_miss` explicitly (`True` or `False`). The schema is correct; this is a client-side requirement. I'll bake it into the `EvalResult` dataclass (the field is non-optional with no default), so it's impossible to forget at the call site.

## Status

Phase 1 complete. Table exists, schema correct, indexes in place, NOT NULL guards enforced. Awaiting explicit go for Phase 2 (EvalResult + strategy instrumentation).
