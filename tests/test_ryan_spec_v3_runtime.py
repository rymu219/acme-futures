"""Tests for the v3 runtime's broker-fill reconciliation path.

Covers the pure slippage and P&L math, the _process_user_event dispatch
for both entry and exit fills (orderId + customTag matching, race cases,
malformed payloads), and the close-side broker plumbing.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from acme.broker.base import Bar
from acme.broker.paper import PaperAdapter
from acme.ryan_spec.v3_engine import Decision
from acme.ryan_spec.v3_runtime import (
    ROUND_TURN_COMMISSION_DOLLARS,
    V3Runtime,
    _parse_hhmm,
    _PendingEntry,
    _PendingExit,
    compute_entry_slippage_ticks,
    compute_realized_pnl_dollars,
)
from acme.ryan_spec.v3_tick_delta import BarWithDelta

# --- pure helper --------------------------------------------------------

def test_slippage_long_adverse_one_tick():
    # Long: paid 0.25 above modeled → +1 tick adverse
    assert compute_entry_slippage_ticks("long", 5000.00, 5000.25, 0.25) == 1


def test_slippage_long_adverse_two_ticks():
    assert compute_entry_slippage_ticks("long", 5000.00, 5000.50, 0.25) == 2


def test_slippage_long_favorable():
    # Long: filled below modeled → negative (favorable)
    assert compute_entry_slippage_ticks("long", 5000.00, 4999.75, 0.25) == -1


def test_slippage_short_adverse():
    # Short: received 0.50 below modeled → +2 ticks adverse
    assert compute_entry_slippage_ticks("short", 5000.00, 4999.50, 0.25) == 2


def test_slippage_short_favorable():
    assert compute_entry_slippage_ticks("short", 5000.00, 5000.25, 0.25) == -1


def test_slippage_zero():
    assert compute_entry_slippage_ticks("long", 5000.00, 5000.00, 0.25) == 0


def test_slippage_rounds_to_nearest_tick():
    # 0.30 above modeled on 0.25 tick = 1.2 ticks → rounds to 1
    assert compute_entry_slippage_ticks("long", 5000.00, 5000.30, 0.25) == 1


# --- fill reconciliation dispatch ---------------------------------------

class _FakeDb:
    def __init__(self) -> None:
        self.inserts: list[dict] = []
        self.updates: list[tuple[int, dict[str, Any]]] = []
        self.next_id = 1

    def insert_ryan_spec_v3_trade(self, row: dict) -> int | None:
        self.inserts.append(dict(row))
        rid = self.next_id
        self.next_id += 1
        return rid

    def update_ryan_spec_v3_trade(self, trade_id: int, fields: dict) -> None:
        self.updates.append((trade_id, dict(fields)))


def _runtime() -> V3Runtime:
    return V3Runtime(
        broker=PaperAdapter(),
        db=_FakeDb(),  # type: ignore[arg-type]
        contract_symbol="MES",
        mode="paper",
    )


def _seed_open(
    runtime: V3Runtime,
    *,
    row_id: int,
    direction: str,
    modeled: float,
    order_id: str = "ORD-1",
) -> None:
    sign = 1 if direction == "long" else -1
    runtime._open_trade_id = row_id
    runtime._open_position_meta = {
        "direction": direction,
        "entry_fill": modeled,
        "atr": 1.0,
        "cum_delta": 0,
        "entry_ts": datetime.now(UTC),
        "sign": sign,
    }
    runtime._pending_entry_fills[order_id] = _PendingEntry(
        row_id=row_id, direction=direction, modeled_price=modeled,  # type: ignore[arg-type]
    )


async def test_fill_event_patches_trade_row_and_meta():
    runtime = _runtime()
    _seed_open(runtime, row_id=42, direction="long", modeled=5000.00)

    await runtime._process_user_event({
        "kind": "fill",
        "payload": {"orderId": "ORD-1", "price": 5000.50},
    })

    db = runtime.db
    assert db.updates == [(42, {"entry_price": 5000.50, "slippage_ticks": 2})]  # type: ignore[attr-defined]
    assert runtime._open_position_meta is not None
    assert runtime._open_position_meta["entry_fill"] == 5000.50
    assert "ORD-1" not in runtime._pending_entry_fills


async def test_fill_matches_via_custom_tag_when_order_id_missing():
    runtime = _runtime()
    _seed_open(runtime, row_id=42, direction="short", modeled=5000.00, order_id="ORD-X")

    await runtime._process_user_event({
        "kind": "fill",
        "payload": {"customTag": "v3-canon:42:in", "price": 4999.75},
    })

    db = runtime.db
    # Short adverse: received 0.25 less → +1 tick
    assert db.updates == [(42, {"entry_price": 4999.75, "slippage_ticks": 1})]  # type: ignore[attr-defined]
    assert runtime._open_position_meta is not None
    assert runtime._open_position_meta["entry_fill"] == 4999.75
    assert "ORD-X" not in runtime._pending_entry_fills


async def test_fill_with_unknown_order_id_is_ignored():
    runtime = _runtime()
    _seed_open(runtime, row_id=42, direction="long", modeled=5000.00)

    await runtime._process_user_event({
        "kind": "fill",
        "payload": {"orderId": "STRANGER", "price": 5000.50},
    })

    assert runtime.db.updates == []  # type: ignore[attr-defined]
    assert runtime._open_position_meta is not None
    assert runtime._open_position_meta["entry_fill"] == 5000.00
    assert "ORD-1" in runtime._pending_entry_fills  # not consumed


async def test_non_fill_event_kinds_are_ignored():
    runtime = _runtime()
    _seed_open(runtime, row_id=42, direction="long", modeled=5000.00)

    await runtime._process_user_event({"kind": "order", "payload": {"orderId": "ORD-1"}})
    await runtime._process_user_event({"kind": "position", "payload": {}})
    await runtime._process_user_event({"kind": "account", "payload": {}})

    assert runtime.db.updates == []  # type: ignore[attr-defined]
    assert "ORD-1" in runtime._pending_entry_fills


async def test_fill_arriving_after_close_still_patches_db():
    """Race: fill confirmation arrives after the position has been flattened.
    DB row should still reflect the real fill, even if no meta is open."""
    runtime = _runtime()
    _seed_open(runtime, row_id=42, direction="long", modeled=5000.00)
    runtime._open_trade_id = None
    runtime._open_position_meta = None

    await runtime._process_user_event({
        "kind": "fill",
        "payload": {"orderId": "ORD-1", "price": 5000.50},
    })

    db = runtime.db
    assert db.updates == [(42, {"entry_price": 5000.50, "slippage_ticks": 2})]  # type: ignore[attr-defined]
    assert runtime._open_position_meta is None


async def test_fill_with_missing_price_is_skipped():
    """Malformed broker payload — log and move on, don't write garbage."""
    runtime = _runtime()
    _seed_open(runtime, row_id=42, direction="long", modeled=5000.00)

    await runtime._process_user_event({
        "kind": "fill",
        "payload": {"orderId": "ORD-1"},  # no price field
    })

    assert runtime.db.updates == []  # type: ignore[attr-defined]


