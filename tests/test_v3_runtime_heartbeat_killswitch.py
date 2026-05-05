"""Tests for v3_runtime ops layer: heartbeat, remote kill-switch, circuit breaker.

We don't drive the full async `run()` loop. Instead we construct a V3Runtime
with fake collaborators, set the minimal state it needs (contract_id, mode),
then call the targeted methods directly. This isolates the ops behaviors
(heartbeat counting, pause-gating, self-halt) from the streaming machinery.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from acme.broker.base import Bar
from acme.ryan_spec.v3_engine import Decision
from acme.ryan_spec.v3_runtime import SERVICE_NAME, V3Runtime
from acme.ryan_spec.v3_tick_delta import BarWithDelta

CT = timezone(timedelta(hours=-6))


class _FakeDb:
    """Records every write the runtime makes; serves a configurable
    runtime_config row. Mirrors the real Db's public surface."""

    def __init__(
        self,
        *,
        runtime_config: dict[str, Any] | None = None,
        config_raises: bool = False,
    ) -> None:
        self._runtime_config = runtime_config or {
            "paused": False, "max_consecutive_errors": 3,
        }
        self._config_raises = config_raises
        self.heartbeats: list[dict[str, Any]] = []
        self.paused_calls: list[dict[str, Any]] = []
        self.trade_inserts: list[dict[str, Any]] = []
        self.trade_updates: list[tuple[int, dict[str, Any]]] = []
        self._next_trade_id = 1

    # --- runtime ops surface ---
    def write_heartbeat(self, service, **kw):
        self.heartbeats.append({"service": service, **kw})

    def read_runtime_config(self, service):
        if self._config_raises:
            raise RuntimeError("simulated supabase outage")
        return dict(self._runtime_config)

    def set_runtime_paused(self, service, *, paused, by):
        self.paused_calls.append({"service": service, "paused": paused, "by": by})
        # Mirror the side-effect a real Supabase row would have on subsequent reads
        self._runtime_config["paused"] = paused

    # --- ryan_spec_v3_trades surface ---
    def insert_ryan_spec_v3_trade(self, row):
        rid = self._next_trade_id
        self._next_trade_id += 1
        self.trade_inserts.append({"id": rid, **row})
        return rid

    def update_ryan_spec_v3_trade(self, trade_id, fields):
        self.trade_updates.append((trade_id, fields))


class _FakeBroker:
    """Records calls; configurable submit_market_order success/failure."""

    def __init__(
        self,
        *,
        submit_raises: bool = False,
        order_id: str = "FAKE-1",
    ) -> None:
        self.submit_raises = submit_raises
        self.order_id = order_id
        self.submit_calls: list[dict[str, Any]] = []
        self.flatten_calls: int = 0

    async def authenticate(self) -> None:
        return None

    async def get_account(self) -> dict:
        return {"id": "FAKE-ACCT-1", "canTrade": True, "balance": 50_000.0}

    async def resolve_contract(self, _symbol: str) -> str:
        return "CON.FAKE.MES"

    async def submit_market_order(
        self, contract_id, side, size, *, custom_tag=None, bracket=None,
    ):
        self.submit_calls.append({
            "contract_id": contract_id, "side": side, "size": size,
            "tag": custom_tag,
        })
        if self.submit_raises:
            raise RuntimeError("simulated broker error")
        return self.order_id

    async def flatten_all(self) -> None:
        self.flatten_calls += 1


def _bd(t: datetime, *, c: float = 5000.0, delta: int = 0,
        cum_delta: int = 0) -> BarWithDelta:
    bar = Bar(t=t, o=c, h=c + 0.25, l=c - 0.25, c=c, v=10)
    return BarWithDelta(bar=bar, delta=delta, cum_delta_session=cum_delta)


def _enter_decision(t: datetime) -> Decision:
    return Decision(
        action="enter", direction="long", reason="oos_v3_signal",
        entry_price=5000.0, stop_price=4995.0, bar_ts=t,
        cum_delta_at_entry=-2500, atr_at_entry=2.0,
    )


def _build_runtime(db: _FakeDb, broker: _FakeBroker) -> V3Runtime:
    rt = V3Runtime(
        broker=broker, db=db, contract_symbol="MES",
        mode="paper", delta_source="quote", risk_contracts=1,
    )
    # Bypass run() — set the bits _open / _dispatch need.
    rt._contract_id = "CON.FAKE.MES"
    rt._auth_ok = True
    return rt


# ---------- heartbeat ----------

def test_heartbeat_written_on_each_closed_bar():
    db = _FakeDb()
    rt = _build_runtime(db, _FakeBroker())
    t0 = datetime(2026, 5, 5, 9, 0, tzinfo=CT)
    # Three flat bars — engine returns "none" for all (warmup) so no dispatch.
    for i in range(3):
        rt._on_closed_bar(_bd(t0 + timedelta(minutes=2 * i)))
    assert len(db.heartbeats) == 3
    assert all(hb["service"] == SERVICE_NAME for hb in db.heartbeats)
    # last_bar_ts on the third heartbeat matches the third bar
    assert db.heartbeats[-1]["last_bar_ts"] == t0 + timedelta(minutes=4)
    # auth_ok and position_state surfaced
    assert db.heartbeats[-1]["auth_ok"] is True
    assert db.heartbeats[-1]["position_state"] == "flat"


