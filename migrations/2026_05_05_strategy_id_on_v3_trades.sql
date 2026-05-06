-- Multi-variant scaffolding for Ryan-Spec v3.
-- One ProjectX connection, N parallel V3Runtime instances, each writing
-- trades tagged with its own strategy_id so the watcher can render per-
-- variant P&L and the analyst can compare them.
--
-- Heartbeat / kill-switch tables don't need new columns — they're already
-- keyed by `service`, so each variant just uses its own service name
-- (e.g., 'ryan_spec_v3.canon', 'ryan_spec_v3.trail').
--
-- Apply against existing Supabase. Idempotent.

alter table ryan_spec_v3_trades
add column if not exists strategy_id text not null default 'v3-canon';

create index if not exists ryan_spec_v3_trades_strategy_id_bar_ts_idx
  on ryan_spec_v3_trades (strategy_id, bar_ts desc);

-- Seed a runtime_config row for each variant so each starts un-paused with
-- a clean error budget. service name == strategy_id throughout, so the
-- watcher / runtime / kill-switch can all key on the same identifier.
-- The legacy 'ryan_spec_v3' row from the previous migration is left in
-- place (deprecated; no new code references it).
insert into runtime_config (service, paused, max_consecutive_errors)
values
  ('v3-canon',    false, 3),
  ('v3-trail',    false, 3),
  ('v3-min2bar',  false, 3),
  ('v3-armor',    false, 3),
  ('v3-pctile',   false, 3)
on conflict (service) do nothing;
