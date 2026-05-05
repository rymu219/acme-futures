-- Runtime heartbeat + remote kill-switch + circuit breaker.
-- Adds two ops tables shared by every long-running runtime (currently
-- ryan_spec_v3; the Conductor can reuse the same tables later).
--
-- runtime_heartbeats: single row per service, upserted each bar. The watcher
-- reads the latest ts to show "last seen Ns ago".
--
-- runtime_config:     single row per service, holds the remote pause flag and
-- the consecutive-error threshold. Flipping `paused=true` from any browser
-- (Supabase Studio works on phone) tells the runtime to skip new entries on
-- its next config poll. Open positions are still allowed to exit.
--
-- Apply against existing Supabase. Idempotent.

create table if not exists runtime_heartbeats (
  service             text primary key,         -- e.g. 'ryan_spec_v3'
  ts                  timestamptz not null,     -- last write
  last_bar_ts         timestamptz,              -- last 2m bar processed
  auth_ok             boolean not null,
  consecutive_errors  int not null default 0,
  position_state      text not null,            -- 'flat' | 'long' | 'short'
  extra               jsonb                     -- contract_id, mode, broker, etc.
);

create table if not exists runtime_config (
  service                  text primary key,    -- e.g. 'ryan_spec_v3'
  paused                   boolean not null default false,
  max_consecutive_errors   int not null default 3,
  updated_at               timestamptz not null default now(),
  updated_by               text                  -- 'manual' | 'self_halt' | etc.
);

-- Seed row so the runtime's first read returns defaults instead of empty.
insert into runtime_config (service, paused, max_consecutive_errors)
values ('ryan_spec_v3', false, 3)
on conflict (service) do nothing;