# ---------- remote kill-switch ----------

def test_runtime_paused_skips_entry_calls_open():
    """When runtime_config.paused=true, an enter decision must NOT call
    submit_market_order. No trade row is even inserted."""
    db = _FakeDb(runtime_config={"paused": True, "max_consecutive_errors": 3})
    broker = _FakeBroker()
    rt = _build_runtime(db, broker)

    decision = _enter_decision(datetime(2026, 5, 5, 9, 0, tzinfo=CT))
    bd = _bd(decision.bar_ts)
    asyncio.run(rt._dispatch_decision(decision, bd))

    assert broker.submit_calls == []
    assert db.trade_inserts == []   # row insert is inside _open(), also skipped


def test_runtime_paused_still_processes_exits():
    """A pause must NEVER block exits — open positions must always be allowed
    to flatten. Otherwise a pause could strand a live position."""
    db = _FakeDb(runtime_config={"paused": True, "max_consecutive_errors": 3})
    broker = _FakeBroker()
    rt = _build_runtime(db, broker)
    # Pretend we already have an open position
    rt._open_trade_id = 42
    rt._open_position_meta = {
        "direction": "long", "entry_fill": 5000.0, "atr": 2.0,
        "cum_delta": -2500, "entry_ts": datetime.now(UTC), "sign": 1,
    }
    exit_decision = Decision(
        action="exit", reason="stop",
        bar_ts=datetime(2026, 5, 5, 9, 0, tzinfo=CT),
    )
    bd = _bd(exit_decision.bar_ts, c=4995.0)
    asyncio.run(rt._dispatch_decision(exit_decision, bd))

    # Exits go through a tagged opposite-side market order so the close fill
    # can be attributed back to this row by _record_exit_fill.
    assert len(broker.submit_calls) == 1
    assert broker.submit_calls[0]["side"] == "sell"  # closing a long
    assert broker.submit_calls[0]["tag"] == "ryan_spec_v3:42:out"
    # Position state cleared after close
    assert rt._open_trade_id is None


# ---------- circuit breaker ----------

def test_consecutive_broker_errors_self_halt_after_threshold():
    """Three consecutive broker errors must auto-flip paused=true via
    set_runtime_paused(by='self_halt')."""
    db = _FakeDb(runtime_config={"paused": False, "max_consecutive_errors": 3})
    broker = _FakeBroker(submit_raises=True)
    rt = _build_runtime(db, broker)

    decision = _enter_decision(datetime(2026, 5, 5, 9, 0, tzinfo=CT))
    bd = _bd(decision.bar_ts)
    for _ in range(3):
        asyncio.run(rt._dispatch_decision(decision, bd))

    # Should have written exactly one self_halt record after the 3rd failure
    halts = [c for c in db.paused_calls if c["by"] == "self_halt"]
    assert len(halts) == 1
    assert halts[0]["paused"] is True
    # And the local cache is flipped, so a 4th attempt is also skipped
    asyncio.run(rt._dispatch_decision(decision, bd))
    # Only 3 broker submit attempts — the 4th was gated by the now-paused cache
    assert len(broker.submit_calls) == 3


def test_successful_open_resets_consecutive_errors():
    """One error then a success should leave _consecutive_errors at 0,
    so a subsequent error doesn't push us over the threshold prematurely."""
    db = _FakeDb()
    broker = _FakeBroker(submit_raises=True)
    rt = _build_runtime(db, broker)

    decision = _enter_decision(datetime(2026, 5, 5, 9, 0, tzinfo=CT))
    bd = _bd(decision.bar_ts)
    asyncio.run(rt._dispatch_decision(decision, bd))
    assert rt._consecutive_errors == 1

    # Now succeed
    broker.submit_raises = False
    # Engine considers us flat because _open's exception path didn't set
    # _open_trade_id; clear engine state too so the new enter is processable
    rt._open_trade_id = None
    asyncio.run(rt._dispatch_decision(decision, bd))
    assert rt._consecutive_errors == 0


# ---------- resilience ----------

def test_config_fetch_failure_does_not_block_trading():
    """If Supabase read_runtime_config throws, the runtime keeps trading
    using the cached defaults — never silently halt on infra hiccups."""
    db = _FakeDb(config_raises=True)
    broker = _FakeBroker()
    rt = _build_runtime(db, broker)
    # Force a refresh attempt — should not raise
    rt._refresh_config(force=True)
    # Default cache stands → not paused
    assert rt._is_paused() is False

    decision = _enter_decision(datetime(2026, 5, 5, 9, 0, tzinfo=CT))
    bd = _bd(decision.bar_ts)
    asyncio.run(rt._dispatch_decision(decision, bd))
    # Submit went through despite the config-read failure
    assert len(broker.submit_calls) == 1


def test_heartbeat_write_failure_does_not_kill_runtime():
    """If write_heartbeat raises, _on_closed_bar should swallow it."""
    class _ExplodingDb(_FakeDb):
        def write_heartbeat(self, *_a, **_kw):
            raise RuntimeError("supabase down")

    db = _ExplodingDb()
    rt = _build_runtime(db, _FakeBroker())
    # Should not raise
    rt._on_closed_bar(_bd(datetime(2026, 5, 5, 9, 0, tzinfo=CT)))