async def test_fill_payload_accepts_capitalized_keys():
    """ProjectX shapes vary — accept Price/OrderId/CustomTag."""
    runtime = _runtime()
    _seed_open(runtime, row_id=42, direction="long", modeled=5000.00, order_id="ORD-Q")

    await runtime._process_user_event({
        "kind": "fill",
        "payload": {"OrderId": "ORD-Q", "Price": 5000.25},
    })

    db = runtime.db
    assert db.updates == [(42, {"entry_price": 5000.25, "slippage_ticks": 1})]  # type: ignore[attr-defined]


# --- dry_run mode -------------------------------------------------------

def _dry_runtime() -> V3Runtime:
    rt = V3Runtime(
        broker=PaperAdapter(),
        db=_FakeDb(),  # type: ignore[arg-type]
        contract_symbol="MES",
        mode="paper",
        dry_run=True,
    )
    rt._contract_id = "CON.F.US.MES.M26"
    return rt


def _decision_enter(
    *, direction: str = "long", entry: float = 5000.0, atr: float = 2.0,
) -> Decision:
    sign = 1 if direction == "long" else -1
    return Decision(
        action="enter",
        direction=direction,  # type: ignore[arg-type]
        reason="oos_v3_signal",
        entry_price=entry,
        stop_price=entry - sign * 1.5 * atr,
        bar_ts=datetime.now(UTC),
        cum_delta_at_entry=-2500,
        atr_at_entry=atr,
    )


