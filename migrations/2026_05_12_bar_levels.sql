-- bar_levels — per-day session levels used by the BOUNDARY strategy and
-- by the historical retag script (scripts/retag_v3_trades.py).
--
-- Levels stored per (date, contract):
--   pdh / pdl  — Previous Day High / Low (RTH session: 08:30-15:00 CT)
--   onh / onl  — Overnight High / Low (Globex: 17:00 prior-day - 08:30 current)
--   orh / orl  — Opening Range High / Low (first 30 min of RTH: 08:30-09:00 CT)
--
-- Idempotent: applies against existing Supabase. PK is (trade_date, contract)
-- so re-running the levels job upserts the same row.

create table if not exists bar_levels (
  trade_date        date         not null,
  contract          text         not null,           -- e.g. 'MES'
  pdh               numeric,
  pdl               numeric,
  onh               numeric,
  onl               numeric,
  orh               numeric,
  orl               numeric,
  computed_at       timestamptz  not null default now(),
  source            text         not null default 'databento_cache',
  primary key (trade_date, contract)
);

create index if not exists bar_levels_date_idx on bar_levels (trade_date desc);
