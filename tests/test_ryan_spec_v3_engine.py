"""Tests for Ryan-Spec OOS-v3 decision engine.

Covers:
  - Two-bar reversal trigger detection
  - Filter application (cum_delta_in_dir < -2000)
  - Entry decision shape
  - Exit on stop hit
  - Exit on opposite-signal trigger
  - Exit on session end
  - Exit on time stop
  - Indicator warmup (no signals before BB(20) is hot)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from acme.broker.base import Bar
from acme.ryan_spec.v3_engine import RyanSpecV3Engine

CT = timezone(timedelta(hours=-6))


def _bar(t_ct: datetime, *, o: float, h: float, l: float, c: float, v: int = 100) -> Bar:  # noqa: E741
    return Bar(t=t_ct, o=o, h=h, l=l, c=c, v=v)


def _flat_warmup(engine: RyanSpecV3Engine, n_bars: int = 22,
                 base_price: float = 5000.0,
                 start_ct: datetime | None = None) -> datetime:
    """Feed enough no-signal bars to warm BB(20) + ATR(4)."""
    if start_ct is None:
        start_ct = datetime(2026, 5, 5, 9, 0, tzinfo=CT)
    t = start_ct
    for _ in range(n_bars):
        engine.on_bar(_bar(t, o=base_price, h=base_price + 0.5,
                           l=base_price - 0.5, c=base_price),
                      bar_delta=0, cum_delta_session=0)
        t += timedelta(minutes=2)
    return t


def test_engine_warmup_no_signals():
    e = RyanSpecV3Engine()
    t = datetime(2026, 5, 5, 9, 0, tzinfo=CT)
    for _ in range(15):  # not enough for BB(20)
        d = e.on_bar(_bar(t, o=5000, h=5001, l=4999, c=5000),
                     bar_delta=0, cum_delta_session=0)
        assert d.action == "none"
        t += timedelta(minutes=2)


def test_engine_two_bar_reversal_long_with_filter_passes():
    """Universal trigger fires AND cum_delta_in_dir < -2000 → enter long."""
    e = RyanSpecV3Engine()
    t = _flat_warmup(e)
    # Prior bar: down or doji (close <= open)
    e.on_bar(_bar(t, o=5000.0, h=5000.5, l=4998.5, c=4999.0),
             bar_delta=-200, cum_delta_session=-2500)
    t += timedelta(minutes=2)
    # Current bar: up close > prior close → long trigger
    d = e.on_bar(_bar(t, o=4999.0, h=5001.0, l=4998.5, c=5000.5),
                 bar_delta=-100, cum_delta_session=-2600)
    # cum_delta_session = -2600. cum_delta_in_dir for long = -2600 (< -2000) → passes filter
    assert d.action == "enter"
    assert d.direction == "long"
    assert d.entry_price == 5000.5
    assert d.atr_at_entry is not None and d.atr_at_entry > 0
    assert d.stop_price < d.entry_price


def test_engine_filter_blocks_when_cum_delta_not_extreme_enough():
    e = RyanSpecV3Engine()
    t = _flat_warmup(e)
    e.on_bar(_bar(t, o=5000.0, h=5000.5, l=4998.5, c=4999.0),
             bar_delta=-200, cum_delta_session=-500)
    t += timedelta(minutes=2)
    d = e.on_bar(_bar(t, o=4999.0, h=5001.0, l=4998.5, c=5000.5),
                 bar_delta=-100, cum_delta_session=-1000)  # > -2000
    assert d.action == "none"
    assert "filter_blocked" in d.reason


def test_engine_short_direction_filter_uses_signed_cum_delta():
    """Short trigger: cum_delta_in_dir = cum_delta * -1. Need cum_delta > +2000
    for short trigger to satisfy filter < -2000."""
    e = RyanSpecV3Engine()
    t = _flat_warmup(e)
    # Prior bar up
    e.on_bar(_bar(t, o=5000.0, h=5001.5, l=4999.5, c=5001.0),
             bar_delta=200, cum_delta_session=2500)
    t += timedelta(minutes=2)
    # Current bar down close < prior close
    d = e.on_bar(_bar(t, o=5001.0, h=5001.5, l=4998.5, c=4999.5),
                 bar_delta=100, cum_delta_session=2600)
    # cum_delta_in_dir for short = 2600 * -1 = -2600 < -2000 → enter short
    assert d.action == "enter"
    assert d.direction == "short"


def test_engine_position_exits_on_stop_hit():
    e = RyanSpecV3Engine()
    t = _flat_warmup(e)
    e.on_bar(_bar(t, o=5000.0, h=5000.5, l=4998.5, c=4999.0),
             bar_delta=-200, cum_delta_session=-2500)
    t += timedelta(minutes=2)
    d = e.on_bar(_bar(t, o=4999.0, h=5001.0, l=4998.5, c=5000.5),
                 bar_delta=-100, cum_delta_session=-2600)
    assert d.action == "enter"

    e.open_position(direction="long", entry_ts=d.bar_ts,
                    entry_fill_price=d.entry_price + 0.25,  # 1 tick slippage
                    atr_at_entry=d.atr_at_entry,
                    cum_delta_at_entry=d.cum_delta_at_entry)
    stop = e.position.stop_price
    # Next bar: drives low through stop
    t += timedelta(minutes=2)
    d2 = e.on_bar(_bar(t, o=5000.0, h=5000.0, l=stop - 0.5, c=stop - 0.1),
                  bar_delta=0, cum_delta_session=-2700)
    assert d2.action == "exit"
    assert d2.reason == "stop"


def test_engine_position_exits_on_opposite_signal():
    e = RyanSpecV3Engine()
    t = _flat_warmup(e)
    e.on_bar(_bar(t, o=5000.0, h=5000.5, l=4998.5, c=4999.0),
             bar_delta=-200, cum_delta_session=-2500)
    t += timedelta(minutes=2)
    d = e.on_bar(_bar(t, o=4999.0, h=5001.0, l=4998.5, c=5000.5),
                 bar_delta=-100, cum_delta_session=-2600)
    assert d.action == "enter" and d.direction == "long"
    e.open_position(direction="long", entry_ts=d.bar_ts,
                    entry_fill_price=d.entry_price + 0.25,
                    atr_at_entry=d.atr_at_entry,
                    cum_delta_at_entry=d.cum_delta_at_entry)

    # Two more bars: an UP bar (prior body >= 0) then a DOWN bar (current body < 0)
    # to fire a SHORT trigger while long is open. Stop must NOT be hit.
    t += timedelta(minutes=2)
    e.on_bar(_bar(t, o=5000.5, h=5002.0, l=5000.0, c=5001.5),
             bar_delta=50, cum_delta_session=-2550)
    t += timedelta(minutes=2)
    d_exit = e.on_bar(_bar(t, o=5001.5, h=5002.0, l=5001.0, c=5001.0),
                      bar_delta=-30, cum_delta_session=-2580)
    # current body = 5001.0 - 5001.5 = -0.5 < 0; prior body 5001.5-5000.5=+1.0 >= 0
    # current close 5001.0 < prior close 5001.5 → short trigger fired
    assert d_exit.action == "exit"
    assert d_exit.reason == "opposite_signal"


def test_engine_position_exits_on_session_end():
    e = RyanSpecV3Engine()
    t = _flat_warmup(e, start_ct=datetime(2026, 5, 5, 14, 0, tzinfo=CT))
    e.on_bar(_bar(t, o=5000.0, h=5000.5, l=4998.5, c=4999.0),
             bar_delta=-200, cum_delta_session=-2500)
    t += timedelta(minutes=2)
    d = e.on_bar(_bar(t, o=4999.0, h=5001.0, l=4998.5, c=5000.5),
                 bar_delta=-100, cum_delta_session=-2600)
    assert d.action == "enter"
    e.open_position(direction="long", entry_ts=d.bar_ts,
                    entry_fill_price=d.entry_price + 0.25,
                    atr_at_entry=d.atr_at_entry,
                    cum_delta_at_entry=d.cum_delta_at_entry)
    # Bar at 14:50 CT — session end exit
    t = datetime(2026, 5, 5, 14, 50, tzinfo=CT)
    d_exit = e.on_bar(_bar(t, o=5000.5, h=5001.0, l=5000.0, c=5000.8),
                      bar_delta=10, cum_delta_session=-2590)
    assert d_exit.action == "exit"
    assert d_exit.reason == "session_end"


def test_engine_position_exits_on_time_stop():
    e = RyanSpecV3Engine(time_stop_bars=3)  # shorten for the test
    t = _flat_warmup(e)
    e.on_bar(_bar(t, o=5000.0, h=5000.5, l=4998.5, c=4999.0),
             bar_delta=-200, cum_delta_session=-2500)
    t += timedelta(minutes=2)
    d = e.on_bar(_bar(t, o=4999.0, h=5001.0, l=4998.5, c=5000.5),
                 bar_delta=-100, cum_delta_session=-2600)
    assert d.action == "enter"
    e.open_position(direction="long", entry_ts=d.bar_ts,
                    entry_fill_price=d.entry_price + 0.25,
                    atr_at_entry=d.atr_at_entry,
                    cum_delta_at_entry=d.cum_delta_at_entry)
    # 3 quiet bars (no opposite signal, no stop, no session end)
    last = None
    for _ in range(3):
        t += timedelta(minutes=2)
        last = e.on_bar(_bar(t, o=5000.5, h=5001.0, l=5000.4, c=5000.6),
                        bar_delta=0, cum_delta_session=-2580)
    assert last is not None
    assert last.action == "exit"
    assert last.reason == "time_stop"


def test_engine_no_signal_when_position_open_and_held():
    """When in position, hold returns 'none' with reason='hold'."""
    e = RyanSpecV3Engine()
    t = _flat_warmup(e)
    e.on_bar(_bar(t, o=5000.0, h=5000.5, l=4998.5, c=4999.0),
             bar_delta=-200, cum_delta_session=-2500)
    t += timedelta(minutes=2)
    d = e.on_bar(_bar(t, o=4999.0, h=5001.0, l=4998.5, c=5000.5),
                 bar_delta=-100, cum_delta_session=-2600)
    e.open_position(direction="long", entry_ts=d.bar_ts,
                    entry_fill_price=d.entry_price + 0.25,
                    atr_at_entry=d.atr_at_entry,
                    cum_delta_at_entry=d.cum_delta_at_entry)
    t += timedelta(minutes=2)
    d2 = e.on_bar(_bar(t, o=5000.5, h=5001.0, l=5000.4, c=5000.6),
                  bar_delta=0, cum_delta_session=-2580)
    assert d2.action == "none"
    assert d2.reason == "hold"


def test_session_end_uses_ct_not_caller_tz():
    """A UTC-tagged bar at 15:22 UTC is 09:22 CT — well before the 14:50 CT
    session-end. Engine must NOT fire session_end on it. Regression for the
    bug where session_end_ct (14:50) was naively compared against bar.t in
    the caller's tz, causing immediate session_end exits when ProjectX bars
    (which arrive UTC-tagged) were fed in."""
    e = RyanSpecV3Engine()
    # Warm up in CT so we can open a position cleanly
    t = _flat_warmup(e)
    e.on_bar(_bar(t, o=5000.0, h=5000.5, l=4998.5, c=4999.0),
             bar_delta=-200, cum_delta_session=-2500)
    t += timedelta(minutes=2)
    d = e.on_bar(_bar(t, o=4999.0, h=5001.0, l=4998.5, c=5000.5),
                 bar_delta=-100, cum_delta_session=-2600)
    e.open_position(direction="long", entry_ts=d.bar_ts,
                    entry_fill_price=d.entry_price + 0.25,
                    atr_at_entry=d.atr_at_entry,
                    cum_delta_at_entry=d.cum_delta_at_entry)

    # Now feed a UTC-tagged bar at 15:22 UTC = 09:22 CT (mid-session).
    # Pre-fix this would have triggered session_end immediately because
    # 15:22 UTC > 14:50 (interpreted as UTC). Post-fix it must not.
    UTC = timezone(timedelta(hours=0))
    utc_bar_t = datetime(2026, 5, 4, 15, 22, tzinfo=UTC)
    d2 = e.on_bar(_bar(utc_bar_t, o=5000.5, h=5001.0, l=5000.4, c=5000.6),
                  bar_delta=0, cum_delta_session=-2580)
    assert d2.reason != "session_end"

    # And a UTC-tagged bar at 20:51 UTC = 14:51 CT (after session end)
    # MUST trigger session_end.
    utc_after_close = datetime(2026, 5, 4, 20, 51, tzinfo=UTC)
    d3 = e.on_bar(_bar(utc_after_close, o=5000.6, h=5001.0, l=5000.4, c=5000.5),
                  bar_delta=0, cum_delta_session=-2580)
    assert d3.action == "exit"
    assert d3.reason == "session_end"
