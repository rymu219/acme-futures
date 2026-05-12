-- operator_events — Warden analyst agent's audit log.
--
-- Warden runs out-of-process on Railway, monitors the new fleet via
-- Supabase, and writes structured events here for the operator (Ryan)
-- to review. Email / SMS routing is layered on top of these rows
-- (planned but not in this migration).
--
-- Event kinds (extensible — Warden adds new strings, this column is
-- intentionally free-form text not an enum):
--   heartbeat_stale     — a fleet heartbeat hasn't ticked recently
--   heartbeat_recovered — a previously-stale heartbeat is alive again
--   cluster_firing      — N+ strategies opened the same direction
--                          within a 5-minute window
--   lifecycle_change    — strategy state transition (SHADOW→PILOT etc.)
--   daily_brief         — Warden's morning 7:00 CT summary
--   weekly_packet       — Warden's Sunday review
--   reconciliation_warn — heartbeat says X position but trade-row
--                          says Y (the orphan-detection signal)
--   anomaly             — catch-all for new monitor types
--
-- Severity (used for routing — info goes to the dashboard only;
-- warn → daily brief; error → immediate email; critical → SMS):
--   info | warn | error | critical
--
-- Idempotent migration. Re-applying is a no-op.

create table if not exists operator_events (
  id              bigserial primary key,
  occurred_at     timestamptz not null default now(),
  kind            text        not null,
  severity        text        not null default 'info',
  strategy        text,                     -- nullable: not all events are strategy-scoped
  summary         text        not null,     -- one-line human-readable
  details         jsonb,                    -- structured payload (kind-specific shape)
  resolved_at     timestamptz,              -- nullable; for recovery / acknowledged
  source          text        not null default 'warden'
);

create index if not exists operator_events_occurred_idx
  on operator_events (occurred_at desc);
create index if not exists operator_events_kind_idx
  on operator_events (kind, occurred_at desc);
create index if not exists operator_events_strategy_idx
  on operator_events (strategy, occurred_at desc);
create index if not exists operator_events_severity_idx
  on operator_events (severity, occurred_at desc)
  where severity in ('error', 'critical');
