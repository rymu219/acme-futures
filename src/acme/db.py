"""Supabase event logger. Append-only writes to broker_events; upserts on daily_state."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from typing import Any

import structlog
from supabase import Client, create_client

log = structlog.get_logger(__name__)


class Db:
    def __init__(self, url: str | None = None, service_role_key: str | None = None) -> None:
        self.url = url or os.getenv("SUPABASE_URL", "")
        self.key = service_role_key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        self._client: Client | None = None

    @property
    def client(self) -> Client:
        if self._client is None:
            if not self.url or not self.key:
                raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set")
            self._client = create_client(self.url, self.key)
        return self._client

    def log_event(
        self,
        kind: str,
        *,
        contract_id: str | None = None,
        symbol: str | None = None,
        account_id: str | None = None,
        side: str | None = None,
        size: int | None = None,
        price: float | None = None,
        strategy: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        row = {
            "kind": kind,
            "contract_id": contract_id,
            "symbol": symbol,
            "account_id": account_id,
            "side": side,
            "size": size,
            "price": price,
            "strategy": strategy,
            "raw": raw or {},
        }
        try:
            self.client.table("broker_events").insert(row).execute()
        except Exception as e:
            log.error("db_log_event_failed", kind=kind, error=str(e))

    def write_wyckoff_snapshot(self, row: dict[str, Any]) -> None:
        """One row per bar to `wyckoff_state`. Fire-and-forget, same
        pattern as log_event."""
        try:
            self.client.table("wyckoff_state").insert(row).execute()
        except Exception as e:
            log.error("db_wyckoff_snapshot_failed", error=str(e))

    def write_wyckoff_event(self, row: dict[str, Any]) -> None:
        """One row per confirmed Wyckoff event to `wyckoff_events`."""
        try:
            self.client.table("wyckoff_events").insert(row).execute()
        except Exception as e:
            log.error("db_wyckoff_event_failed",
                      kind=row.get("event_kind"), error=str(e))

    def fetch_recent_wyckoff_events(
        self, *, contract: str = "MES", limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Read the most recent N confirmed Wyckoff events in
        chronological order (oldest first). Used by the classifier's
        state-replay on conductor startup."""
        try:
            res = (self.client.table("wyckoff_events")
                   .select("bar_ts,event_kind,bar_l,bar_h,bar_c,bar_v")
                   .eq("contract", contract)
                   .order("bar_ts", desc=True).limit(limit).execute())
            rows = res.data or []
            # Reverse so caller can replay in chronological order.
            return list(reversed(rows))
        except Exception as e:
            log.error("db_fetch_wyckoff_events_failed", error=str(e))
            return []

    def write_eval_log(
        self,
        *,
        bar_ts: datetime,
        strategy: str,
        outcome: str,
        near_miss: bool,
        gate_failed: str | None = None,
        signal_side: str | None = None,
        gate_values: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> None:
        """Append one row to the `eval_log` table.

        Same fire-and-forget pattern as `log_event` — synchronous insert
        wrapped in try/except so write failures don't tear down the bar
        loop. `near_miss` is required (no default) because PostgREST
        overrides Postgres column defaults with explicit nulls on
        missing JSON keys, violating eval_log's NOT NULL constraint.
        """
        row = {
            "bar_ts": bar_ts.isoformat(),
            "strategy": strategy,
            "outcome": outcome,
            "near_miss": bool(near_miss),
            "gate_failed": gate_failed,
            "signal_side": signal_side,
            "gate_values": gate_values or {},
            "reason": (reason or "")[:120],   # 120-char convention per migration comment
        }
        try:
            self.client.table("eval_log").insert(row).execute()
        except Exception as e:
            log.error("db_eval_log_write_failed",
                      strategy=strategy, outcome=outcome, error=str(e))

    def read_control_flag(self, flag: str) -> dict | None:
        try:
            res = self.client.table("control_flags").select("*").eq("flag", flag).limit(1).execute()
            return (res.data or [None])[0]
        except Exception as e:
            log.error("db_read_control_flag_failed", flag=flag, error=str(e))
            return None

    def clear_control_flag(self, flag: str) -> None:
        try:
            self.client.table("control_flags").update({
                "active": False, "requested_at": None, "reason": None, "set_by": None,
            }).eq("flag", flag).execute()
        except Exception as e:
            log.error("db_clear_control_flag_failed", flag=flag, error=str(e))

    def upsert_daily_state(
        self,
        trade_date: date,
        *,
        starting_balance: float,
        realized_pnl: float = 0.0,
        ending_balance: float | None = None,
        peak_balance_eod: float | None = None,
        max_loss_limit: float | None = None,
        daily_loss_limit: float | None = None,
        notes: str | None = None,
    ) -> None:
        row: dict[str, Any] = {
            "trade_date": trade_date.isoformat(),
            "starting_balance": starting_balance,
            "realized_pnl": realized_pnl,
        }
        if ending_balance is not None:
            row["ending_balance"] = ending_balance
        if peak_balance_eod is not None:
            row["peak_balance_eod"] = peak_balance_eod
        if max_loss_limit is not None:
            row["max_loss_limit"] = max_loss_limit
        if daily_loss_limit is not None:
            row["daily_loss_limit"] = daily_loss_limit
        if notes is not None:
            row["notes"] = notes
        try:
            self.client.table("daily_state").upsert(row, on_conflict="trade_date").execute()
        except Exception as e:
            log.error("db_upsert_daily_state_failed", date=str(trade_date), error=str(e))

    # ---------- C-2 regime helpers ----------

    def insert_regime_snapshot(self, row: dict[str, Any]) -> None:
        try:
            self.client.table("market_regimes").insert(row).execute()
        except Exception as e:
            log.error("db_insert_regime_snapshot_failed", error=str(e))

    def insert_regime_snapshots(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        try:
            self.client.table("market_regimes").insert(rows).execute()
        except Exception as e:
            log.error("db_insert_regime_snapshots_failed", n=len(rows), error=str(e))

    def latest_regime(self) -> dict | None:
        try:
            res = (
                self.client.table("market_regimes")
                .select("*")
                .order("ts", desc=True)
                .limit(1)
                .execute()
            )
            return (res.data or [None])[0]
        except Exception as e:
            log.error("db_latest_regime_failed", error=str(e))
            return None

    def regime_at_or_before(self, ts_iso: str) -> dict | None:
        """Returns the most recent market_regimes row with ts <= ts_iso."""
        try:
            res = (
                self.client.table("market_regimes")
                .select("*")
                .lte("ts", ts_iso)
                .order("ts", desc=True)
                .limit(1)
                .execute()
            )
            return (res.data or [None])[0]
        except Exception as e:
            log.error("db_regime_at_or_before_failed", error=str(e))
            return None

    def upsert_trade_regime_tag(self, row: dict[str, Any]) -> None:
        try:
            self.client.table("trade_regime_tags").upsert(
                row, on_conflict="trade_id"
            ).execute()
        except Exception as e:
            log.error("db_upsert_trade_regime_tag_failed", error=str(e))

    def upsert_regime_perf(self, row: dict[str, Any]) -> None:
        try:
            self.client.table("regime_strategy_performance").upsert(
                row, on_conflict="strategy,regime"
            ).execute()
        except Exception as e:
            log.error("db_upsert_regime_perf_failed", error=str(e))

    def insert_coverage_gap(self, row: dict[str, Any]) -> None:
        try:
            self.client.table("coverage_gaps").insert(row).execute()
        except Exception as e:
            log.error("db_insert_coverage_gap_failed", error=str(e))

    # ---------- ryan_spec helpers ----------

    def upsert_ryan_spec_ground_truth(self, row: dict[str, Any]) -> None:
        try:
            self.client.table("ryan_spec_ground_truth").upsert(
                row, on_conflict="setup_number"
            ).execute()
        except Exception as e:
            log.error("db_upsert_ryan_spec_ground_truth_failed", error=str(e))

    def update_ryan_spec_ground_truth(self, setup_number: int, fields: dict[str, Any]) -> None:
        try:
            self.client.table("ryan_spec_ground_truth").update(fields).eq(
                "setup_number", setup_number
            ).execute()
        except Exception as e:
            log.error("db_update_ryan_spec_ground_truth_failed",
                      setup_number=setup_number, error=str(e))

    def select_ryan_spec_ground_truth(self) -> list[dict]:
        try:
            res = (
                self.client.table("ryan_spec_ground_truth")
                .select("*")
                .order("setup_number", desc=False)
                .execute()
            )
            return res.data or []
        except Exception as e:
            log.error("db_select_ryan_spec_ground_truth_failed", error=str(e))
            return []

    def insert_ryan_spec_trigger(self, row: dict[str, Any]) -> int | None:
        try:
            res = self.client.table("ryan_spec_triggers").insert(row).execute()
            data = res.data or []
            return data[0]["id"] if data else None
        except Exception as e:
            log.error("db_insert_ryan_spec_trigger_failed", error=str(e))
            return None

    def insert_ryan_spec_triggers(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        try:
            self.client.table("ryan_spec_triggers").insert(rows).execute()
        except Exception as e:
            log.error("db_insert_ryan_spec_triggers_failed", n=len(rows), error=str(e))

    def update_ryan_spec_trigger(self, trigger_id: int, fields: dict[str, Any]) -> None:
        try:
            self.client.table("ryan_spec_triggers").update(fields).eq(
                "id", trigger_id
            ).execute()
        except Exception as e:
            log.error("db_update_ryan_spec_trigger_failed",
                      trigger_id=trigger_id, error=str(e))

    def bulk_upsert_ryan_spec_triggers(self, rows: list[dict[str, Any]]) -> None:
        """Bulk update — each row must include the primary `id` column.
        Uses Supabase upsert (on_conflict=id) so 100s of rows go in one HTTP."""
        if not rows:
            return
        try:
            self.client.table("ryan_spec_triggers").upsert(
                rows, on_conflict="id"
            ).execute()
        except Exception as e:
            log.error("db_bulk_upsert_ryan_spec_triggers_failed",
                      n=len(rows), error=str(e))

    def insert_ryan_spec_regression(self, row: dict[str, Any]) -> None:
        try:
            self.client.table("ryan_spec_regression").insert(row).execute()
        except Exception as e:
            log.error("db_insert_ryan_spec_regression_failed", error=str(e))

    # ---------- ryan_spec_v3 paper/live trades ----------

    def insert_ryan_spec_v3_trade(self, row: dict[str, Any]) -> int | None:
        """Insert one open trade row; returns its id on success.

        ARCHIVE NOTE (2026-05-11): the v3 multi-variant runtime is retired.
        The only legitimate callers now are cleanup / force-flatten
        scripts that write `manual_*` exit_reasons. Any other call site
        is unexpected — a structured warning is emitted to make
        accidental writes easy to spot in the runner log.
        """
        reason = row.get("exit_reason") or ""
        if not reason.startswith("manual_"):
            log.warning(
                "ryan_spec_v3_trades_unexpected_write",
                strategy_id=row.get("strategy_id"),
                exit_reason=reason or "(open)",
                note="v3 is archived; new writes should be cleanup/force-flatten only",
            )
        try:
            res = self.client.table("ryan_spec_v3_trades").insert(row).execute()
            data = res.data or []
            return int(data[0]["id"]) if data else None
        except Exception as e:
            log.error("db_insert_ryan_spec_v3_trade_failed", error=str(e))
            return None

    def update_ryan_spec_v3_trade(
        self, trade_id: int, fields: dict[str, Any]
    ) -> None:
        """Patch fields on an existing v3 trade row.

        ARCHIVE NOTE (2026-05-11): same caveat as insert. Updates that
        change `exit_reason` to a `manual_*` value are expected (cleanup
        flow). Any other field-level update is unexpected.
        """
        reason = fields.get("exit_reason") or ""
        if reason and not reason.startswith("manual_"):
            log.warning(
                "ryan_spec_v3_trades_unexpected_update",
                trade_id=trade_id,
                exit_reason=reason,
                note="v3 is archived; new updates should be cleanup/force-flatten only",
            )
        try:
            self.client.table("ryan_spec_v3_trades").update(fields).eq(
                "id", trade_id
            ).execute()
        except Exception as e:
            log.error("db_update_ryan_spec_v3_trade_failed",
                      trade_id=trade_id, error=str(e))

    def select_ryan_spec_v3_trades(
        self, *, mode: str | None = None, since: str | None = None,
        limit: int = 5000,
    ) -> list[dict]:
        """Read trades back for the Streamlit viewer + promotion gate."""
        try:
            q = self.client.table("ryan_spec_v3_trades").select("*")
            if mode:
                q = q.eq("mode", mode)
            if since:
                q = q.gte("bar_ts", since)
            res = q.order("bar_ts", desc=True).limit(limit).execute()
            return res.data or []
        except Exception as e:
            log.error("db_select_ryan_spec_v3_trades_failed", error=str(e))
            return []

    # ---------- runtime ops: heartbeat + remote kill-switch ----------

    def write_heartbeat(
        self,
        service: str,
        *,
        last_bar_ts: datetime | None,
        auth_ok: bool,
        consecutive_errors: int,
        position_state: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Upsert a single heartbeat row for `service`. Best-effort —
        never raises. The runtime calls this on every closed bar so the
        watcher can detect a stale or dead bot."""
        row: dict[str, Any] = {
            "service": service,
            "ts": datetime.now(UTC).isoformat(),
            "last_bar_ts": last_bar_ts.astimezone(UTC).isoformat() if last_bar_ts else None,
            "auth_ok": bool(auth_ok),
            "consecutive_errors": int(consecutive_errors),
            "position_state": position_state,
            "extra": extra or {},
        }
        try:
            self.client.table("runtime_heartbeats").upsert(
                row, on_conflict="service"
            ).execute()
        except Exception as e:
            log.warning("db_write_heartbeat_failed", service=service, error=str(e))

    def read_runtime_config(self, service: str) -> dict[str, Any]:
        """Fetch the kill-switch config for `service`. Returns safe defaults
        on read failure so a Supabase hiccup never silently halts trading."""
        defaults = {"paused": False, "max_consecutive_errors": 3}
        try:
            res = (
                self.client.table("runtime_config")
                .select("paused,max_consecutive_errors")
                .eq("service", service)
                .limit(1)
                .execute()
            )
            rows = res.data or []
            if not rows:
                return defaults
            row = rows[0]
            return {
                "paused": bool(row.get("paused", False)),
                "max_consecutive_errors": int(
                    row.get("max_consecutive_errors") or defaults["max_consecutive_errors"]
                ),
            }
        except Exception as e:
            log.warning("db_read_runtime_config_failed",
                        service=service, error=str(e))
            return defaults

    def set_runtime_paused(self, service: str, *, paused: bool, by: str) -> None:
        """Flip the remote pause flag. Used by the self-halt circuit breaker
        and by manual operator action (via Supabase Studio). Best-effort."""
        try:
            self.client.table("runtime_config").upsert({
                "service": service,
                "paused": bool(paused),
                "updated_at": datetime.now(UTC).isoformat(),
                "updated_by": by,
            }, on_conflict="service").execute()
        except Exception as e:
            log.error("db_set_runtime_paused_failed",
                      service=service, paused=paused, by=by, error=str(e))

    def truncate_regime_tables(self, *, chunk_size: int = 1000) -> None:
        """Wipe all regime engine output before a clean re-run. Used by
        backfill --truncate.

        Supabase enforces a ~10s statement timeout. A single bulk DELETE on
        market_regimes (~150k rows after a 2yr backfill) exceeds that and
        times out silently. Chunk the delete in id-range batches instead.
        """
        for table in (
            "coverage_gaps",
            "regime_strategy_performance",
            "trade_regime_tags",
            "market_regimes",
        ):
            self._chunk_delete(table, chunk_size=chunk_size)

    def _chunk_delete(self, table: str, *, chunk_size: int = 1000) -> None:
        deleted = 0
        try:
            while True:
                res = (
                    self.client.table(table)
                    .select("id")
                    .order("id", desc=False)
                    .limit(chunk_size)
                    .execute()
                )
                ids = [r["id"] for r in (res.data or [])]
                if not ids:
                    break
                lo, hi = ids[0], ids[-1]
                self.client.table(table).delete().gte("id", lo).lte("id", hi).execute()
                deleted += len(ids)
            log.info("regime_table_truncated", table=table, deleted=deleted)
        except Exception as e:
            log.error("regime_table_truncate_failed", table=table,
                      deleted_so_far=deleted, error=str(e))


# SQL DDL kept here as a single source of truth — run manually in Supabase Studio.
SUPABASE_DDL = """
create table if not exists broker_events (
  id           bigserial primary key,
  occurred_at  timestamptz not null default now(),
  kind         text not null,
  contract_id  text,
  symbol       text,
  account_id   text,
  side         text,
  size         int,
  price        numeric(18,6),
  raw          jsonb not null,
  strategy     text,
  regime       text,
  confidence   numeric(5,4)
);
create index if not exists broker_events_occurred_at_idx on broker_events (occurred_at desc);
create index if not exists broker_events_kind_idx        on broker_events (kind);

create table if not exists daily_state (
  trade_date         date primary key,
  starting_balance   numeric(18,2) not null,
  realized_pnl       numeric(18,2) not null default 0,
  ending_balance     numeric(18,2),
  peak_balance_eod   numeric(18,2),
  max_loss_limit     numeric(18,2),
  daily_loss_limit   numeric(18,2),
  notes              text
);

create table if not exists strategy_state (
  strategy   text primary key,
  state      jsonb not null,
  updated_at timestamptz not null default now()
);

-- B1 additions: strategy registry + emergency control flags
create table if not exists strategies (
  name              text primary key,
  version           text not null,
  state             text not null,            -- BACKTEST|REPLAY|SHADOW|PILOT|LIVE|BENCH|RETIRED
  tier              int  not null default 2,
  params            jsonb not null default '{}'::jsonb,
  score             numeric(5,4) default 0,
  last_promoted_at  timestamptz,
  bench_cycles_30d  int default 0,
  notes             text
);

create table if not exists control_flags (
  flag             text primary key,
  active           boolean not null default false,
  requested_at     timestamptz,
  confirm_seconds  int default 60,
  reason           text,
  set_by           text
);

-- B3 addition: per-strategy rolling perf snapshot
create table if not exists strategy_perf_snapshot (
  id                    bigserial primary key,
  occurred_at           timestamptz not null default now(),
  strategy              text not null,
  window_label          text not null,
  sharpe                numeric(8,4),
  max_drawdown          numeric(10,2),
  profit_factor         numeric(8,4),
  win_rate              numeric(5,4),
  n_trades              int not null,
  net_pnl               numeric(10,2),
  signal_latency_p95_ms int,
  raw                   jsonb not null
);
create index if not exists strategy_perf_snapshot_strat_idx
  on strategy_perf_snapshot (strategy, occurred_at desc);

-- C-2 additions: Regime Engine
-- Per-bar regime classification (5m cadence intraday).
create table if not exists market_regimes (
  id               bigserial primary key,
  ts               timestamptz not null,
  timeframe        text not null,                 -- '5m', '15m', '1h'
  adx              numeric,
  adx_direction    text,                          -- 'rising' | 'falling' | 'flat'
  atr_current      numeric,
  atr_ratio        numeric,                       -- atr_current / mean(atr[-20:])
  bb_width         numeric,
  bb_width_pct     numeric,                       -- percentile rank in 50-bar lookback
  hurst            numeric,
  volume_ratio     numeric,
  momentum_score   numeric,                       -- 0..1 close position in period range
  regime           text not null,                 -- trending|ranging|compressing|chaotic|ambiguous
  regime_direction text,                          -- long_bias|short_bias|neutral
  confidence       numeric,                       -- 0..1
  raw_signals      jsonb,
  created_at       timestamptz not null default now()
);
create index if not exists market_regimes_ts_idx     on market_regimes (ts desc);
create index if not exists market_regimes_regime_idx on market_regimes (regime);

-- One row per trade, joining the trade to the regime active at entry.
create table if not exists trade_regime_tags (
  id                  bigserial primary key,
  trade_id            text not null,              -- references bar_events.id (sqlite) OR
                                                  -- broker_events.id (live signal_emitted)
  strategy            text not null,
  entry_ts            timestamptz not null,
  regime_at_entry     text not null,
  regime_direction    text,
  adx_at_entry        numeric,
  atr_ratio_at_entry  numeric,
  hurst_at_entry      numeric,
  confidence_at_entry numeric,
  pnl                 numeric,
  outcome             text,                       -- win|loss|scratch
  tagged_at           timestamptz not null default now()
);
create index if not exists trade_regime_tags_strategy_idx on trade_regime_tags (strategy);
create index if not exists trade_regime_tags_regime_idx   on trade_regime_tags (regime_at_entry);
create unique index if not exists trade_regime_tags_unique_trade
  on trade_regime_tags (trade_id);

-- Aggregated strategy x regime performance. Recomputed via analytics.
create table if not exists regime_strategy_performance (
  id              bigserial primary key,
  strategy        text not null,
  regime          text not null,
  trade_count     int,
  win_count       int,
  loss_count      int,
  win_rate        numeric,
  avg_win         numeric,
  avg_loss        numeric,
  profit_factor   numeric,
  expectancy      numeric,
  sharpe_approx   numeric,
  habitat_match   boolean,                        -- regime in this strategy's regime_fit >= 0.7
  computed_at     timestamptz not null default now()
);
create unique index if not exists regime_strategy_perf_unique
  on regime_strategy_performance (strategy, regime);

-- Periods where the market was in a regime no strategy could trade.
create table if not exists coverage_gaps (
  id            bigserial primary key,
  ts            timestamptz not null,
  regime        text not null,
  duration_bars int,
  adx           numeric,
  atr_ratio     numeric,
  hurst         numeric,
  notes         text,
  created_at    timestamptz not null default now()
);
create index if not exists coverage_gaps_ts_idx on coverage_gaps (ts desc);

-- C-6 / Ryan-Spec Bot
-- Ground truth: 30 hand-tagged ideal setups Ryan curated Oct 14-16 2025.
-- These are the contract — the spec must trigger on all 30.
create table if not exists ryan_spec_ground_truth (
  id              bigserial primary key,
  setup_number    int unique not null,
  entry_ts        timestamptz not null,
  direction       text not null,
  csv_entry_price numeric,
  db_entry_price  numeric,
  ohlc_match      boolean,
  notes           text,
  delta_at_entry          int,
  cum_delta_at_entry      int,
  regime_at_entry         text,
  bars_since_session_open int,
  sub_pattern             text,
  captured_features       jsonb,
  matched_track_a         boolean,
  matched_track_b         boolean,
  created_at      timestamptz not null default now()
);

-- Every shadow / backfill trigger from the spec.
create table if not exists ryan_spec_triggers (
  id                 bigserial primary key,
  bar_ts             timestamptz not null,
  track              text,                          -- 'A' (pullback) | 'B' (momentum)
  direction          text not null,
  entry_price        numeric,
  stop_price         numeric,
  target_price       numeric,
  sub_pattern        text,
  matched_conditions jsonb,
  features           jsonb,
  user_tag           text,                       -- 'yes'|'no'|'maybe'|null
  user_tag_ts        timestamptz,
  user_tag_note      text,
  outcome            text,
  exit_ts            timestamptz,
  exit_price         numeric,
  pnl_ticks          int,
  pnl_dollars        numeric,
  mfe_atr            numeric,
  mae_atr            numeric,
  bars_held          int,
  created_at         timestamptz not null default now()
);
create index if not exists ryan_spec_triggers_bar_ts_idx  on ryan_spec_triggers (bar_ts desc);
create index if not exists ryan_spec_triggers_outcome_idx on ryan_spec_triggers (outcome);
create index if not exists ryan_spec_triggers_track_idx   on ryan_spec_triggers (track);

-- Per-spec-version regression test results.
create table if not exists ryan_spec_regression (
  id              bigserial primary key,
  spec_version    text not null,
  spec_hash       text not null,
  pass_count      int,
  missed_setups   int[],
  failure_reasons jsonb,
  run_at          timestamptz not null default now()
);
create index if not exists ryan_spec_regression_run_at_idx on ryan_spec_regression (run_at desc);

-- Ryan-Spec OOS-v3 paper / live trades. Each row = one round-trip trade.
-- mode flips from 'paper' → 'live' once promotion gate passes.
create table if not exists ryan_spec_v3_trades (
  id                  bigserial primary key,
  mode                text not null,                 -- 'paper' | 'live' | 'shadow'
  bar_ts              timestamptz not null,          -- 2m bar that fired the trigger
  direction           text not null,                 -- 'long' | 'short'
  entry_ts            timestamptz,
  entry_price         numeric,                       -- realized fill
  stop_price          numeric,                       -- working resting stop
  cum_delta_at_entry  int,
  atr_at_entry        numeric,
  -- outcome (filled when trade closes)
  exit_ts             timestamptz,
  exit_price          numeric,
  exit_reason         text,                          -- 'stop' | 'opposite_signal' | 'session_end' | 'time_stop'
  pnl_dollars         numeric,
  bars_held           int,
  mfe_atr             numeric,
  mae_atr             numeric,
  slippage_ticks      int,                           -- realized vs modeled (1 tick adverse)
  created_at          timestamptz not null default now()
);
create index if not exists ryan_spec_v3_trades_mode_bar_ts_idx
  on ryan_spec_v3_trades (mode, bar_ts desc);
create index if not exists ryan_spec_v3_trades_exit_reason_idx
  on ryan_spec_v3_trades (exit_reason);

-- Runtime heartbeat (one row per service, upserted each bar by the runtime).
create table if not exists runtime_heartbeats (
  service             text primary key,
  ts                  timestamptz not null,
  last_bar_ts         timestamptz,
  auth_ok             boolean not null,
  consecutive_errors  int not null default 0,
  position_state      text not null,
  extra               jsonb
);

-- Remote kill-switch + circuit breaker config (one row per service).
create table if not exists runtime_config (
  service                  text primary key,
  paused                   boolean not null default false,
  max_consecutive_errors   int not null default 3,
  updated_at               timestamptz not null default now(),
  updated_by               text
);
insert into runtime_config (service, paused, max_consecutive_errors)
values ('ryan_spec_v3', false, 3)
on conflict (service) do nothing;
"""
