"""Arbitrator tests: B1 is no-op (single signal passes through). Conflict cases
exist as a deterministic safety net but the full composite-score logic lands in B3.
"""

from acme.broker.base import BracketSpec
from acme.conductor.arbitrator import arbitrate_b1
from acme.strategies.base import Signal


def _sig(side="buy", size=1, reason="test"):
    return Signal(
        side=side, size=size,
        bracket=BracketSpec(stop_loss_offset_ticks=8, take_profit_offset_ticks=16),
        reason=reason,
    )


def test_no_candidates_returns_no_winner():
    r = arbitrate_b1([])
    assert r.winner is None
    assert r.suppressed == []
    assert "no_signals" in r.notes


def test_single_candidate_wins():
    r = arbitrate_b1([("ema_cross", _sig(side="buy", size=2, reason="cross_up"))])
    assert r.has_winner
    assert r.winner.strategy == "ema_cross"
    assert r.winner.signal.size == 2
    assert r.winner.signal.side == "buy"
    assert r.suppressed == []


def test_multiple_candidates_lower_name_wins_in_b1():
    r = arbitrate_b1([
        ("zeta", _sig(side="sell", size=3, reason="z")),
        ("alpha", _sig(side="buy", size=1, reason="a")),
        ("mike", _sig(side="buy", size=2, reason="m")),
    ])
    assert r.has_winner
    assert r.winner.strategy == "alpha"
    suppressed_names = {s.strategy for s in r.suppressed}
    assert suppressed_names == {"zeta", "mike"}
    # Suppressed signals carry the b1-only reason
    for s in r.suppressed:
        assert "b1_single_strategy_only" in s.score_breakdown


def test_zero_size_signal_is_winner_but_not_actionable():
    """A risk-blocked signal (size=0) is still a winner so the conductor logs
    it as risk_block; has_actionable_winner reflects that no order should fire."""
    r = arbitrate_b1([("ema_cross", _sig(size=0, reason="blocked: dd"))])
    assert r.winner is not None
    assert r.has_winner
    assert not r.has_actionable_winner