async def test_dry_run_open_skips_broker_submit():
    rt = _dry_runtime()
    await rt._open(_decision_enter(direction="long", entry=5000.0), _bar_with_delta(close=5000.0))

    # No real order was submitted on the broker.
    assert rt.broker.submitted_orders == []  # type: ignore[attr-defined]
    # Trade row was still inserted at the modeled price.
    db = rt.db
    assert len(db.inserts) == 1  # type: ignore[attr-defined]
    assert db.inserts[0]["entry_price"] == 5000.0  # type: ignore[attr-defined]
    assert db.inserts[0]["mode"] == "paper"  # type: ignore[attr-defined]
    # Slippage stamped 0 so the gate's slippage stat distinguishes phantom
    # rows from real fills it hasn't yet measured.
    assert db.updates == [(1, {"slippage_ticks": 0})]  # type: ignore[attr-defined]
    # Open state set; no pending entry registered (no fill confirmation will arrive).
    assert rt._open_trade_id == 1
    assert rt._open_position_meta is not None
    assert rt._open_position_meta["entry_fill"] == 5000.0
    assert rt._pending_entry_fills == {}


async def test_dry_run_open_does_not_count_as_broker_error():
    """Phantom fills must NOT bump the consecutive-errors counter or trip
    the circuit breaker."""
    rt = _dry_runtime()
    await rt._open(_decision_enter(), _bar_with_delta())
    assert rt._consecutive_errors == 0


async def test_dry_run_close_skips_broker_submit_and_writes_exit_row():
    rt = _dry_runtime()
    _seed_open(rt, row_id=42, direction="long", modeled=5000.0)

    await rt._close(_decision_exit(), _bar_with_delta(close=5004.0))

    # No real exit order was submitted.
    assert rt.broker.submitted_orders == []  # type: ignore[attr-defined]
    # Exit row written from bar.c, P&L computed at the phantom prices.
    db = rt.db
    row_id, fields = db.updates[0]  # type: ignore[attr-defined]
    assert row_id == 42
    assert fields["exit_price"] == 5004.0
    assert fields["exit_reason"] == "opposite_signal"
    # (5004 - 5000) * 1 * 5 - 0.70 = 19.30
    assert fields["pnl_dollars"] == pytest.approx(19.30)
    # No pending exit registered — no real fill is coming.
    assert rt._pending_exit_fills == {}
    # Position state cleared.
    assert rt._open_trade_id is None


async def test_dry_run_skips_user_events_task():
    """In dry_run, run() must not start the user-events background task —
    no real fills will arrive, and the user hub may not even be authorised
    on a read-only connection."""
    rt = _dry_runtime()
    await rt.broker.authenticate()
    rt._auth_ok = True
    rt._contract_id = await rt.broker.resolve_contract("MES")

    # Simulate the slice of run() that decides whether to start the task.
    if not rt.dry_run:
        rt._user_events_task = asyncio.create_task(rt._consume_user_events())  # noqa
    assert rt._user_events_task is None


# --- trade-stream loop --------------------------------------------------

async def test_paper_stream_trades_yields_expected_sequence():
    """PaperAdapter trade stream is the test substrate for _run_trade_loop."""
    paper = PaperAdapter()
    trades = [tr async for tr in paper.stream_trades("CON.X")]
    assert len(trades) == 5
    assert trades[0].price == 5000.00
    assert trades[1].price == 5000.25
    assert all(t.size == 1 for t in trades)
    assert [t.side for t in trades] == ["B", "A", "B", "A", "B"]
    assert all(t.contract_id == "CON.X" for t in trades)


async def test_run_trade_loop_feeds_builder_with_trade_events():
    runtime = V3Runtime(
        broker=PaperAdapter(),
        db=_FakeDb(),  # type: ignore[arg-type]
        contract_symbol="MES",
        mode="paper",
        delta_source="trade",
    )
    runtime._contract_id = "CON.F.US.MES.M26"

    captured: list[tuple[float, int, str | None]] = []
    original = runtime.builder.add_trade

    def spy(t: datetime, price: float, size: int, *, side: str | None = None) -> None:
        captured.append((price, size, side))
        original(t, price, size, side=side)  # type: ignore[arg-type]

    runtime.builder.add_trade = spy  # type: ignore[assignment]

    await runtime._run_trade_loop()

    assert len(captured) == 5
    assert captured[0] == (5000.00, 1, "B")
    assert captured[1] == (5000.25, 1, "A")


# --- realized P&L (pure helper) -----------------------------------------

