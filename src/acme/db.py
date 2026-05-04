"""Supabase event logger. Append-only writes to broker_events; upserts on daily_state."""

from __future__ import annotations

import os
from datetime import date
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
"""
