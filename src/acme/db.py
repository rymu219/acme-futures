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
"""