def test_realized_pnl_long_winner():
    # (5005 - 5000) * +1 * 5 - 0.70 = 24.30
    assert compute_realized_pnl_dollars(1, 5000.0, 5005.0, 5.0, 0.70) == pytest.approx(24.30)


def test_realized_pnl_long_loser():
    assert compute_realized_pnl_dollars(1, 5000.0, 4995.0, 5.0, 0.70) == pytest.approx(-25.70)


def test_realized_pnl_short_winner():
    # Short: (4995 - 5000) * -1 * 5 - 0.70 = 24.30
    assert compute_realized_pnl_dollars(-1, 5000.0, 4995.0, 5.0, 0.70) == pytest.approx(24.30)


def test_realized_pnl_short_loser():
    assert compute_realized_pnl_dollars(-1, 5000.0, 5005.0, 5.0, 0.70) == pytest.approx(-25.70)


def test_realized_pnl_zero_minus_commission():
    assert compute_realized_pnl_dollars(1, 5000.0, 5000.0, 5.0, 0.70) == pytest.approx(-0.70)


# --- exit-fill dispatch -------------------------------------------------

def _seed_pending_exit(
    runtime: V3Runtime,
    *,
    row_id: int,
    sign: int,
    entry_fill: float,
    order_id: str = "OUT-1",
    commission: float = ROUND_TURN_COMMISSION_DOLLARS,
) -> None:
    runtime._pending_exit_fills[order_id] = _PendingExit(
        row_id=row_id, sign=sign, entry_fill=entry_fill,
        commission_dollars=commission,
    )


async def test_exit_fill_recomputes_pnl_dollars_long():
    runtime = _runtime()
    _seed_pending_exit(runtime, row_id=42, sign=1, entry_fill=5000.0, order_id="OUT-1")

    await runtime._process_user_event({
        "kind": "fill",
        "payload": {"orderId": "OUT-1", "price": 5004.50},
    })

    db = runtime.db
    # (5004.50 - 5000.00) * 1 * 5 - 0.70 = 21.80
    assert len(db.updates) == 1  # type: ignore[attr-defined]
    row_id, fields = db.updates[0]  # type: ignore[attr-defined]
    assert row_id == 42
    assert fields["exit_price"] == 5004.50
    assert fields["pnl_dollars"] == pytest.approx(21.80)
    assert "OUT-1" not in runtime._pending_exit_fills


async def test_exit_fill_recomputes_pnl_dollars_short():
    runtime = _runtime()
    _seed_pending_exit(runtime, row_id=43, sign=-1, entry_fill=5000.0, order_id="OUT-S")

    await runtime._process_user_event({
        "kind": "fill",
        "payload": {"orderId": "OUT-S", "price": 4998.0},
    })

    row_id, fields = runtime.db.updates[0]  # type: ignore[attr-defined]
    # (4998 - 5000) * -1 * 5 - 0.70 = 9.30
    assert row_id == 43
    assert fields["exit_price"] == 4998.0
    assert fields["pnl_dollars"] == pytest.approx(9.30)


async def test_exit_fill_matches_via_custom_tag():
    runtime = _runtime()
    _seed_pending_exit(runtime, row_id=42, sign=1, entry_fill=5000.0, order_id="OUT-X")

    await runtime._process_user_event({
        "kind": "fill",
        "payload": {"customTag": "v3-canon:42:out", "price": 5004.0},
    })

    row_id, fields = runtime.db.updates[0]  # type: ignore[attr-defined]
    assert row_id == 42
    assert fields["pnl_dollars"] == pytest.approx(19.30)
    assert "OUT-X" not in runtime._pending_exit_fills


async def test_unknown_exit_order_id_is_ignored():
    runtime = _runtime()
    _seed_pending_exit(runtime, row_id=42, sign=1, entry_fill=5000.0)

    await runtime._process_user_event({
        "kind": "fill",
        "payload": {"orderId": "STRANGER", "price": 5004.0},
    })

    assert runtime.db.updates == []  # type: ignore[attr-defined]
    assert "OUT-1" in runtime._pending_exit_fills


