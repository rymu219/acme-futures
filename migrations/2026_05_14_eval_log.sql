-- eval_log — one row per (strategy, bar) evaluation, regardless of outcome.
--
-- Surfaces the invisible work done by each strategy on every bar. Every
-- strategy currently computes its gate values on every bar and silently
-- discards them when no trade fires; this table makes that computation
-- visible — to the dashboard, to forensic queries, to the operator —
-- so a slow day no longer looks like a dead bot.
--
-- One row is written from the conductor's per-bar loop for each active
-- strategy. The strategy itself returns either:
--   Signal       — fired a trade (logged separately to broker_events)
--   EvalResult   — evaluated and chose not to trade (logged here)
--   None         — still warming up; no row written
--
-- Outcome semantics:
--   PASS   — strategy didn't fire because one of its gates failed
--            (e.g. GO/NO-GO sep below threshold, no level proximity,
--            bias body too strong); gate_failed names the first
--            blocking gate.
--   NEAR   — all entry gates passed but an external condition blocked
--            the entry (see near_miss column comment below).
--   ENTRY  — strategy fired a Signal this bar. eval_log captures the
--            decision context; the trade itself is in broker_events.
--   HOLD   — strategy is in a position and chose not to act
--            (no opposite signal, no force-flat, etc.).
--
-- Apply against existing Supabase. Idempotent.

create table if not exists eval_log (
  id            bigserial primary key,
  created_at    timestamptz not null default now(),
  bar_ts        timestamptz not null,            -- the bar's timestamp
  strategy      text        not null,            -- strategy name string
  outcome       text        not null,            -- 'PASS' | 'NEAR' | 'ENTRY' | 'HOLD'
  gate_failed   text,                            -- first gate that blocked entry;
                                                  -- null if outcome is ENTRY or HOLD

  -- near_miss is the PRIMARY INVISIBLE-WORK SIGNAL of this table.
  -- True means every entry gate the strategy defines passed cleanly,
  -- and yet the strategy stood down — because an *external* condition
  -- blocked it: outside its time window, position already open,
  -- direction conflicts with the level being approached, daily loss
  -- limit hit, etc. These are the moments the system was right at
  -- the door, evaluated correctly, and chose not to act for a valid
  -- reason. They are the "invisible saves" — the work that prevents
  -- bad trades from happening, work the dashboard now exposes so
  -- the operator can see the system thinking even on no-trade days.
  near_miss     boolean     not null default false,

  signal_side   text,                            -- 'buy' | 'sell' | null
  gate_values   jsonb,                           -- raw computed values for this bar:
                                                  -- sep, rvol, slope, level_dist, atr,
                                                  -- charge_pct, etc. — whatever the
                                                  -- strategy computes. Strategy-specific
                                                  -- shape; readers must tolerate that.
  reason        text                             -- human-readable one-line summary,
                                                  -- max 120 chars, plain English,
                                                  -- written by the strategy.
);

create index if not exists eval_log_bar_ts_idx
  on eval_log (bar_ts desc);
create index if not exists eval_log_strategy_bar_ts_idx
  on eval_log (strategy, bar_ts desc);
create index if not exists eval_log_near_miss_idx
  on eval_log (near_miss, bar_ts desc)
  where near_miss = true;
