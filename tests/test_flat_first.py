"""Flat-first state machine tests.

Critical invariants:
  - In CLOSING and COOLDOWN states, the FSM blocks new orders (is_blocking)
  - COOLDOWN requires the configured number of seconds before transitioning
    to RE_EVAL
  - After re-eval completes, FSM returns to IDLE regardless of whether the
    re-evaluated signal fired
"""

from datetime import UTC, datetime, timedelta

import pytest

from acme.conductor.flat_first import FlatFirstFSM

_BASE = datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)


def _utc(s: int = 0) -> datetime:
    """Returns BASE + s seconds. Accepts s >= 60 (rolls into minutes)."""
    return _BASE + timedelta(seconds=s)


def test_starts_idle_not_blocking():
    fsm = FlatFirstFSM(cooldown_seconds=60)
    assert fsm.status.state == "IDLE"
    assert not fsm.is_blocking


def test_request_reversal_transitions_to_closing():
    fsm = FlatFirstFSM(cooldown_seconds=60)
    action = fsm.request_reversal("sell", _utc(0), reason="test")
    assert action == "close_now"
    assert fsm.status.state == "CLOSING"
    assert fsm.is_blocking
    assert fsm.status.pending_direction == "sell"


def test_on_position_closed_transitions_to_cooldown():
    fsm = FlatFirstFSM(cooldown_seconds=60)
    fsm.request_reversal("sell", _utc(0))
    fsm.on_position_closed(_utc(1))
    assert fsm.status.state == "COOLDOWN"
    assert fsm.is_blocking
    assert fsm.status.cooldown_until == _utc(1) + timedelta(seconds=60)


def test_tick_during_cooldown_does_nothing():
    fsm = FlatFirstFSM(cooldown_seconds=60)
    fsm.request_reversal("sell", _utc(0))
    fsm.on_position_closed(_utc(0))
    # 30 seconds in — still in cooldown
    assert fsm.tick(_utc(30)) is None
    assert fsm.status.state == "COOLDOWN"


def test_tick_after_cooldown_transitions_to_re_eval():
    fsm = FlatFirstFSM(cooldown_seconds=60)
    fsm.request_reversal("sell", _utc(0))
    fsm.on_position_closed(_utc(0))
    # 60 seconds in — exactly at boundary
    assert fsm.tick(_utc(60)) == "re_eval"
    assert fsm.status.state == "RE_EVAL"
    assert not fsm.is_blocking   # RE_EVAL is the moment we allow a new signal


def test_on_re_eval_done_returns_to_idle():
    fsm = FlatFirstFSM(cooldown_seconds=60)
    fsm.request_reversal("sell", _utc(0))
    fsm.on_position_closed(_utc(0))
    fsm.tick(_utc(60))
    fsm.on_re_eval_done(executed=True)
    assert fsm.status.state == "IDLE"
    assert fsm.status.pending_direction is None
    assert fsm.status.cooldown_until is None


def test_on_re_eval_done_returns_to_idle_even_if_not_executed():
    """Even when the post-cooldown re-eval fails to produce a signal, FSM resets."""
    fsm = FlatFirstFSM(cooldown_seconds=60)
    fsm.request_reversal("sell", _utc(0))
    fsm.on_position_closed(_utc(0))
    fsm.tick(_utc(60))
    fsm.on_re_eval_done(executed=False)
    assert fsm.status.state == "IDLE"


def test_cooldown_with_short_seconds():
    fsm = FlatFirstFSM(cooldown_seconds=5)
    fsm.request_reversal("buy", _utc(0))
    fsm.on_position_closed(_utc(0))
    assert fsm.tick(_utc(4)) is None
    assert fsm.tick(_utc(5)) == "re_eval"


def test_invariant_no_orders_in_closing_or_cooldown():
    """The conductor uses is_blocking to refuse new orders. Both intermediate
    states must block; IDLE and RE_EVAL must not.
    """
    fsm = FlatFirstFSM(cooldown_seconds=10)
    assert not fsm.is_blocking                         # IDLE
    fsm.request_reversal("sell", _utc(0))
    assert fsm.is_blocking                             # CLOSING
    fsm.on_position_closed(_utc(0))
    assert fsm.is_blocking                             # COOLDOWN
    fsm.tick(_utc(10))
    assert not fsm.is_blocking                         # RE_EVAL
    fsm.on_re_eval_done(executed=False)
    assert not fsm.is_blocking                         # IDLE again


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_pending_direction_round_trips(direction):
    fsm = FlatFirstFSM()
    fsm.request_reversal(direction, _utc(0))
    assert fsm.status.pending_direction == direction