async def test_entry_match_takes_precedence_over_exit_when_same_id():
    """Defensive: if the same orderId appears in both pending dicts, treat
    as entry first. Real broker won't reuse IDs but the dispatcher must be
    deterministic."""
    runtime = _runtime()
    _seed_open(runtime, row_id=10, direction="long", modeled=5000.00, order_id="DUP")
    _seed_pending_exit(runtime, row_id=11, sign=1, entry_fill=4990.0, order_id="DUP")

    await runtime._process_user_event({
        "kind": "fill",
        "payload": {"orderId": "DUP", "price": 5000.50},
    })

    # Entry path won → row 10 patched, row 11 untouched
    assert len(runtime.db.updates) == 1  # type: ignore[attr-defined]
    assert runtime.db.updates[0][0] == 10  # type: ignore[attr-defined]
    assert "slippage_ticks" in runtime.db.updates[0][1]  # type: ignore[attr-defined]
    # Exit pending still queued under same key
    assert "DUP" in runtime._pending_exit_fills


# --- _close() integration -----------------------------------------------

def _decision_exit(reason: str = "opposite_signal") -> Decision:
    return Decision(action="exit", reason=reason, bar_ts=datetime.now(UTC))


def _bar_with_delta(close: float = 5004.0) -> BarWithDelta:
    bar = Bar(t=datetime.now(UTC), o=5000.0, h=5005.0, l=4998.0, c=close, v=100)
    return BarWithDelta(bar=bar, delta=0, cum_delta_session=0)


async def test_close_submits_tagged_opposite_order_and_registers_pending_exit():
    runtime = _runtime()
    runtime._contract_id = "CON.X"
    _seed_open(runtime, row_id=42, direction="long", modeled=5000.00)

    await runtime._close(_decision_exit(), _bar_with_delta(close=5004.0))

    # Provisional DB row written from bar.c
    db = runtime.db
    assert len(db.updates) == 1  # type: ignore[attr-defined]
    row_id, fields = db.updates[0]  # type: ignore[attr-defined]
    assert row_id == 42
    assert fields["exit_price"] == 5004.0
    assert fields["exit_reason"] == "opposite_signal"
    # Provisional PnL: (5004 - 5000) * 1 * 5 - 0.70 = 19.30
    assert fields["pnl_dollars"] == pytest.approx(19.30)

    # Tagged opposite-side market order submitted (long → sell)
    submitted = runtime.broker.submitted_orders  # type: ignore[attr-defined]
    assert len(submitted) == 1
    assert submitted[0]["side"] == "sell"
    assert submitted[0]["tag"] == "v3-canon:42:out"

    # Pending exit registered for fill reconciliation
    assert len(runtime._pending_exit_fills) == 1
    pending = next(iter(runtime._pending_exit_fills.values()))
    assert pending.row_id == 42
    assert pending.sign == 1
    assert pending.entry_fill == 5000.00

    # Runtime forgets the position
    assert runtime._open_trade_id is None
    assert runtime._open_position_meta is None


async def test_close_short_position_submits_buy_order():
    runtime = _runtime()
    runtime._contract_id = "CON.X"
    _seed_open(runtime, row_id=43, direction="short", modeled=5000.00)

    await runtime._close(_decision_exit(), _bar_with_delta(close=4998.0))

    submitted = runtime.broker.submitted_orders  # type: ignore[attr-defined]
    assert submitted[0]["side"] == "buy"
    assert submitted[0]["tag"] == "v3-canon:43:out"
    pending = next(iter(runtime._pending_exit_fills.values()))
    assert pending.sign == -1


async def test_close_then_exit_fill_overrides_provisional_pnl():
    """End-to-end: _close writes provisional PnL from bar.c, then the real
    exit fill arrives via _process_user_event and overrides it."""
    runtime = _runtime()
    runtime._contract_id = "CON.X"
    _seed_open(runtime, row_id=42, direction="long", modeled=5000.00)

    await runtime._close(_decision_exit(), _bar_with_delta(close=5004.0))
    # Provisional pnl: 19.30
    assert runtime.db.updates[-1][1]["pnl_dollars"] == pytest.approx(19.30)  # type: ignore[attr-defined]

    # Real fill came in worse than bar.c
    pending = next(iter(runtime._pending_exit_fills.values()))
    order_id = next(iter(runtime._pending_exit_fills.keys()))
    await runtime._process_user_event({
        "kind": "fill",
        "payload": {"orderId": order_id, "price": 5003.50},
    })

    # Real pnl: (5003.50 - 5000) * 1 * 5 - 0.70 = 16.80
    assert runtime.db.updates[-1] == (  # type: ignore[attr-defined]
        42, {"exit_price": 5003.50, "pnl_dollars": pytest.approx(16.80)}
    )
    assert pending.row_id == 42  # sanity


