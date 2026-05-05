"""Tests for the v3 same-day backfill replay logic.

The replay script's only non-IO function is `replay()`. We assert:
  - Bar-level cum_delta reconstruction is size-weighted via tick rule.
  - Deterministic engine integration: when an engine signal fires + closes,
    the replay emits a row with matching timestamps, prices, and P&L sign.
  - Open positions left at the end of the input are dropped (with a warning,
    not an exception).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from acme.broker.base import Bar
from acme.ryan_spec.v3_backfill import reconstruct_bar_delta, replay

CT = timezone(timedelta(hours=-6))


def _bar(t: datetime, *, o: float, h: float, l: float, c: float, v: int = 100) -> Bar:  # noqa: E741
    return Bar(t=t, o=o, h=h, l=l, c=c, v=v)


def test_reconstruct_bar_delta_first_bar_is_zero():
    b = _bar(datetime(2026, 5, 4, 9, 0, tzinfo=CT), o=5000, h=5001, l=4999, c=5000, v=200)
    assert reconstruct_bar_delta(prior_close=None, bar=b) == 0


def test_reconstruct_bar_delta_up_close_positive():
    b = _bar(datetime(2026, 5, 4, 9, 0, tzinfo=CT), o=5000, h=5001, l=4999, c=5001, v=150)
    assert reconstruct_bar_delta(prior_close=5000.0, bar=b) == 150


def test_reconstruct_bar_delta_down_close_negative():
    b = _bar(datetime(2026, 5, 4, 9, 0, tzinfo=CT), o=5000, h=5001, l=4999, c=4999, v=150)
    assert reconstruct_bar_delta(prior_close=5000.0, bar=b) == -150


def test_reconstruct_bar_delta_unchanged_close_zero():
    b = _bar(datetime(2026, 5, 4, 9, 0, tzinfo=CT), o=5000, h=5001, l=4999, c=5000, v=150)
    assert reconstruct_bar_delta(prior_close=5000.0, bar=b) == 0


def test_replay_with_no_bars_returns_empty():
    assert replay([]) == []


def test_replay_no_signals_returns_empty():
    """Flat warmup bars never fire entries; replay should produce zero rows."""
    t = datetime(2026, 5, 4, 8, 30, tzinfo=CT)
    bars = [_bar(t + timedelta(minutes=2 * i), o=5000, h=5000.25,
                 l=4999.75, c=5000, v=100) for i in range(40)]
    assert replay(bars) == []


def test_replay_open_position_at_end_is_dropped_not_raised():
    """If the engine would still be in a trade when bars run out, replay
    must finish cleanly — the live runtime would handle that case live."""
    # Construct a synthetic series that warms indicators then fires a long
    # entry on the last bar, so no exit is possible. We only check that
    # replay() returns without raising; row count may be 0 if no exit.
    t = datetime(2026, 5, 4, 8, 30, tzinfo=CT)
    bars: list[Bar] = []
    # 22 flat warmup bars
    for i in range(22):
        bars.append(_bar(t + timedelta(minutes=2 * i),
                         o=5000, h=5000.25, l=4999.75, c=5000, v=100))
    # Two-bar reversal pattern with strongly-negative cum_delta lead-in:
    # need cum_delta_in_dir < -2000 for long, so we accumulate down-volume
    # via large volume on the prior down bar.
    bars.append(_bar(t + timedelta(minutes=2 * 22),
                    o=5000, h=5000, l=4995, c=4995, v=3000))  # red, big down vol
    bars.append(_bar(t + timedelta(minutes=2 * 23),
                    o=4995, h=5001, l=4995, c=5001, v=200))   # green close > prior
    # No further bars — engine still in position when input ends.
    rows = replay(bars)
    # Must not raise; row count is 0 (no exit captured).
    assert isinstance(rows, list)
    assert len(rows) == 0


def test_replay_emits_round_trip_when_engine_exits_on_stop():
    """Drive the engine into a long, then immediately stop it out the next
    bar via a low that violates the ATR-stop. The replay should emit one
    completed row with exit_reason='stop' and a negative P&L."""
    t = datetime(2026, 5, 4, 8, 30, tzinfo=CT)
    bars: list[Bar] = []
    for i in range(22):
        bars.append(_bar(t + timedelta(minutes=2 * i),
                         o=5000, h=5000.25, l=4999.75, c=5000, v=100))
    # Trigger long: prior bar red with heavy down volume, current bar green
    bars.append(_bar(t + timedelta(minutes=2 * 22),
                    o=5000, h=5000, l=4995, c=4995, v=3000))
    bars.append(_bar(t + timedelta(minutes=2 * 23),
                    o=4995, h=5001, l=4995, c=5001, v=200))
    # Next bar: drive low far below entry to guarantee stop hit
    bars.append(_bar(t + timedelta(minutes=2 * 24),
                    o=5001, h=5001, l=4900, c=4901, v=500))
    rows = replay(bars)
    assert len(rows) == 1
    row = rows[0]
    assert row["direction"] == "long"
    assert row["exit_reason"] == "stop"
    assert row["pnl_dollars"] < 0
    assert row["entry_price"] == 5001.0
