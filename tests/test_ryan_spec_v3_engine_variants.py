"""Tests for Ryan-Spec v3 engine variants (the four tonight's-build flags).

Covers:
  - Trail (option A): MFE tracking, BE-lock at +1 ATR, trail at +2 ATR, monotone ratchet
  - min_bars_before_opposite_exit (option B)
  - opposite_signal_armor_mfe_atr (option C)
  - filter_mode='pctile' (option D)

Mirrors test_ryan_spec_v3_engine.py's style — uses CT-tagged datetimes,
warmup helper, etc.
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
                 start_ct: datetime | None = None,
                 cum_delta: int = 0) -> datetime:
    if start_ct is None:
        start_ct = datetime(2026, 5, 5, 9, 0, tzinfo=CT)
    t = start_ct
    for _ in range(n_bars):
        engine.on_bar(_bar(t, o=base_price, h=base_price + 0.5,
                           l=base_price - 0.5, c=base_price),
                      bar_delta=0, cum_delta_session=cum_delta)
        t += timedelta(minutes=2)
    return t


def _open_long(engine: RyanSpecV3Engine, t: datetime, base_price: float = 5000.0
               ) -> tuple[datetime, float]:
    """Run the entry sequence and open a long. Returns (next_t, atr_at_entry)."""
    engine.on_bar(_bar(t, o=base_price, h=base_price + 0.5,
                       l=base_price - 0.5, c=base_price - 1.0),
                  bar_delta=-200, cum_delta_session=-2500)
    t += timedelta(minutes=2)
    d = engine.on_bar(_bar(t, o=base_price - 1.0, h=base_price + 1.0,
                           l=base_price - 1.5, c=base_price + 0.5),
                      bar_delta=-100, cum_delta_session=-2600)
    assert d.action == "enter"
    assert d.direction == "long"
    engine.open_position(direction="long", entry_ts=d.bar_ts,
                         entry_fill_price=d.entry_price,
                         atr_at_entry=d.atr_at_entry,
                         cum_delta_at_entry=d.cum_delta_at_entry)
    return t, d.atr_at_entry


# --- Trail (option A) ---------------------------------------------------

def test_trail_off_by_default_stops_at_canon_level():
    """Without enable_trailing_stop, MFE-driven ratchet doesn't fire."""
    e = RyanSpecV3Engine()  # canon
    t = _flat_warmup(e)
    t, atr = _open_long(e, t)
    initial_stop = e.position.stop_price
    # Drive a strongly favourable bar — MFE should grow but stop should NOT move
    t += timedelta(minutes=2)
    e.on_bar(_bar(t, o=5000.5, h=5010.0, l=5000.0, c=5008.0),
             bar_delta=0, cum_delta_session=-2600)
    assert e.position.stop_price == initial_stop


def test_trail_locks_at_be_after_one_atr_mfe():
    """With enable_trailing_stop=True, MFE crossing +1 ATR ratchets stop to entry."""
    e = RyanSpecV3Engine(enable_trailing_stop=True, trail_be_lock_atr_mult=1.0)
    t = _flat_warmup(e)
    t, atr = _open_long(e, t)
    entry = e.position.entry_fill
    initial_stop = e.position.stop_price
    assert initial_stop < entry  # long: stop below entry
    # Bar with high reaching entry + 1.5 × ATR — comfortably past 1 ATR MFE
    t += timedelta(minutes=2)
    e.on_bar(_bar(t, o=entry + 0.5, h=entry + 1.5 * atr, l=entry,
                  c=entry + 1.0 * atr),
             bar_delta=0, cum_delta_session=-2600)
    assert e.position.stop_price == entry  # ratcheted to BE


def test_trail_engages_after_two_atr_mfe():
    """Once MFE >= 2 ATR, stop trails at trail_atr_mult × ATR behind MFE."""
    e = RyanSpecV3Engine(enable_trailing_stop=True, trail_atr_mult=1.0)
    t = _flat_warmup(e)
    t, atr = _open_long(e, t)
    entry = e.position.entry_fill
    # Bar with high = entry + 3 × ATR — well past 2 ATR
    t += timedelta(minutes=2)
    e.on_bar(_bar(t, o=entry + 0.5, h=entry + 3 * atr, l=entry,
                  c=entry + 2 * atr),
             bar_delta=0, cum_delta_session=-2600)
    # MFE = 3 × ATR; trail_offset = 1 × ATR; high_water = entry + 3 ATR
    # trail_stop = high_water - 1 ATR = entry + 2 ATR
    expected = entry + 2 * atr
    assert abs(e.position.stop_price - expected) < 0.01