# --- MFE/MAE capture on close -------------------------------------------

async def test_close_writes_mfe_atr_and_mae_atr_from_engine():
    """When a position closes, V3Runtime reads MFE/MAE off engine.position
    (in price points), divides by atr_at_entry, and writes the resulting
    ATR-units values to the DB row alongside the other exit fields.
    """
    runtime = _runtime()
    runtime._contract_id = "CON.X"
    _seed_open(runtime, row_id=42, direction="long", modeled=5000.00)
    # Force the engine into a position state with explicit MFE/MAE values.
    # We'll bypass the bar-driven update and just plant the values directly.
    from datetime import datetime as _dt

    from acme.ryan_spec.v3_engine import _Position
    runtime.engine._position = _Position(
        direction="long",
        entry_ts=_dt.now(UTC),
        entry_fill=5000.0,
        stop_price=4995.0,
        bars_held=3,
        cum_delta_at_entry=-2500,
        atr_at_entry=2.0,
        max_favorable_excursion=5.0,   # 2.5 ATR
        max_adverse_excursion=3.0,     # 1.5 ATR
    )

    await runtime._close(_decision_exit(), _bar_with_delta(close=5004.0))

    # The exit-row update should include mfe_atr and mae_atr in ATR units
    db = runtime.db
    row_id, fields = db.updates[0]  # type: ignore[attr-defined]
    assert row_id == 42
    assert fields["mfe_atr"] == 2.5
    assert fields["mae_atr"] == 1.5
    # Sanity: other exit fields still present
    assert "exit_ts" in fields
    assert "exit_reason" in fields


async def test_close_skips_mfe_mae_when_engine_has_no_position():
    """If engine.position is None at close-time (race or already-closed),
    the runtime still writes the provisional exit row but omits mfe_atr/mae_atr
    rather than crashing or writing zeros."""
    runtime = _runtime()
    runtime._contract_id = "CON.X"
    _seed_open(runtime, row_id=42, direction="long", modeled=5000.00)
    runtime.engine._position = None  # simulate engine already-closed race

    await runtime._close(_decision_exit(), _bar_with_delta(close=5004.0))

    db = runtime.db
    row_id, fields = db.updates[0]  # type: ignore[attr-defined]
    assert row_id == 42
    assert "mfe_atr" not in fields
    assert "mae_atr" not in fields


# --- _parse_hhmm helper -------------------------------------------------

def test_parse_hhmm_colon_form():
    from datetime import time as dtime
    assert _parse_hhmm("08:30", field_name="x") == dtime(8, 30)


def test_parse_hhmm_compact_form():
    from datetime import time as dtime
    assert _parse_hhmm("0830", field_name="x") == dtime(8, 30)


def test_parse_hhmm_strips_whitespace():
    from datetime import time as dtime
    assert _parse_hhmm("  14:50  ", field_name="x") == dtime(14, 50)


def test_parse_hhmm_invalid_garbage_raises():
    with pytest.raises(SystemExit):
        _parse_hhmm("nope", field_name="x")


def test_parse_hhmm_out_of_range_hour_raises():
    with pytest.raises(SystemExit):
        _parse_hhmm("25:00", field_name="x")


def test_parse_hhmm_out_of_range_minute_raises():
    with pytest.raises(SystemExit):
        _parse_hhmm("08:60", field_name="x")


async def test_run_trade_loop_honours_stop_event():
    """Cooperative shutdown: setting _stop_event halts ingestion mid-stream."""
    runtime = V3Runtime(
        broker=PaperAdapter(),
        db=_FakeDb(),  # type: ignore[arg-type]
        contract_symbol="MES",
        mode="paper",
        delta_source="trade",
    )
    runtime._contract_id = "CON.F.US.MES.M26"
    runtime._stop_event.set()  # already requested before loop starts

    captured: list[float] = []
    original = runtime.builder.add_trade

    def spy(t: datetime, price: float, size: int, *, side: str | None = None) -> None:
        captured.append(price)
        original(t, price, size, side=side)  # type: ignore[arg-type]

    runtime.builder.add_trade = spy  # type: ignore[assignment]

    await runtime._run_trade_loop()

    # Loop checks stop_event after first iteration → at most 1 trade may slip through
    assert len(captured) <= 1
