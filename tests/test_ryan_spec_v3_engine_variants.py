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


# --- asymmetric short-side pctile (v4-loose-shorts) -----------------------

def test_pctile_short_defaults_to_symmetric():
    """When filter_pctile_short is unset, both legs use filter_pctile (the
    pre-v4 behavior — backward compatible)."""
    e = RyanSpecV3Engine(filter_mode="pctile",
                         filter_pctile_window_bars=30,
                         filter_pctile=10.0)
    # Drive 40 bars with monotonically rising cum_delta so the rolling list is
    # well-spaced and the percentile boundaries are unambiguous.
    t = datetime(2026, 5, 5, 9, 0, tzinfo=CT)
    for i in range(40):
        e.on_bar(_bar(t, o=5000.0, h=5000.5, l=4999.5, c=5000.0),
                 bar_delta=0, cum_delta_session=i * 10)
        t += timedelta(minutes=2)
    # Long-side threshold uses 10th percentile (low tail).
    long_thresh = e._compute_pctile_threshold("long")
    # Short-side threshold uses 90th percentile (high tail) — symmetric default.
    short_thresh = e._compute_pctile_threshold("short")
    # Sanity: low tail < high tail and both inside the rolling-window range.
    # The window holds the last 30 bars (i=10..39), so values are [100..390].
    assert 100 <= long_thresh < short_thresh <= 390
    # Symmetry check: on linearly-spaced data the 10th and 90th percentile
    # values are equidistant from the midpoint, so long+short ≈ min+max.
    assert abs((long_thresh + short_thresh) - (100 + 390)) < 1.0


def test_pctile_short_loosened_makes_short_qualify_more_easily():
    """v4-loose-shorts: filter_pctile_short=20 means the short threshold sits
    at the 80th percentile instead of the 90th. Short-side block is therefore
    LESS strict — values that would have been blocked at the 90th now pass."""
    # Same data as above but with filter_pctile_short=20 → 80th-percentile cut.
    e = RyanSpecV3Engine(filter_mode="pctile",
                         filter_pctile_window_bars=30,
                         filter_pctile=10.0,
                         filter_pctile_short=20.0)
    t = datetime(2026, 5, 5, 9, 0, tzinfo=CT)
    for i in range(40):
        e.on_bar(_bar(t, o=5000.0, h=5000.5, l=4999.5, c=5000.0),
                 bar_delta=0, cum_delta_session=i * 10)
        t += timedelta(minutes=2)
    long_thresh = e._compute_pctile_threshold("long")
    short_thresh_loose = e._compute_pctile_threshold("short")
    # Reference: an engine with the same data but symmetric (no _short override)
    # would have its short threshold at the 90th percentile.
    sym = RyanSpecV3Engine(filter_mode="pctile",
                           filter_pctile_window_bars=30, filter_pctile=10.0)
    t = datetime(2026, 5, 5, 9, 0, tzinfo=CT)
    for i in range(40):
        sym.on_bar(_bar(t, o=5000.0, h=5000.5, l=4999.5, c=5000.0),
                   bar_delta=0, cum_delta_session=i * 10)
        t += timedelta(minutes=2)
    short_thresh_sym = sym._compute_pctile_threshold("short")
    # Loosened (80th pctile) sits BELOW the symmetric 90th pctile reference.
    # That makes the short-side block fire less often — values that were
    # blocked at the 90th can now pass at the 80th.
    assert short_thresh_loose < short_thresh_sym
    # Long side untouched — still uses filter_pctile=10.
    assert long_thresh < short_thresh_loose


# --- v3.1 safeguards (PR-G) ---------------------------------------------

def test_entry_atr_ceiling_blocks_high_vol_entries():
    """When entry_atr_ceiling is set, entries above the ceiling are skipped
    even though every other condition would have produced an enter decision."""
    e = RyanSpecV3Engine(entry_atr_ceiling=2.5)
    # Warm up with WIDE bars so ATR climbs above 2.5
    t = datetime(2026, 5, 5, 9, 0, tzinfo=CT)
    for _ in range(22):
        e.on_bar(_bar(t, o=5000.0, h=5004.0, l=4996.0, c=5000.0),
                 bar_delta=0, cum_delta_session=0)
        t += timedelta(minutes=2)
    # Drive entry trigger
    e.on_bar(_bar(t, o=5000.0, h=5004.0, l=4995.0, c=4998.0),
             bar_delta=-200, cum_delta_session=-2500)
    t += timedelta(minutes=2)
    d = e.on_bar(_bar(t, o=4998.0, h=5002.0, l=4995.0, c=5001.0),
                 bar_delta=-100, cum_delta_session=-2600)
    assert d.action == "none"
    assert "entry_atr_ceiling_blocked" in d.reason