def test_trail_is_monotone_never_loosens():
    """Successive bars with smaller MFE don't lower the stop."""
    e = RyanSpecV3Engine(enable_trailing_stop=True)
    t = _flat_warmup(e)
    t, atr = _open_long(e, t)
    entry = e.position.entry_fill
    # First bar: MFE = 3 × ATR → stop ratchets to entry + 2 ATR
    t += timedelta(minutes=2)
    e.on_bar(_bar(t, o=entry + 0.5, h=entry + 3 * atr, l=entry,
                  c=entry + 2 * atr),
             bar_delta=0, cum_delta_session=-2600)
    high_stop = e.position.stop_price
    # Second bar: MFE doesn't grow further; stop must not move backward
    t += timedelta(minutes=2)
    e.on_bar(_bar(t, o=entry + 2 * atr, h=entry + 2 * atr,
                  l=entry + 1.5 * atr, c=entry + 1.5 * atr),
             bar_delta=0, cum_delta_session=-2600)
    assert e.position.stop_price == high_stop


# --- min_bars_before_opposite_exit (option B) ---------------------------

def test_min2bar_suppresses_opposite_at_bar_1():
    """Even if opposite_signal fires at bar 1, position holds."""
    e = RyanSpecV3Engine(min_bars_before_opposite_exit=2)
    t = _flat_warmup(e)
    t, atr = _open_long(e, t)
    # Bar that fires SHORT trigger (prior up + current down + close < prior close)
    t += timedelta(minutes=2)
    d = e.on_bar(_bar(t, o=5001.5, h=5002.0, l=5000.5, c=5000.5),
                 bar_delta=-50, cum_delta_session=-2580)
    # Without the flag this would exit on opposite_signal; with min2bar=2, hold
    assert d.action == "none"
    assert e.position is not None  # still in position


def test_min2bar_allows_opposite_at_bar_2():
    """At bars_held=2, opposite_signal exits normally."""
    e = RyanSpecV3Engine(min_bars_before_opposite_exit=2)
    t = _flat_warmup(e)
    t, atr = _open_long(e, t)
    # Bar 1: hold
    t += timedelta(minutes=2)
    e.on_bar(_bar(t, o=5000.5, h=5001.0, l=5000.4, c=5000.6),
             bar_delta=0, cum_delta_session=-2590)
    # Bar 2: opposite trigger fires. Need prior up + current down.
    t += timedelta(minutes=2)
    e.on_bar(_bar(t, o=5000.6, h=5002.0, l=5000.5, c=5001.5),
             bar_delta=50, cum_delta_session=-2540)
    t += timedelta(minutes=2)
    d = e.on_bar(_bar(t, o=5001.5, h=5002.0, l=5000.5, c=5001.0),
                 bar_delta=-30, cum_delta_session=-2570)
    assert d.action == "exit"
    assert d.reason == "opposite_signal"


def test_min2bar_does_not_block_stop_or_session_end():
    """min_bars only gates opposite_signal; stop / session_end / time_stop fire normally."""
    e = RyanSpecV3Engine(min_bars_before_opposite_exit=10)
    t = _flat_warmup(e)
    t, atr = _open_long(e, t)
    # Bar 1: drive low through stop → must exit on stop, NOT held by min_bars
    stop = e.position.stop_price
    t += timedelta(minutes=2)
    d = e.on_bar(_bar(t, o=5000.5, h=5001.0, l=stop - 0.5, c=stop - 0.1),
                 bar_delta=0, cum_delta_session=-2580)
    assert d.action == "exit"
    assert d.reason == "stop"


# --- opposite_signal_armor_mfe_atr (option C) ---------------------------

def test_armor_does_not_engage_below_threshold():
    """If MFE has not crossed armor × ATR, opposite_signal exits normally."""
    e = RyanSpecV3Engine(opposite_signal_armor_mfe_atr=2.0)
    t = _flat_warmup(e)
    t, atr = _open_long(e, t)
    # Bar 1 (modest MFE — well below 2 ATR), prior up
    t += timedelta(minutes=2)
    e.on_bar(_bar(t, o=5000.5, h=5001.0, l=5000.4, c=5000.8),
             bar_delta=20, cum_delta_session=-2570)
    # Bar 2 fires opposite trigger (current down vs prior up)
    t += timedelta(minutes=2)
    d = e.on_bar(_bar(t, o=5000.8, h=5001.0, l=5000.0, c=5000.2),
                 bar_delta=-30, cum_delta_session=-2600)
    assert d.action == "exit"
    assert d.reason == "opposite_signal"


