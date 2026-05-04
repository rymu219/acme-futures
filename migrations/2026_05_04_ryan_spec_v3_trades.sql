-- Ryan-Spec OOS-v3 paper/live trades table.
-- Apply against existing Supabase. Idempotent — IF NOT EXISTS guards.

create table if not exists ryan_spec_v3_trades (
  id                  bigserial primary key,
  mode                text not null,
  bar_ts              timestamptz not null,
  direction           text not null,
  entry_ts            timestamptz,
  entry_price         numeric,
  stop_price          numeric,
  cum_delta_at_entry  int,
  atr_at_entry        numeric,
  exit_ts             timestamptz,
  exit_price          numeric,
  exit_reason         text,
  pnl_dollars         numeric,
  bars_held           int,
  mfe_atr             numeric,
  mae_atr             numeric,
  slippage_ticks      int,
  created_at          timestamptz not null default now()
);

create index if not exists ryan_spec_v3_trades_mode_bar_ts_idx
  on ryan_spec_v3_trades (mode, bar_ts desc);
create index if not exists ryan_spec_v3_trades_exit_reason_idx
  on ryan_spec_v3_trades (exit_reason);
