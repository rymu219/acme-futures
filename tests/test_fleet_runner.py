"""Wire-up smoke test for the new fleet runner.

Doesn't invoke the conductor's event loop — just confirms:
  - all 4 strategies can be instantiated and attached to a registry
  - registry.list_active() returns 4 entries with the expected names
  - each strategy implements the contract (has on_bar, required_history_bars,
    metadata.default_lifecycle == SHADOW)
"""
from __future__ import annotations

from acme.registry import StrategyRegistry
from acme.strategies.boundary import BoundaryStrategy
from acme.strategies.ignition import IgnitionStrategy
from acme.strategies.regime import RegimeStrategy
from acme.strategies.session import SessionStrategy


EXPECTED = [
    ("ignition", IgnitionStrategy),
    ("session", SessionStrategy),
    ("regime", RegimeStrategy),
    ("boundary", BoundaryStrategy),
]


def test_all_four_strategies_can_be_built():
    for _name, cls in EXPECTED:
        inst = cls()
        assert inst.metadata.default_lifecycle == "SHADOW"
        assert callable(inst.on_bar)
        assert callable(inst.required_history_bars)


def test_each_strategy_emits_no_signal_on_empty_state():
    from datetime import UTC, datetime, timedelta

    from acme.broker.base import Bar
    from acme.risk import TOPSTEP_50K, DailyState

    state = DailyState(
        trade_date=datetime.now(UTC).date(),
        starting_balance=50_000.0, peak_balance_eod=50_000.0,
        max_loss_limit=48_000.0, daily_loss_limit=1_000.0,
    )
    bar = Bar(t=datetime(2026, 5, 15, 14, 0, tzinfo=UTC),
              o=100, h=100.1, l=99.9, c=100, v=100)

    for _name, cls in EXPECTED:
        inst = cls()
        out = inst.on_bar(bar, state=state, profile=TOPSTEP_50K,
                          current_position=0, current_balance_unrealized=50_000)
        # Warm-up bar — every strategy should be silent
        assert out is None


def test_registry_wiring_pattern():
    """The fleet_runner pattern: upsert then attach_instance for each."""
    reg = StrategyRegistry(db=None)
    for name, cls in EXPECTED:
        inst = cls()
        reg.upsert(name=inst.name, version=inst.version,
                   state=inst.metadata.default_lifecycle,
                   tier=inst.metadata.tier, params={})
        reg.attach_instance(name, inst)

    active = reg.list_active()
    assert len(active) == 4
    assert {s.name for s in active} == {"ignition", "session", "regime", "boundary"}
    # All in SHADOW → not executable, but ARE active
    assert all(s.state == "SHADOW" for s in active)
    assert all(not s.is_executable for s in active)