def test_armor_suppresses_opposite_when_deeply_profitable():
    """When MFE >= armor × ATR, opposite_signal is ignored."""
    e = RyanSpecV3Engine(opposite_signal_armor_mfe_atr=2.0)
    t = _flat_warmup(e)
    t, atr = _open_long(e, t)
    entry = e.position.entry_fill
    # Bar 1: drive high to 3 × ATR above entry — armor activates (MFE >= 2 ATR)
    t += timedelta(minutes=2)
    e.on_bar(_bar(t, o=entry + 0.5, h=entry + 3 * atr, l=entry,
                  c=entry + 2.5 * atr),
             bar_delta=200, cum_delta_session=-2400)
    # Bar 2: opposite trigger fires (current red after prior green)
    t += timedelta(minutes=2)
    d = e.on_bar(_bar(t, o=entry + 2.5 * atr, h=entry + 2.5 * atr,
                      l=entry + 2 * atr, c=entry + 2.0 * atr),
                 bar_delta=-100, cum_delta_session=-2500)
    # Armor blocks the opposite_signal exit; position holds
    assert d.action != "exit" or d.reason != "opposite_signal"
    assert e.position is not None


# --- filter_mode='pctile' (option D) ------------------------------------

def test_pctile_filter_falls_back_to_static_until_warm():
    """When fewer than MIN_PCTILE_SAMPLES bars accumulated, pctile mode uses
    the static threshold so we don't enter on garbage during early bars."""
    e = RyanSpecV3Engine(filter_mode="pctile", filter_pctile_window_bars=200)
    t = _flat_warmup(e)
    # 22 warmup bars only. MIN_PCTILE_SAMPLES is 30 → fallback to static.
    # Two-bar reversal with cum_delta_in_dir = -2600 → static filter passes (< -2000)
    e.on_bar(_bar(t, o=5000.0, h=5000.5, l=4998.5, c=4999.0),
             bar_delta=-200, cum_delta_session=-2500)
    t += timedelta(minutes=2)
    d = e.on_bar(_bar(t, o=4999.0, h=5001.0, l=4998.5, c=5000.5),
                 bar_delta=-100, cum_delta_session=-2600)
    assert d.action == "enter"


def test_pctile_filter_uses_percentile_when_warm():
    """Once enough samples, the percentile threshold replaces the static one."""
    # Use a small window so we can warm it quickly in the test
    e = RyanSpecV3Engine(filter_mode="pctile",
                         filter_pctile_window_bars=30,
                         filter_pctile=10.0)
    # Warmup: 22 bars with cum_delta values clustered near zero
    t = datetime(2026, 5, 5, 9, 0, tzinfo=CT)
    for i in range(40):  # well over MIN_PCTILE_SAMPLES + warmup
        # Vary cum_delta tightly: ranges roughly -100 to +100
        cd = (-100 + (i * 17) % 200)
        e.on_bar(_bar(t, o=5000.0, h=5000.5, l=4999.5, c=5000.0),
                 bar_delta=0, cum_delta_session=cd)
        t += timedelta(minutes=2)
    # All historical cum_delta values are in [-100, +100]. The 10th percentile
    # is roughly -80. So a long entry needs cum_delta < ~-80.
    # Trigger a two-bar reversal with cum_delta = -2600 (very extreme; passes pctile)
    e.on_bar(_bar(t, o=5000.0, h=5000.5, l=4998.5, c=4999.0),
             bar_delta=-200, cum_delta_session=-50)  # not extreme enough for pctile
    t += timedelta(minutes=2)
    d = e.on_bar(_bar(t, o=4999.0, h=5001.0, l=4998.5, c=5000.5),
                 bar_delta=-100, cum_delta_session=-50)
    # Static filter would block (cum_delta=-50 not < -2000), but pctile filter
    # ALSO blocks because -50 isn't in the bottom 10% of recent values
    assert d.action == "none"
    assert "filter_blocked" in d.reason


def test_pctile_filter_passes_when_cum_delta_is_extreme_relative_to_recent():
    """A long entry should fire when current cum_delta is far below the
    bottom-pctile of recent values, even though it's not extreme by static
    standards."""
    e = RyanSpecV3Engine(filter_mode="pctile",
                         filter_pctile_window_bars=30,
                         filter_pctile=10.0)
    t = datetime(2026, 5, 5, 9, 0, tzinfo=CT)
    # Warmup: 40 bars with cum_delta values clustered tightly above zero
    for i in range(40):
        cd = 100 + (i * 13) % 200  # range roughly +100 to +300
        e.on_bar(_bar(t, o=5000.0, h=5000.5, l=4999.5, c=5000.0),
                 bar_delta=0, cum_delta_session=cd)
        t += timedelta(minutes=2)
    # Now drive a sharp reversal with cum_delta = -50 (far below the +100..+300
    # range, well into the bottom 10%). Static -670 would block it, but pctile
    # should pass.
    e.on_bar(_bar(t, o=5000.0, h=5000.5, l=4998.5, c=4999.0),
             bar_delta=-200, cum_delta_session=-50)
    t += timedelta(minutes=2)
    d = e.on_bar(_bar(t, o=4999.0, h=5001.0, l=4998.5, c=5000.5),
                 bar_delta=-100, cum_delta_session=-50)
    assert d.action == "enter"
    assert d.direction == "long"