def test_entry_atr_ceiling_default_off_lets_high_vol_through():
    """Default ceiling=None preserves canonical behavior — high-ATR entries fire."""
    e = RyanSpecV3Engine()  # ceiling=None
    t = datetime(2026, 5, 5, 9, 0, tzinfo=CT)
    for _ in range(22):
        e.on_bar(_bar(t, o=5000.0, h=5004.0, l=4996.0, c=5000.0),
                 bar_delta=0, cum_delta_session=0)
        t += timedelta(minutes=2)
    e.on_bar(_bar(t, o=5000.0, h=5004.0, l=4995.0, c=4998.0),
             bar_delta=-200, cum_delta_session=-2500)
    t += timedelta(minutes=2)
    d = e.on_bar(_bar(t, o=4998.0, h=5002.0, l=4995.0, c=5001.0),
                 bar_delta=-100, cum_delta_session=-2600)
    assert d.action == "enter"


def test_entry_hour_blacklist_blocks_configured_hours():
    """When the entry bar's CT hour is in the blacklist, the entry is skipped."""
    # Engine session_tz defaults to CT; this engine's CT happens to be UTC-6
    # in our test fixture (no DST). Build a setup where the entry bar lands
    # at 11:00 CT — a blacklisted hour.
    e = RyanSpecV3Engine(entry_hour_blacklist_ct=(6, 7, 8, 11, 12))
    # Warmup at 09:00 CT, then advance into 11:00 territory.
    t = _flat_warmup(e, start_ct=datetime(2026, 5, 5, 10, 30, tzinfo=CT))
    # After 22 warmup bars at 2-min cadence, t is at 11:14 CT — in blacklist.
    e.on_bar(_bar(t, o=5000.0, h=5000.5, l=4998.5, c=4999.0),
             bar_delta=-200, cum_delta_session=-2500)
    t += timedelta(minutes=2)
    d = e.on_bar(_bar(t, o=4999.0, h=5001.0, l=4998.5, c=5000.5),
                 bar_delta=-100, cum_delta_session=-2600)
    assert d.action == "none"
    assert "entry_hour_blacklist_blocked" in d.reason


def test_entry_hour_blacklist_off_by_default():
    """Default empty tuple preserves canonical behavior."""
    e = RyanSpecV3Engine()
    t = _flat_warmup(e, start_ct=datetime(2026, 5, 5, 10, 30, tzinfo=CT))
    e.on_bar(_bar(t, o=5000.0, h=5000.5, l=4998.5, c=4999.0),
             bar_delta=-200, cum_delta_session=-2500)
    t += timedelta(minutes=2)
    d = e.on_bar(_bar(t, o=4999.0, h=5001.0, l=4998.5, c=5000.5),
                 bar_delta=-100, cum_delta_session=-2600)
    assert d.action == "enter"


def test_bar1_fast_fail_exits_when_mae_dominates_mfe():
    """At bar 1, if MAE > MFE × 1.5, the fast-fail rule exits with reason
    `bar1_fast_fail`. This is the v3.1 refinement that caps loss size on
    trades that go against immediately."""
    e = RyanSpecV3Engine(enable_bar1_fast_fail=True,
                         bar1_fast_fail_mae_mfe_ratio=1.5)
    t = _flat_warmup(e)
    t, atr = _open_long(e, t)
    entry = e.position.entry_fill
    # Bar 1 of the held position: minimal favorable, large adverse move.
    # MFE = 0.2 (high just barely above entry); MAE = 1.5 (low well below).
    # 1.5 > 0.2 × 1.5 = 0.3 → fast-fail triggers.
    t += timedelta(minutes=2)
    d = e.on_bar(_bar(t, o=entry, h=entry + 0.2, l=entry - 1.5,
                      c=entry - 1.0),
                 bar_delta=0, cum_delta_session=-2700)
    assert d.action == "exit"
    assert d.reason == "bar1_fast_fail"


