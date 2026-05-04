"""Standalone async runtime for Ryan-Spec OOS-v3.

Designed to run identically Mac-local OR on Railway as a worker process.
Connects to a configured broker (paper-mode), streams quotes, builds 2m bars
with cum-delta via tick rule, drives the v3 engine, places orders, logs to
Supabase. No conductor dependency.

Env vars (read at startup):
  ACME_BROKER             paper | projectx        (default paper)
  ACME_PROJECTX_USER      ProjectX username        (required if projectx)
  ACME_PROJECTX_API_KEY   ProjectX API key         (required if projectx)
  ACME_CONTRACT_SYMBOL    e.g. MES                 (default MES)
  ACME_MODE               paper | live | shadow    (default paper)
  ACME_DELTA_SOURCE       quote | trade            (default quote)
  ACME_RISK_CONTRACTS     int                      (default 1)

Run locally:
  uv run python -m acme.ryan_spec.v3_runtime

Run on Railway:
  Procfile: `worker: uv run python -m acme.ryan_spec.v3_runtime`
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from datetime import UTC, datetime
from typing import Any, Literal

import structlog

from acme.broker.base import BracketSpec, BrokerAdapter
from acme.contracts import MES
from acme.db import Db
from acme.ryan_spec.v3_engine import (
    FILTER_THRESH_SIZE_WEIGHTED,
    FILTER_THRESH_UNIT_WEIGHTED,
    Decision,
    RyanSpecV3Engine,
)
from acme.ryan_spec.v3_tick_delta import BarWithDelta, LiveBarDeltaBuilder

log = structlog.get_logger(__name__)


def _env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name)
    return v if v is not None and v != "" else (default if default is not None else "")


def _build_broker() -> BrokerAdapter:
    kind = _env("ACME_BROKER", "paper").lower()
    if kind == "paper":
        from acme.broker.paper import PaperAdapter
        return PaperAdapter()  # type: ignore[return-value]
    if kind == "projectx":
        from acme.broker.projectx import ProjectXAdapter
        user = _env("ACME_PROJECTX_USER")
        api_key = _env("ACME_PROJECTX_API_KEY")
        if not user or not api_key:
            raise SystemExit("ACME_PROJECTX_USER and ACME_PROJECTX_API_KEY "
                             "required for projectx broker.")
        return ProjectXAdapter(username=user, api_key=api_key)
    raise SystemExit(f"Unknown ACME_BROKER={kind!r}")


class V3Runtime:
    """Standalone runtime — connects, streams, decides, orders, logs."""

    def __init__(
        self,
        *,
        broker: BrokerAdapter,
        db: Db,
        contract_symbol: str,
        mode: Literal["paper", "live", "shadow"] = "paper",
        delta_source: Literal["quote", "trade"] = "quote",
        risk_contracts: int = 1,
    ) -> None:
        self.broker = broker
        self.db = db
        self.contract_symbol = contract_symbol
        self.mode = mode
        self.delta_source = delta_source
        self.risk_contracts = risk_contracts
        # Pick the engine's filter threshold based on delta source.
        # quote → unit-weighted (recalibrated to -670 vs OOS-validated -2000)
        # trade → size-weighted (matches OOS exactly)
        thresh = (FILTER_THRESH_UNIT_WEIGHTED if delta_source == "quote"
                  else FILTER_THRESH_SIZE_WEIGHTED)
        self.engine = RyanSpecV3Engine(filter_thresh=thresh)
        self.builder = LiveBarDeltaBuilder(on_bar=self._on_closed_bar)
        self._contract_id: str | None = None
        self._open_trade_id: int | None = None
        self._open_position_meta: dict[str, Any] | None = None
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        log.info("v3_runtime_starting", mode=self.mode,
                 delta_source=self.delta_source, contract=self.contract_symbol,
                 broker=type(self.broker).__name__)
        await self.broker.authenticate()
        self._contract_id = await self.broker.resolve_contract(self.contract_symbol)
        log.info("v3_runtime_contract", id=self._contract_id)

        # Set up signal handlers for clean shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._stop_event.set)

        if self.delta_source == "trade":
            await self._run_trade_loop()
        else:
            await self._run_quote_loop()

        # Drain final partial bar (best-effort)
        self.builder.force_close_current()
        log.info("v3_runtime_stopped")

    async def _run_quote_loop(self) -> None:
        log.info(
            "v3_runtime_quote_mode",
            filter_thresh=FILTER_THRESH_UNIT_WEIGHTED,
            note=("delta source is QUOTE-driven unit-weight tick rule. "
                  "Recalibrated against 78-day OOS data: PF 2.21 vs OOS 2.30 "
                  "(96%). Threshold -670 unit-weight ≈ -2000 size-weighted. "
                  "Will fire on ~89 trades/day if live quote frequency "
                  "matches cached trade frequency."),
        )
        try:
            async for q in self.broker.stream_quotes(self._contract_id):  # type: ignore[arg-type]
                if self._stop_event.is_set():
                    break
                price = q.last or q.bid or q.ask
                if price is None:
                    continue
                self.builder.add_quote_tick(q.t, float(price))
        except Exception:
            log.exception("v3_runtime_quote_loop_failed")
            raise

    async def _run_trade_loop(self) -> None:
        # The BrokerAdapter Protocol does not currently define stream_trades.
        # Caller must extend the adapter to expose a (t, price, size, side)
        # async iterator. This wiring is intentionally explicit so we don't
        # silently fall back to lower-fidelity quote tick rule.
        raise NotImplementedError(
            "delta_source='trade' requires broker.stream_trades(contract_id). "
            "Extend the BrokerAdapter Protocol + ProjectXAdapter to subscribe "
            "to the trades hub event (likely 'GatewayTrade'), then replace "
            "this method body with an async-for over that stream calling "
            "builder.add_trade(t, price, size, side=...). See module docstring."
        )

    # ---------- engine integration ----------

    def _on_closed_bar(self, bd: BarWithDelta) -> None:
        decision = self.engine.on_bar(
            bd.bar,
            bar_delta=bd.delta,
            cum_delta_session=bd.cum_delta_session,
        )
        if decision.action == "none":
            return
        # Hand off to async dispatch
        asyncio.create_task(self._dispatch_decision(decision, bd))

    async def _dispatch_decision(
        self, decision: Decision, bd: BarWithDelta
    ) -> None:
        try:
            if decision.action == "enter" and self._open_trade_id is None:
                await self._open(decision, bd)
            elif decision.action == "exit" and self._open_trade_id is not None:
                await self._close(decision, bd)
        except Exception:
            log.exception("v3_runtime_dispatch_failed",
                          action=decision.action, reason=decision.reason)

    async def _open(self, decision: Decision, bd: BarWithDelta) -> None:
        assert decision.direction is not None
        assert decision.entry_price is not None
        assert decision.stop_price is not None
        assert decision.atr_at_entry is not None
        side = "buy" if decision.direction == "long" else "sell"
        sign = 1 if decision.direction == "long" else -1
        # Convert stop distance to ticks for BracketSpec
        stop_dist_pts = abs(decision.entry_price - decision.stop_price)
        stop_ticks = max(1, int(round(stop_dist_pts / MES.tick_size)))
        log.info("v3_runtime_open_attempt",
                 direction=decision.direction, side=side,
                 entry=decision.entry_price, stop=decision.stop_price,
                 cum_delta=decision.cum_delta_at_entry)

        # Insert open trade row first; we'll patch in fill price on confirmation
        row_id = self.db.insert_ryan_spec_v3_trade({
            "mode": self.mode,
            "bar_ts": bd.bar.t.astimezone(UTC).isoformat(),
            "direction": decision.direction,
            "entry_ts": datetime.now(UTC).isoformat(),
            "entry_price": float(decision.entry_price),  # provisional
            "stop_price": float(decision.stop_price),
            "cum_delta_at_entry": int(decision.cum_delta_at_entry or 0),
            "atr_at_entry": float(decision.atr_at_entry),
        })
        if row_id is None:
            log.error("v3_runtime_db_insert_failed_aborting_order")
            return

        # Submit market entry + resting stop bracket
        try:
            order_id = await self.broker.submit_market_order(
                self._contract_id,  # type: ignore[arg-type]
                side,  # type: ignore[arg-type]
                self.risk_contracts,
                custom_tag=f"ryan_spec_v3:{row_id}",
                bracket=BracketSpec(
                    stop_loss_offset_ticks=stop_ticks,
                    take_profit_offset_ticks=None,
                ),
            )
            log.info("v3_runtime_open_submitted", order_id=order_id, row_id=row_id)
        except Exception:
            log.exception("v3_runtime_open_failed")
            # Mark the trade row as broker-error so the row isn't a phantom
            self.db.update_ryan_spec_v3_trade(row_id, {
                "exit_reason": "broker_error",
                "exit_ts": datetime.now(UTC).isoformat(),
            })
            return

        self._open_trade_id = row_id
        self._open_position_meta = {
            "direction": decision.direction,
            "entry_fill": float(decision.entry_price),
            "atr": float(decision.atr_at_entry),
            "cum_delta": int(decision.cum_delta_at_entry or 0),
            "entry_ts": datetime.now(UTC),
            "sign": sign,
        }
        # Notify engine of the recorded open. (Real fill price may differ;
        # the broker callback should call engine.open_position with the actual
        # fill once we have it — for now we use the bar close as a proxy.)
        self.engine.open_position(
            direction=decision.direction,
            entry_ts=datetime.now(UTC),
            entry_fill_price=float(decision.entry_price),
            atr_at_entry=float(decision.atr_at_entry),
            cum_delta_at_entry=int(decision.cum_delta_at_entry or 0),
        )

    async def _close(self, decision: Decision, bd: BarWithDelta) -> None:
        if self._open_trade_id is None or self._open_position_meta is None:
            return
        log.info("v3_runtime_close_attempt", reason=decision.reason)
        # Close via flatten_all (paper); for live this should be per-contract.
        try:
            await self.broker.flatten_all()
        except Exception:
            log.exception("v3_runtime_flatten_failed")
            # Don't lose the row — record the attempted exit anyway
        # Realized P&L is approximate (bar close ≠ exact fill); the broker
        # fill callback should reconcile this. For now we use bar.c.
        meta = self._open_position_meta
        sign = meta["sign"]
        exit_price = float(bd.bar.c)
        pnl_points = (exit_price - meta["entry_fill"]) * sign
        pnl_dollars = pnl_points * MES.point_value - 0.70  # round-turn commission
        held_seconds = (datetime.now(UTC) - meta["entry_ts"]).total_seconds()
        bars_held = max(1, int(held_seconds / 120))
        self.db.update_ryan_spec_v3_trade(self._open_trade_id, {
            "exit_ts": datetime.now(UTC).isoformat(),
            "exit_price": exit_price,
            "exit_reason": decision.reason,
            "pnl_dollars": float(pnl_dollars),
            "bars_held": bars_held,
        })
        self.engine.close_position()
        self._open_trade_id = None
        self._open_position_meta = None


def main() -> None:
    structlog.configure(processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ])
    db = Db()
    broker = _build_broker()
    contract = _env("ACME_CONTRACT_SYMBOL", "MES")
    mode = _env("ACME_MODE", "paper")
    delta_source = _env("ACME_DELTA_SOURCE", "quote")
    risk = int(_env("ACME_RISK_CONTRACTS", "1") or "1")
    if mode not in ("paper", "live", "shadow"):
        raise SystemExit(f"Invalid ACME_MODE={mode!r}")
    if delta_source not in ("quote", "trade"):
        raise SystemExit(f"Invalid ACME_DELTA_SOURCE={delta_source!r}")
    runtime = V3Runtime(
        broker=broker, db=db, contract_symbol=contract,
        mode=mode, delta_source=delta_source,  # type: ignore[arg-type]
        risk_contracts=risk,
    )
    asyncio.run(runtime.run())


if __name__ == "__main__":
    main()
