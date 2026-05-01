"""Tests for the strategy registry: state-machine transitions, gate evaluation,
illegal transitions rejected.
"""

import pytest

from acme.registry import IllegalTransitionError, StrategyRegistry


@pytest.fixture
def reg():
    r = StrategyRegistry(db=None)
    r.upsert(name="alpha", version="1", state="SHADOW", tier=2)
    return r


def test_upsert_adds_strategy(reg):
    rec = reg.get("alpha")
    assert rec.name == "alpha"
    assert rec.state == "SHADOW"
    assert rec.tier == 2
    assert rec.score == 0.0


def test_legal_transition(reg):
    reg.transition("alpha", "PILOT", reason="manual promote")
    assert reg.get("alpha").state == "PILOT"


def test_illegal_transition_raises(reg):
    # SHADOW -> LIVE is not allowed (must go through PILOT)
    with pytest.raises(IllegalTransitionError):
        reg.transition("alpha", "LIVE", reason="should fail")
    # State unchanged
    assert reg.get("alpha").state == "SHADOW"


def test_retired_is_terminal(reg):
    reg.transition("alpha", "PILOT", reason="ok")
    reg.transition("alpha", "LIVE", reason="ok")
    reg.transition("alpha", "RETIRED", reason="end of life")
    with pytest.raises(IllegalTransitionError):
        reg.transition("alpha", "LIVE", reason="resurrection attempt")


def test_set_score(reg):
    reg.set_score("alpha", 0.72)
    assert reg.get("alpha").score == 0.72


def test_unknown_strategy_raises(reg):
    with pytest.raises(KeyError):
        reg.transition("ghost", "PILOT", reason="x")
    with pytest.raises(KeyError):
        reg.set_score("ghost", 0.5)


def test_list_active_excludes_retired(reg):
    reg.upsert(name="beta", version="1", state="SHADOW", tier=1)

    # Without instances attached, list_active is empty (instance=None filter)
    assert reg.list_active() == []

    # Attach a minimal stub
    class _StubStrategy:
        name = "alpha"
        version = "1"
        timeframe_minutes = 1

        def required_history_bars(self):
            return 0

        def on_bar(self, *args, **kwargs):
            return None

    reg.attach_instance("alpha", _StubStrategy())
    assert len(reg.list_active()) == 1
    assert reg.list_active()[0].name == "alpha"

    reg.transition("alpha", "RETIRED", reason="done")
    assert reg.list_active() == []


def test_is_executable_property(reg):
    rec = reg.get("alpha")
    assert not rec.is_executable    # SHADOW
    reg.transition("alpha", "PILOT", reason="ok")
    assert reg.get("alpha").is_executable
    reg.transition("alpha", "LIVE", reason="ok")
    assert reg.get("alpha").is_executable
    reg.transition("alpha", "BENCH", reason="ok")
    assert not reg.get("alpha").is_executable
