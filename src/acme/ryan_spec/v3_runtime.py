"""Standalone async runtime for Ryan-Spec OOS-v3.

Connects to a configured broker (paper-mode), streams quotes, builds 2m bars
with cum-delta via tick rule, drives the v3 engine, places orders, logs to
Supabase. No conductor dependency.

Env vars (loaded from .env at startup):
  ACME_BROKER             paper | projectx        (default paper)
  ACME_CONTRACT_SYMBOL    e.g. MES                 (default MES)
  ACME_MODE               paper | live | shadow    (default paper)
  ACME_DELTA_SOURCE       quote | trade            (default quote)
  ACME_RISK_CONTRACTS     int                      (default 1)
  ACME_SESSION_OPEN_CT    HH:MM in America/Chicago (default 08:30)
  ACME_SESSION_END_CT     HH:MM in America/Chicago (default 14:50)

  PROJECTX_USERNAME       ProjectX username        (required if projectx)
  PROJECTX_API_KEY        ProjectX API key         (required if projectx)
  PROJECTX_ACCOUNT_ID     optional, falls back to lookup
  SUPABASE_URL            (required)
  SUPABASE_SERVICE_ROLE_KEY (required)

Run:
  uv run python -m acme.ryan_spec.v3_runtime
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from datetime import time as dtime
from typing import Any, Literal

import structlog
from dotenv import load_dotenv

from acme.broker.base import (
    Bar,  # noqa: TC001  used as a runtime type hint string
    BracketSpec,
    BrokerAdapter,
)
from acme.contracts import MES
from acme.db import Db
from acme.ryan_spec.v3_engine import (
    FILTER_THRESH_SIZE_WEIGHTED,
    FILTER_THRESH_UNIT_WEIGHTED,
    SESSION_END_CT,
    Decision,
    Direction,
    RyanSpecV3Engine,
)
from acme.ryan_spec.v3_tick_delta import (
    CT,
    SESSION_OPEN_CT,
    BarWithDelta,
    LiveBarDeltaBuilder,
)

log = structlog.get_logger(__name__)

# Round-turn commission per contract (MES). Matches what was previously
# hardcoded in _close(); centralised so entry and exit reconciliation share
# the same number.
ROUND_TURN_COMMISSION_DOLLARS = 0.70

# Service identifier for runtime_heartbeats / runtime_config rows.
SERVICE_NAME = "ryan_spec_v3"
# How long to cache the runtime_config row before re-fetching from Supabase.
CONFIG_CACHE_TTL_SECONDS = 30.0


def _env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name)
    return v if v is not None and v != "" else (default if default is not None else "")


def _parse_hhmm(value: str, *, field_name: str) -> dtime:
    """Parse 'HH:MM' (or 'HHMM') into a naive time. Raises SystemExit on bad input."""
    s = value.strip().replace(":", "")
    if len(s) != 4 or not s.isdigit():
        raise SystemExit(f"Invalid {field_name}={value!r}; expected HH:MM (e.g. 08:30)")
    hh, mm = int(s[:2]), int(s[2:])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise SystemExit(f"Invalid {field_name}={value!r}; HH must be 0-23, MM 0-59")
    return dtime(hh, mm)


@dataclass(frozen=True)
class _PendingEntry:
    """An entry order awaiting its broker fill confirmation."""
    row_id: int
    direction: Direction
    modeled_price: float


@dataclass(frozen=True)
class _PendingExit:
    """An exit order awaiting its broker fill confirmation.

    `entry_fill` is captured at exit-submit time; if the entry fill arrives
    *after* the exit submits (rare but possible network race), this snapshot
    will use the modeled entry, not the real one. The DB's entry_price is
    patched independently — only pnl_dollars carries this small risk.
    """
    row_id: int
    sign: int            # +1 for long position, -1 for short
    entry_fill: float
    commission_dollars: float


def compute_entry_slippage_ticks(
    direction: Direction, modeled_price: float, fill_price: float, tick_size: float
) -> int:
    """Signed entry slippage in ticks. Positive = adverse (worse fill).

    Long: positive when fill > modeled (paid more).
    Short: positive when fill < modeled (received less).
    """
    sign = 1 if direction == "long" else -1
    return int(round((fill_price - modeled_price) * sign / tick_size))


def compute_realized_pnl_dollars(
    sign: int, entry_fill: float, exit_fill: float,
    point_value: float, commission_dollars: float,
) -> float:
    """Realized P&L in dollars: (exit - entry) * sign * point_value - commission."""
    return (exit_fill - entry_fill) * sign * point_value - commission_dollars


def _build_broker() -> BrokerAdapter:
    kind = _env("ACME_BROKER", "paper").lower()
    if kind == "paper":
        from acme.broker.paper import PaperAdapter
        return PaperAdapter()  # type: ignore[return-value]
    if kind == "projectx":
        from acme.broker.projectx import ProjectXAdapter
        # ProjectXAdapter reads PROJECTX_USERNAME / PROJECTX_API_KEY from env
        return ProjectXAdapter()
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
        session_open_ct: dtime = SESSION_OPEN_CT,
        session_end_ct: dtime = SESSION_END_CT,
        session_tz: tzinfo = CT,
        dry_run: bool = False,
        # Multi-variant: each runtime instance writes trade rows tagged with
        # its strategy_id and reads its own runtime_config / heartbeat row
        # under the same key. Default 'v3-canon' = the canonical OOS-validated
        # configuration; variants override engine flags below to ship as
        # 'v3-trail' / 'v3-min2bar' / 'v3-armor' / 'v3-pctile'.
        strategy_id: str = "v3-canon",
        # Engine variant overrides — passed through to RyanSpecV3Engine.
        # Defaulting all to canon-equivalent keeps backward compat.
        enable_trailing_stop: bool = False,
        trail_be_lock_atr_mult: float = 1.0,
        trail_atr_mult: float = 1.0,
        min_bars_before_opposite_exit: int = 0,
        opposite_signal_armor_mfe_atr: float | None = None,
        filter_mode: Literal["static", "pctile"] = "static",
        filter_pctile_window_bars: int = 60,
        filter_pctile: float = 5.0,
        filter_pctile_short: float | None = None,
        # Time-of-day exits. Default True keeps OOS-validated behavior. Set
        # both False for 24-hour shadow runs where exits should be pure
        # thesis (stop / opposite_signal) only.
        enable_session_end_exit: bool = True,
        enable_time_stop: bool = True,
        # v4 regime-aware engine. Default None = bare RyanSpecV3Engine (no
        # behavior change). When set, the runtime wraps the engine in a
        # V4GatedEngine that consults the classifier on every bar and gates
        # entries by regime. Backward-compatible: every existing v3 variant
        # leaves these as None and behaves identically to before PR #B.
        regime_classifier: Callable[[Sequence[Bar]], str] | None = None,
        regime_gate_mode: Literal["gate", "flip"] = "gate",
        # How many bars the wrapper retains for regime classification.
        # 60 (default) covers EMA(20) + 10-bar lookback. Variants that need
        # deeper history (e.g. overnight bias = 360 bars ~= 12 h of 2-min
        # bars; vol-regime baseline = 120 bars) override this per-variant.
        regime_history_bars: int = 60,
    ) -> None:
        self.broker = broker
        self.db = db
        self.contract_symbol = contract_symbol
        self.mode = mode
        self.delta_source = delta_source
        self.risk_contracts = risk_contracts
        self.strategy_id = strategy_id
        # Phantom-fill mode: skip submit_market_order entirely; record trade
        # rows at the modeled price. Used when running alongside other bots on
        # one shared broker connection so we can paper-trade all of them
        # against live market data without competing for real margin.
        self.dry_run = dry_run
        # Pick the engine's filter threshold based on delta source.
        # quote → unit-weighted (recalibrated to -670 vs OOS-validated -2000)
        # trade → size-weighted (matches OOS exactly)
        thresh = (FILTER_THRESH_UNIT_WEIGHTED if delta_source == "quote"
                  else FILTER_THRESH_SIZE_WEIGHTED)
        engine_kwargs = dict(
            filter_thresh=thresh,
            session_end_ct=session_end_ct,
            session_tz=session_tz,
            enable_trailing_stop=enable_trailing_stop,
            trail_be_lock_atr_mult=trail_be_lock_atr_mult,
            trail_atr_mult=trail_atr_mult,
            min_bars_before_opposite_exit=min_bars_before_opposite_exit,
            opposite_signal_armor_mfe_atr=opposite_signal_armor_mfe_atr,
            filter_mode=filter_mode,
            filter_pctile_window_bars=filter_pctile_window_bars,
            filter_pctile=filter_pctile,
            filter_pctile_short=filter_pctile_short,
            enable_session_end_exit=enable_session_end_exit,
            enable_time_stop=enable_time_stop,
        )
        if regime_classifier is None:
            self.engine = RyanSpecV3Engine(**engine_kwargs)
        else:
            # Wrap in v4 regime-aware engine. Same on_bar interface, so the
            # rest of the runtime treats it identically.
            from .v4_engine import V4GatedEngine
            self.engine = V4GatedEngine(
                classifier=regime_classifier,
                gate_mode=regime_gate_mode,
                regime_history_bars=regime_history_bars,
                **engine_kwargs,
            )
        self.builder = LiveBarDeltaBuilder(
            on_bar=self._on_closed_bar,
            session_open_ct=session_open_ct,
            session_tz=session_tz,
        )
        self._contract_id: str | None = None
        self._open_trade_id: int | None = None
        self._open_position_meta: dict[str, Any] | None = None
        self._pending_entry_fills: dict[str, _PendingEntry] = {}
        self._pending_exit_fills: dict[str, _PendingExit] = {}
        self._user_events_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        # Auth status flips to True after broker.authenticate + get_account
        # succeed in run(). Heartbeat surfaces this so a stale auth shows up.
        self._auth_ok: bool = False
        # Last 2m bar timestamp the runtime processed; surfaced in heartbeat.
        self._last_bar_ts: datetime | None = None
        # Consecutive broker-error counter for the self-halt circuit breaker.
        # Reset to 0 on any successful submit_market_order.
        self._consecutive_errors: int = 0
        # Runtime_config cache. Refreshed every CONFIG_CACHE_TTL_SECONDS so
        # operator pause-toggles propagate within ~30s without DB-hammering.
        self._config_cache: dict[str, Any] = {
            "paused": False, "max_consecutive_errors": 3,
        }
        self._config_last_fetch: datetime | None = None

    async def run(self) -> None:
        log.info("v3_runtime_starting", mode=self.mode,
                 delta_source=self.delta_source, contract=self.contract_symbol,
                 broker=type(self.broker).__name__)
        await self.broker.authenticate()
        # Resolve the trading account before placing orders. ProjectXAdapter's
        # account_id property raises until get_account() has populated it (or
        # PROJECTX_ACCOUNT_ID is set in env), which would surface here as
        # broker_error on every entry.
        account = await self.broker.get_account()
        log.info("v3_runtime_account",
                 id=account.get("id") or account.get("Id"),
                 can_trade=account.get("canTrade"))
        self._contract_id = await self.broker.resolve_contract(self.contract_symbol)
        log.info("v3_runtime_contract", id=self._contract_id)

        # Bootstrap the kill-switch cache and surface a startup heartbeat so
        # the watcher sees the bot come alive immediately (don't wait for the
        # first 2m bar to close).
        self._auth_ok = True
        self._refresh_config(force=True)
        self._write_heartbeat()
        log.info("v3_runtime_heartbeat_initial",
                 paused=self._config_cache["paused"],
                 max_consecutive_errors=self._config_cache["max_consecutive_errors"])

        # Set up signal handlers for clean shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._stop_event.set)

        # Reconcile real broker fills onto open trade rows in the background.
        # Skipped in dry_run mode — no real fills will ever arrive and the
        # user-events stream may not be authorised when the broker connection
        # is read-only.
        if not self.dry_run:
            self._user_events_task = asyncio.create_task(self._consume_user_events())

        try:
            if self.delta_source == "trade":
                await self._run_trade_loop()
            else:
                await self._run_quote_loop()
        finally:
            if self._user_events_task is not None:
                self._user_events_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._user_events_task

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
        log.info(
            "v3_runtime_trade_mode",
            filter_thresh=FILTER_THRESH_SIZE_WEIGHTED,
            note=("delta source is TRADE-driven size-weighted aggressor flow. "
                  "Matches OOS methodology exactly — threshold -2000 size-weighted."),
        )
        try:
            async for tr in self.broker.stream_trades(self._contract_id):  # type: ignore[arg-type]
                if self._stop_event.is_set():
                    break
                self.builder.add_trade(
                    tr.t, float(tr.price), int(tr.size), side=tr.side,
                )
        except Exception:
            log.exception("v3_runtime_trade_loop_failed")
            raise

    # ---------- ops: heartbeat + remote kill-switch + circuit breaker ----------

    def _position_state(self) -> str:
        """Compact label for the heartbeat row."""
        if self._open_position_meta is None:
            return "flat"
        return self._open_position_meta.get("direction", "flat")

    def _write_heartbeat(self) -> None:
        """Best-effort heartbeat upsert. Never raises."""
        try:
            self.db.write_heartbeat(
                self.strategy_id,
                last_bar_ts=self._last_bar_ts,
                auth_ok=self._auth_ok,
                consecutive_errors=self._consecutive_errors,
                position_state=self._position_state(),
                extra={
                    "mode": self.mode,
                    "delta_source": self.delta_source,
                    "contract_id": self._contract_id,
                    "broker": type(self.broker).__name__,
                    "open_trade_id": self._open_trade_id,
                    "strategy_id": self.strategy_id,
                },
            )
        except Exception:
            log.exception("v3_runtime_heartbeat_write_failed",
                          strategy_id=self.strategy_id)

    def _refresh_config(self, *, force: bool = False) -> None:
        """Refresh the kill-switch cache from Supabase if stale. Never raises."""
        now = datetime.now(UTC)
        if (not force and self._config_last_fetch is not None
                and (now - self._config_last_fetch).total_seconds() < CONFIG_CACHE_TTL_SECONDS):
            return
        try:
            cfg = self.db.read_runtime_config(self.strategy_id)
            self._config_cache = cfg
            self._config_last_fetch = now
        except Exception:
            log.exception("v3_runtime_config_refresh_failed",
                          strategy_id=self.strategy_id)
            # Keep using whatever was cached; never block trading on this.

    def _is_paused(self) -> bool:
        """True if the runtime should skip new entries (manual or self-halt)."""
        self._refresh_config()
        return bool(self._config_cache.get("paused", False))

    def _maybe_self_halt(self) -> None:
        """If consecutive_errors crossed the threshold, flip paused=true so
        we stop the bleeding without operator intervention. Updates the local
        cache immediately so the very next entry attempt is also skipped."""
        threshold = int(self._config_cache.get("max_consecutive_errors", 3))
        if self._consecutive_errors < threshold:
            return
        log.error("v3_runtime_self_halted",
                  consecutive_errors=self._consecutive_errors,
                  threshold=threshold,
                  note="auto-paused after consecutive broker errors; "
                       "investigate then unpause via Supabase Studio "
                       "(update runtime_config set paused=false ...).")
        try:
            self.db.set_runtime_paused(self.strategy_id, paused=True, by="self_halt")
        except Exception:
            log.exception("v3_runtime_self_halt_db_write_failed",
                          strategy_id=self.strategy_id)
        self._config_cache["paused"] = True
        self._config_last_fetch = datetime.now(UTC)

    # ---------- engine integration ----------

    def _on_closed_bar(self, bd: BarWithDelta) -> None:
        self._last_bar_ts = bd.bar.t
        decision = self.engine.on_bar(
            bd.bar,
            bar_delta=bd.delta,
            cum_delta_session=bd.cum_delta_session,
        )
        # Always heartbeat at bar close so the watcher knows we're alive
        # even on no-decision bars.
        self._write_heartbeat()
        if decision.action == "none":
            return
        # Hand off to async dispatch
        asyncio.create_task(self._dispatch_decision(decision, bd))

    async def _dispatch_decision(
        self, decision: Decision, bd: BarWithDelta
    ) -> None:
        try:
            if decision.action == "enter" and self._open_trade_id is None:
                # Remote kill-switch: skip new entries when paused, but never
                # block exits — open positions must always be allowed to flatten.
                if self._is_paused():
                    log.info("v3_runtime_skipped_remote_paused",
                             direction=decision.direction,
                             reason=decision.reason,
                             bar_ts=bd.bar.t.isoformat())
                    return
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
            "strategy_id": self.strategy_id,
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

        if self.dry_run:
            # Phantom fill at the modeled price. Mark slippage_ticks=0 so the
            # promotion gate's slippage stat distinguishes phantom rows from
            # real fills it hasn't yet measured.
            log.info("v3_runtime_open_phantom",
                     row_id=row_id, entry=decision.entry_price)
            self.db.update_ryan_spec_v3_trade(row_id, {"slippage_ticks": 0})
            self._consecutive_errors = 0
        else:
            # Submit market entry + resting stop bracket
            try:
                order_id = await self.broker.submit_market_order(
                    self._contract_id,  # type: ignore[arg-type]
                    side,  # type: ignore[arg-type]
                    self.risk_contracts,
                    custom_tag=f"{self.strategy_id}:{row_id}:in",
                    bracket=BracketSpec(
                        stop_loss_offset_ticks=stop_ticks,
                        take_profit_offset_ticks=None,
                    ),
                )
                log.info("v3_runtime_open_submitted", order_id=order_id, row_id=row_id)
                # Successful submit clears the broker-error streak.
                self._consecutive_errors = 0
            except Exception:
                log.exception("v3_runtime_open_failed")
                # Mark the trade row as broker-error so the row isn't a phantom
                self.db.update_ryan_spec_v3_trade(row_id, {
                    "exit_reason": "broker_error",
                    "exit_ts": datetime.now(UTC).isoformat(),
                })
                # Circuit breaker: count this error and self-halt if we've crossed
                # the configured threshold. Heartbeat surfaces the new counter.
                self._consecutive_errors += 1
                self._maybe_self_halt()
                self._write_heartbeat()
                return
            # Register the order so _consume_user_events can attribute the fill
            # back to this row even if the position is closed before fill arrives.
            self._pending_entry_fills[str(order_id)] = _PendingEntry(
                row_id=row_id,
                direction=decision.direction,
                modeled_price=float(decision.entry_price),
            )

        self._open_trade_id = row_id
        self._open_position_meta = {
            "direction": decision.direction,
            "entry_fill": float(decision.entry_price),  # provisional; patched on real fill
            "atr": float(decision.atr_at_entry),
            "cum_delta": int(decision.cum_delta_at_entry or 0),
            "entry_ts": datetime.now(UTC),
            "sign": sign,
        }
        # Notify engine of the recorded open. Real fill arrives async via
        # _process_user_event, which patches the DB row + meta entry_fill.
        # Engine state isn't re-seeded — its stop_price drift vs the broker
        # bracket is bounded by entry slippage, which we measure separately.
        self.engine.open_position(
            direction=decision.direction,
            entry_ts=datetime.now(UTC),
            entry_fill_price=float(decision.entry_price),
            atr_at_entry=float(decision.atr_at_entry),
            cum_delta_at_entry=int(decision.cum_delta_at_entry or 0),
        )

    # ---------- broker fill reconciliation ----------

    async def _consume_user_events(self) -> None:
        """Background loop: stream broker user events, reconcile fills."""
        try:
            async for evt in self.broker.stream_user_events():
                if self._stop_event.is_set():
                    break
                try:
                    await self._process_user_event(evt)
                except Exception:
                    log.exception("v3_runtime_user_event_failed",
                                  evt_kind=(evt or {}).get("kind"))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("v3_runtime_user_event_loop_failed")

    async def _process_user_event(self, evt: dict) -> None:
        """Handle one event from broker.stream_user_events().

        Public-ish for tests — covers entry- and exit-fill matching by
        orderId (primary) or customTag (fallback for shapes that omit
        orderId). Entry fills are tried first, then exits.
        """
        if (evt or {}).get("kind") != "fill":
            return
        payload = evt.get("payload") or {}
        order_id = str(payload.get("orderId") or payload.get("OrderId") or "")
        tag = str(payload.get("customTag") or payload.get("CustomTag") or "")

        pending_entry = (
            self._pending_entry_fills.pop(order_id, None) if order_id else None
        )
        if pending_entry is None:
            pending_entry = self._match_entry_pending_by_tag(tag)
        if pending_entry is not None:
            fill_price = self._extract_fill_price(payload, pending_entry.row_id)
            if fill_price is not None:
                self._record_entry_fill(pending_entry, fill_price)
            return

        pending_exit = (
            self._pending_exit_fills.pop(order_id, None) if order_id else None
        )
        if pending_exit is None:
            pending_exit = self._match_exit_pending_by_tag(tag)
        if pending_exit is not None:
            fill_price = self._extract_fill_price(payload, pending_exit.row_id)
            if fill_price is not None:
                self._record_exit_fill(pending_exit, fill_price)

    @staticmethod
    def _extract_fill_price(payload: dict, row_id: int) -> float | None:
        price = payload.get("price") or payload.get("Price")
        if price is None:
            log.error("v3_runtime_fill_missing_price",
                      row_id=row_id, payload_keys=list(payload.keys()))
            return None
        return float(price)

    def _match_entry_pending_by_tag(self, tag: str) -> _PendingEntry | None:
        prefix = f"{self.strategy_id}:"
        if not tag.startswith(prefix) or not tag.endswith(":in"):
            return None
        for order_id, pending in list(self._pending_entry_fills.items()):
            if f"{self.strategy_id}:{pending.row_id}:in" == tag:
                self._pending_entry_fills.pop(order_id, None)
                return pending
        return None

    def _match_exit_pending_by_tag(self, tag: str) -> _PendingExit | None:
        prefix = f"{self.strategy_id}:"
        if not tag.startswith(prefix) or not tag.endswith(":out"):
            return None
        for order_id, pending in list(self._pending_exit_fills.items()):
            if f"{self.strategy_id}:{pending.row_id}:out" == tag:
                self._pending_exit_fills.pop(order_id, None)
                return pending
        return None

    def _record_entry_fill(self, pending: _PendingEntry, fill_price: float) -> None:
        """Patch DB row + runtime meta with the real entry fill."""
        slippage = compute_entry_slippage_ticks(
            pending.direction, pending.modeled_price, fill_price, MES.tick_size
        )
        self.db.update_ryan_spec_v3_trade(pending.row_id, {
            "entry_price": fill_price,
            "slippage_ticks": slippage,
        })
        # If the position is still open and matches, update the meta so the
        # eventual close P&L is computed against the real fill.
        if (
            self._open_trade_id == pending.row_id
            and self._open_position_meta is not None
        ):
            self._open_position_meta["entry_fill"] = fill_price
        log.info("v3_runtime_entry_fill_reconciled",
                 row_id=pending.row_id,
                 direction=pending.direction,
                 modeled=pending.modeled_price,
                 fill=fill_price,
                 slippage_ticks=slippage)

    def _record_exit_fill(self, pending: _PendingExit, fill_price: float) -> None:
        """Patch DB row with the real exit fill and recomputed P&L."""
        pnl_dollars = compute_realized_pnl_dollars(
            sign=pending.sign,
            entry_fill=pending.entry_fill,
            exit_fill=fill_price,
            point_value=MES.point_value,
            commission_dollars=pending.commission_dollars,
        )
        self.db.update_ryan_spec_v3_trade(pending.row_id, {
            "exit_price": fill_price,
            "pnl_dollars": float(pnl_dollars),
        })
        log.info("v3_runtime_exit_fill_reconciled",
                 row_id=pending.row_id,
                 entry_fill=pending.entry_fill,
                 exit_fill=fill_price,
                 pnl_dollars=pnl_dollars)

    async def _close(self, decision: Decision, bd: BarWithDelta) -> None:
        if self._open_trade_id is None or self._open_position_meta is None:
            return
        log.info("v3_runtime_close_attempt", reason=decision.reason)
        meta = self._open_position_meta
        row_id = self._open_trade_id
        sign = meta["sign"]
        side = "sell" if meta["direction"] == "long" else "buy"

        # Submit a tagged opposite-side market order so we can attribute the
        # close fill back to this row when the broker confirms it.
        # In dry_run we skip the submit entirely — bar.c is the phantom fill.
        order_id: str | None = None
        if self.dry_run:
            log.info("v3_runtime_close_phantom", row_id=row_id, exit=float(bd.bar.c))
        else:
            try:
                order_id = await self.broker.submit_market_order(
                    self._contract_id,  # type: ignore[arg-type]
                    side,  # type: ignore[arg-type]
                    self.risk_contracts,
                    custom_tag=f"{self.strategy_id}:{row_id}:out",
                )
                log.info("v3_runtime_close_submitted", order_id=order_id, row_id=row_id)
            except Exception:
                log.exception("v3_runtime_close_failed")
                # Don't lose the row — record the attempted exit with bar.c below.

        # Provisional exit using bar.c. Real fill arrives async via
        # _process_user_event → _record_exit_fill which patches exit_price
        # and pnl_dollars with the broker-confirmed values.
        exit_price = float(bd.bar.c)
        pnl_dollars = compute_realized_pnl_dollars(
            sign=sign,
            entry_fill=float(meta["entry_fill"]),
            exit_fill=exit_price,
            point_value=MES.point_value,
            commission_dollars=ROUND_TURN_COMMISSION_DOLLARS,
        )
        held_seconds = (datetime.now(UTC) - meta["entry_ts"]).total_seconds()
        bars_held = max(1, int(held_seconds / 120))

        # Capture MFE/MAE from the engine while pos still exists.
        # `engine.close_position()` below clears it. Use the engine's own
        # atr_at_entry as the normalising denominator so the units stay
        # consistent with how MFE/MAE were computed (per-bar updates use
        # the same atr the engine recorded at entry).
        mfe_atr: float | None = None
        mae_atr: float | None = None
        epos = self.engine.position
        if epos is not None and epos.atr_at_entry > 0:
            atr_e = epos.atr_at_entry
            # Schema says mfe_atr / mae_atr are `numeric` — store fractional ATR
            # units for precision. Round to 2 decimals to keep rows tidy.
            mfe_atr = round(epos.max_favorable_excursion / atr_e, 2)
            mae_atr = round(epos.max_adverse_excursion / atr_e, 2)

        update_fields: dict[str, Any] = {
            "exit_ts": datetime.now(UTC).isoformat(),
            "exit_price": exit_price,
            "exit_reason": decision.reason,
            "pnl_dollars": float(pnl_dollars),
            "bars_held": bars_held,
        }
        if mfe_atr is not None:
            update_fields["mfe_atr"] = mfe_atr
        if mae_atr is not None:
            update_fields["mae_atr"] = mae_atr
        self.db.update_ryan_spec_v3_trade(row_id, update_fields)

        if order_id is not None:
            self._pending_exit_fills[str(order_id)] = _PendingExit(
                row_id=row_id,
                sign=sign,
                entry_fill=float(meta["entry_fill"]),
                commission_dollars=ROUND_TURN_COMMISSION_DOLLARS,
            )

        self.engine.close_position()
        self._open_trade_id = None
        self._open_position_meta = None


def main() -> None:
    # Load .env from project root (PROJECTX creds, SUPABASE_*, etc.)
    load_dotenv()
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
    session_open = _parse_hhmm(
        _env("ACME_SESSION_OPEN_CT", "08:30"), field_name="ACME_SESSION_OPEN_CT",
    )
    session_end = _parse_hhmm(
        _env("ACME_SESSION_END_CT", "14:50"), field_name="ACME_SESSION_END_CT",
    )
    dry_run = _env("ACME_V3_DRY_RUN", "false").lower() in ("1", "true", "yes")
    strategy_id = _env("ACME_STRATEGY_ID", "v3-canon")
    runtime = V3Runtime(
        broker=broker, db=db, contract_symbol=contract,
        mode=mode, delta_source=delta_source,  # type: ignore[arg-type]
        risk_contracts=risk,
        session_open_ct=session_open,
        session_end_ct=session_end,
        dry_run=dry_run,
        strategy_id=strategy_id,
    )
    asyncio.run(runtime.run())


if __name__ == "__main__":
    main()