def test_bar1_fast_fail_does_not_trigger_when_favorable():
    """If MFE dominates by bar 1 (the winners-look-like-this profile), the
    fast-fail rule must NOT fire."""
    e = RyanSpecV3Engine(enable_bar1_fast_fail=True)
    t = _flat_warmup(e)
    t, atr = _open_long(e, t)
    entry = e.position.entry_fill
    # MFE = 1.5 (price went up); MAE = 0.2 (small dip). Wins-like.
    t += timedelta(minutes=2)
    d = e.on_bar(_bar(t, o=entry, h=entry + 1.5, l=entry - 0.2,
                      c=entry + 1.0),
                 bar_delta=0, cum_delta_session=-2700)
    # Should NOT exit on fast-fail. May still hold or hit other exits;
    # critically, the reason should not be bar1_fast_fail.
    assert d.reason != "bar1_fast_fail"


def test_bar1_fast_fail_off_by_default():
    """Default off — even an obvious bar-1 loss should not trigger the
    fast-fail exit on a vanilla canon engine."""
    e = RyanSpecV3Engine()
    t = _flat_warmup(e)
    t, atr = _open_long(e, t)
    entry = e.position.entry_fill
    t += timedelta(minutes=2)
    d = e.on_bar(_bar(t, o=entry, h=entry + 0.2, l=entry - 1.5,
                      c=entry - 1.0),
                 bar_delta=0, cum_delta_session=-2700)
    assert d.reason != "bar1_fast_fail"


# --- MFE / MAE tracking (always-on observability, not a variant) ---------

def test_mfe_and_mae_both_ratchet_independently():
    """MFE tracks the highest favourable price excursion; MAE tracks the
    worst adverse. Both move forward only — never decrease."""
    e = RyanSpecV3Engine()
    t = _flat_warmup(e)
    t, atr = _open_long(e, t)
    entry = e.position.entry_fill
    # Bar 1: bar swings both ways (high above entry, low below)
    t += timedelta(minutes=2)
    e.on_bar(_bar(t, o=entry + 0.5, h=entry + 5.0, l=entry - 3.0,
                  c=entry + 1.0),
             bar_delta=0, cum_delta_session=-2600)
    assert e.position.max_favorable_excursion == 5.0
    assert e.position.max_adverse_excursion == 3.0
    # Bar 2: more adverse, less favourable — MAE grows, MFE holds
    t += timedelta(minutes=2)
    e.on_bar(_bar(t, o=entry + 1.0, h=entry + 2.0, l=entry - 4.5,
                  c=entry - 2.0),
             bar_delta=0, cum_delta_session=-2700)
    assert e.position.max_favorable_excursion == 5.0  # holds
    assert e.position.max_adverse_excursion == 4.5    # ratcheted up


def test_mae_uses_correct_direction_for_short():
    """For shorts, MAE is the highest the bar high reached above entry
    (price went against us = up); MFE is the lowest the bar low reached."""
    # Build a clean SHORT position. Use the short trigger setup.
    e = RyanSpecV3Engine()
    t = _flat_warmup(e)
    # Prior bar UP, current bar DOWN with close < prior close — short trigger
    e.on_bar(_bar(t, o=5000.0, h=5001.5, l=4999.5, c=5001.0),
             bar_delta=200, cum_delta_session=2500)
    t += timedelta(minutes=2)
    d = e.on_bar(_bar(t, o=5001.0, h=5001.5, l=4998.5, c=4999.5),
                 bar_delta=100, cum_delta_session=2600)
    assert d.action == "enter" and d.direction == "short"
    e.open_position(direction="short", entry_ts=d.bar_ts,
                    entry_fill_price=d.entry_price,
                    atr_at_entry=d.atr_at_entry,
                    cum_delta_at_entry=d.cum_delta_at_entry)
    entry = e.position.entry_fill
    # Bar: high goes 4 above entry (adverse for short), low goes 2 below (favourable)
    t += timedelta(minutes=2)
    e.on_bar(_bar(t, o=entry, h=entry + 4.0, l=entry - 2.0, c=entry - 1.0),
             bar_delta=0, cum_delta_session=2700)
    assert e.position.max_favorable_excursion == 2.0  # price went 2 below entry
    assert e.position.max_adverse_excursion == 4.0    # price went 4 above entry


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
