"""Integration tests for the Conductor — verifies the signal-routing pipeline
end-to-end with the paper broker and a stub Strategy.

Critical invariants:
  - Every signal_emitted is followed by exactly one signal_arbitrated (winner)
    or signal_suppressed (loser)
  - The strategy field is non-null on every signal/order event
  - Conductor never holds opposite positions simultaneously
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

import pytest

from acme.broker.base import Bar, BracketSpec
from acme.broker.paper import PaperAdapter
from acme.conductor.conductor import Conductor
from acme.config import Config
from acme.contracts import MES
from acme.registry import StrategyRegistry
from acme.risk import TOPSTEP_50K
from acme.strategies.base import Signal, StrategyMetadata


class _CapturingDb:
    """Minimal Db replacement that captures log_event calls in memory."""

    def __init__(self):
        self.events: list[dict[str, Any]] = []

    def log_event(self, kind: str, **kw):
        self.events.append({"kind": kind, **kw})

    def upsert_daily_state(self, *_a, **_kw):
        pass


class _StubStrategy:
    """Programmable Strategy that emits a queued signal sequence on each on_bar."""

    name = "stub"
    version = "1"
    contract = MES
    timeframe_minutes = 1
    metadata = StrategyMetadata(
        tier=2,
        regime_fit={"trending": 1.0},
        time_buckets=["08:30-14:45"],
        default_lifecycle="PILOT",
        timeframe_minutes=1,
    )

    def __init__(self, signal_queue: list[Signal | None] | None = None):
        self._queue = list(signal_queue or [])

    def required_history_bars(self) -> int:
        return 0

    def on_bar(self, bar, *, state, profile, current_position, current_balance_unrealized):
        if not self._queue:
            return None
        return self._queue.pop(0)


def _config() -> Config:
    return Config(
        eval_profile=TOPSTEP_50K,
        live=False,
        tz="America/Chicago",
        flatten_hh=14, flatten_mm=55,
        trade_window_start=(8, 30),
        trade_window_end=(14, 45),
    )


def _bar(t: datetime, o=5000.0, h=5001.0, low=4999.0, c=5000.0, v=10):
    return Bar(t=t, o=o, h=h, l=low, c=c, v=v)


def _build_conductor(strategy_signals: list[Signal | None], dry_run: bool = True):
    db = _CapturingDb()
    registry = StrategyRegistry(db=None)
    registry.upsert(name="stub", version="1", state="PILOT", tier=2)
    registry.attach_instance("stub", _StubStrategy(signal_queue=strategy_signals))
    broker = PaperAdapter()
    cond = Conductor(broker, db, _config(), registry, dry_run=dry_run)
    return cond, db, broker


def _buy(size=1, reason="up"):
    return Signal(side="buy", size=size,
                  bracket=BracketSpec(stop_loss_offset_ticks=8, take_profit_offset_ticks=16),
                  reason=reason)


def _sell(size=1, reason="down"):
    return Signal(side="sell", size=size,
                  bracket=BracketSpec(stop_loss_offset_ticks=8, take_profit_offset_ticks=16),
                  reason=reason)


# ---------- _process_bar smoke tests (don't need a live quote stream) ----------

@pytest.mark.asyncio
async def test_no_signal_no_events():
    cond, db, _ = _build_conductor(strategy_signals=[None])
    bar = _bar(datetime(2026, 4, 30, 10, 0, tzinfo=UTC))
    state = _state_for(cond)
    await cond._process_bar(bar, 1, "CON.F.US.MES.M26", 50_000.0, state)
    assert db.events == []


@pytest.mark.asyncio
async def test_single_signal_emits_full_event_sequence():
    """B2 ordering: per-strategy phantoms open during collection, then arbitration
    runs across all collected candidates. So the sequence is:
      signal_emitted → dry_run_signal → signal_arbitrated
    """
    cond, db, _ = _build_conductor(strategy_signals=[_buy(size=2, reason="cross_up")])
    bar = _bar(datetime(2026, 4, 30, 10, 0, tzinfo=UTC), c=5000.0)
    await cond._process_bar(bar, 1, "CON.F.US.MES.M26", 50_000.0, _state_for(cond))

    kinds = [e["kind"] for e in db.events]
    assert kinds == ["signal_emitted", "dry_run_signal", "signal_arbitrated"]
    # All events tagged with strategy
    for e in db.events:
        assert e.get("strategy") == "stub"


@pytest.mark.asyncio
async def test_size_zero_signal_logs_risk_block_not_dry_run():
    """If the strategy returns a size=0 signal (its own risk gate fired), the
    conductor should log risk_block, NOT dry_run_signal."""
    cond, db, _ = _build_conductor(
        strategy_signals=[Signal(side="buy", size=0, reason="blocked: dd")]
    )
    bar = _bar(datetime(2026, 4, 30, 10, 0, tzinfo=UTC))
    await cond._process_bar(bar, 1, "CON.F.US.MES.M26", 50_000.0, _state_for(cond))

    kinds = Counter(e["kind"] for e in db.events)
    assert kinds["signal_emitted"] == 1
    assert kinds["risk_block"] == 1
    assert kinds["dry_run_signal"] == 0
    # Arbitration still runs (info-only) since the risk-blocked signal is still a candidate.
    assert kinds["signal_arbitrated"] == 1


@pytest.mark.asyncio
async def test_dry_run_phantom_position_opens_per_strategy():
    cond, db, _ = _build_conductor(strategy_signals=[_buy(size=2)])
    bar = _bar(datetime(2026, 4, 30, 10, 0, tzinfo=UTC), c=5000.0)
    await cond._process_bar(bar, 1, "CON.F.US.MES.M26", 50_000.0, _state_for(cond))

    # Per-strategy phantom dict (B2 shape)
    assert "stub" in cond.dry_run_open
    assert len(cond.dry_run_open["stub"]) == 1
    assert cond.dry_run_open["stub"][0].side == "buy"
    assert cond.dry_run_open["stub"][0].size == 2
    assert cond.dry_run_position_per_strategy["stub"] == 2


@pytest.mark.asyncio
async def test_dry_run_phantom_closes_on_target_hit_next_bar():
    """Phantom position opens on bar 1, closes on bar 2 when the high reaches target."""
    cond, db, _ = _build_conductor(strategy_signals=[_buy(size=1, reason="up"), None])
    bar1 = _bar(datetime(2026, 4, 30, 10, 0, tzinfo=UTC), c=5000.0)
    state = _state_for(cond)
    await cond._process_bar(bar1, 1, "CON.F.US.MES.M26", 50_000.0, state)
    assert cond.dry_run_position_per_strategy["stub"] == 1

    # 16-tick target above 5000.0 with tick=0.25 = 5004.0; high reaches it
    bar2 = _bar(datetime(2026, 4, 30, 10, 1, tzinfo=UTC),
                o=5000.5, h=5004.5, low=5000.0, c=5004.0)
    # Manually run the dry-run exit check on the strategy's bucket
    from acme.conductor.dry_run import check_dry_run_exits
    delta, closes_returned = check_dry_run_exits(
        cond.dry_run_open["stub"], bar2, "CON.F.US.MES.M26",
        cond.contract.point_value, cond.round_turn_fee, db,
    )
    cond.dry_run_position_per_strategy["stub"] += delta
    assert cond.dry_run_position_per_strategy["stub"] == 0
    assert len(closes_returned) == 1
    assert closes_returned[0].outcome == "target"
    closes = [e for e in db.events if e["kind"] == "dry_run_close"]
    assert len(closes) == 1
    assert closes[0]["raw"]["outcome"] == "target"
    # 16 ticks * 0.25 * $5/pt = $20 win - $1.24 commission = $18.76 net
    assert closes[0]["raw"]["net_pnl"] == pytest.approx(18.76, abs=0.01)


# ---------- Helpers ----------

def _state_for(cond: Conductor):
    from acme.risk import DailyState
    return DailyState(
        trade_date=datetime(2026, 4, 30).date(),
        starting_balance=50_000.0,
        peak_balance_eod=50_000.0,
        max_loss_limit=50_000.0 - cond.config.eval_profile.max_loss_amount,
        daily_loss_limit=cond.config.eval_profile.daily_loss_limit,
    )
