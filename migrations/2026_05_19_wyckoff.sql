-- wyckoff_state + wyckoff_events — Wyckoff phase classifier persistence.
--
-- Two-table design mirroring the regime engine's `market_regimes` pattern
-- but split into a per-bar state snapshot (`wyckoff_state`) and a sparse
-- append-only event log (`wyckoff_events`). The event log is the source
-- of truth for state replay on conductor restart.
--
-- The classifier (`acme.wyckoff.WyckoffClassifier`) emits one snapshot
-- per bar and zero-or-more events per bar. The conductor persists both
-- on the bars that pass through its `_process_bar` loop.
--
-- Idempotent. Apply via Supabase Studio per the established convention.

create table if not exists wyckoff_state (
  id              bigserial primary key,
  ts              timestamptz not null,
  timeframe       text not null,                    -- '2m', '5m'
  contract        text not null,                    -- 'MES'

  phase           text not null,                    -- 'unknown' | 'accumulation' |
                                                     -- 'markup' | 'distribution' | 'markdown'
  last_event      text,                              -- 'sc' | 'ar' | 'st' | 'spring' | 'sos' | 'ut'
  last_event_ts   timestamptz,
  armed_for       text,                              -- next-expected event kind

  spread_atr      numeric,                           -- (bar.h - bar.l) / atr
  rvol            numeric,                           -- bar.v / SMA(vol, 20)
  raw_state       jsonb,                             -- reasoning trace, debug bag

  created_at      timestamptz not null default now()
);
create index if not exists wyckoff_state_ts_idx        on wyckoff_state (ts desc);
create index if not exists wyckoff_state_phase_idx     on wyckoff_state (phase, ts desc);
create index if not exists wyckoff_state_contract_idx  on wyckoff_state (contract, ts desc);


-- One row per confirmed Wyckoff event. Sparse — events are RARE.
-- This is the persistent source of truth for state replay.
--
-- For Spring events specifically: `bar_ts` is the RECOVERY bar's
-- timestamp, NOT the candidate bar's. Arm-on-candidate /
-- confirm-on-recovery is enforced in the classifier; this table
-- only ever records confirmed events.

create table if not exists wyckoff_events (
  id                bigserial primary key,
  bar_ts            timestamptz not null,
  contract          text        not null,

  event_kind        text        not null,           -- 'sc'|'ar'|'st'|'spring'|'sos'|'ut'
  phase_at_event    text        not null,

  bar_o             numeric,
  bar_h             numeric,
  bar_l             numeric,
  bar_c             numeric,
  bar_v             bigint,

  spread_pts        numeric,                        -- bar.h - bar.l
  atr_pts           numeric,
  spread_atr_ratio  numeric,
  rvol              numeric,
  volume_vs_anchor  numeric,                        -- e.g., ST.v / SC.v, Spring.v / ST.v

  -- Cross-reference to the prior event that this one resolves.
  -- ST refs SC; Spring refs ST; SOS refs Spring; AR refs SC.
  ref_event_kind    text,
  ref_event_ts      timestamptz,

  notes             text,                            -- reasoning trace

  created_at        timestamptz not null default now()
);
create index if not exists wyckoff_events_bar_ts_idx
  on wyckoff_events (bar_ts desc);
create index if not exists wyckoff_events_kind_ts_idx
  on wyckoff_events (event_kind, bar_ts desc);
create index if not exists wyckoff_events_contract_idx
  on wyckoff_events (contract, bar_ts desc);
