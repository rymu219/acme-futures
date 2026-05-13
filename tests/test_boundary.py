"""Tests for the BOUNDARY strategy."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from acme.broker.base import Bar
from acme.levels import DayLevels
from acme.risk import TOPSTEP_50K, DailyState
from acme.strategies.boundary import BoundaryConfig, BoundaryStrategy

CT = ZoneInfo("America/Chicago")


def _state():
    return DailyState(
        trade_date=datetime.now(UTC).date(),
        starting_balance=50_000.0,
        peak_balance_eod=50_000.0,
        max_loss_limit=48_000.0,
        daily_loss_limit=1_000.0,
    )


def _bar(t, *, o, h, l, c, v=100):  # noqa: E741 — matches Bar.l field
    return Bar(t=t, o=o, h=h, l=l, c=c, v=v)


def _filler(t, *, c=100.0, v=100):
    return _bar(t, o=c, h=c + 0.1, l=c - 0.1, c=c, v=v)


T0 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _make_levels(*, pdh=110.0, pdl=90.0, onh=None, onl=None, orh=None, orl=None):
    return DayLevels(
        trade_date=date(2026, 5, 1), contract="MES",
        pdh=pdh, pdl=pdl, onh=onh, onl=onl, orh=orh, orl=orl,
    )


# ════════════ basics ══════════════════════════════════════════════════


def test_metadata_defaults():
    s = BoundaryStrategy()
    assert s.metadata.default_lifecycle == "SHADOW"
    assert s.name == "boundary"


def test_no_entry_without_levels():
    s = BoundaryStrategy()
    state = _state()
    for i in range(15):
        sig = s.on_bar(_filler(T0 + timedelta(minutes=2 * i)),
                       state=state, profile=TOPSTEP_50K,
                       current_position=0, current_balance_unrealized=50_000)
        assert sig is None


def test_no_entry_during_warmup():
    s = BoundaryStrategy()
    s.set_levels(_make_levels())
    state = _state()
    for i in range(5):
        sig = s.on_bar(_filler(T0 + timedelta(minutes=2 * i)),
                       state=state, profile=TOPSTEP_50K,
                       current_position=0, current_balance_unrealized=50_000)
        assert sig is None


# ════════════ short at PDH (high-side fade) ═══════════════════════════


def test_short_fade_at_pdh():
    # PD class is off by default (2-year backtest: PF 0.69, -$289).
    # This test exercises the PD-fade mechanic, so opt in explicitly.
    s = BoundaryStrategy(config=BoundaryConfig(
        level_classes_enabled=("pd", "on", "or"),
    ))
    s.set_levels(_make_levels(pdh=110.0))
    state = _state()
    # Warm 10 bars at price 100
    for i in range(22):
        s.on_bar(_filler(T0 + timedelta(minutes=2 * i)),
                 state=state, profile=TOPSTEP_50K,
                 current_position=0, current_balance_unrealized=50_000)
    # Bar 11: ramp up to 110 — local high near PDH=110
    s.on_bar(_bar(T0 + timedelta(minutes=2 * 23),
                  o=109.5, h=110.0, l=109.4, c=109.9, v=100),
             state=state, profile=TOPSTEP_50K,
             current_position=0, current_balance_unrealized=50_000)
    # Exhaustion top bar at h=110.0, doji body (0.1/1.0=0.1), low volume,
    # close in lower half (midpoint 109.5; close 109.25 < midpoint).
    sig = s.on_bar(_bar(T0 + timedelta(minutes=2 * 24),
                        o=109.35, h=110.0, l=109.0, c=109.25, v=40),
                   state=state, profile=TOPSTEP_50K,
                   current_position=0, current_balance_unrealized=50_000)
    assert sig is not None
    assert sig.side == "sell"
    assert "pdh" in sig.reason or "boundary_fade" in sig.reason


def test_no_short_when_high_is_far_from_level():
    """Exhaustion top but the local high is 5 points away from PDH."""
    s = BoundaryStrategy()
    s.set_levels(_make_levels(pdh=120.0))   # PDH way above
    state = _state()
    for i in range(22):
        s.on_bar(_filler(T0 + timedelta(minutes=2 * i)),
                 state=state, profile=TOPSTEP_50K,
                 current_position=0, current_balance_unrealized=50_000)
    s.on_bar(_bar(T0 + timedelta(minutes=2 * 23),
                  o=109.5, h=110.0, l=109.4, c=109.9, v=100),
             state=state, profile=TOPSTEP_50K,
             current_position=0, current_balance_unrealized=50_000)
    sig = s.on_bar(_bar(T0 + timedelta(minutes=2 * 24),
                        o=109.65, h=110.0, l=109.0, c=109.25, v=40),
                   state=state, profile=TOPSTEP_50K,
                   current_position=0, current_balance_unrealized=50_000)
    assert sig is None


# ════════════ long at PDL (low-side fade) ═════════════════════════════


def test_long_fade_at_pdl():
    # PD class is off by default; opt in for this PDL-fade test.
    s = BoundaryStrategy(config=BoundaryConfig(
        level_classes_enabled=("pd", "on", "or"),
    ))
    s.set_levels(_make_levels(pdl=90.0))
    state = _state()
    for i in range(22):
        s.on_bar(_filler(T0 + timedelta(minutes=2 * i)),
                 state=state, profile=TOPSTEP_50K,
                 current_position=0, current_balance_unrealized=50_000)
    s.on_bar(_bar(T0 + timedelta(minutes=2 * 23),
                  o=90.5, h=90.6, l=90.0, c=90.1, v=100),
             state=state, profile=TOPSTEP_50K,
             current_position=0, current_balance_unrealized=50_000)
    # Bottom exhaustion: l=90.0 near PDL=90.0, doji (body 0.1), low vol,
    # close in upper half (midpoint 90.5; close 90.75 > midpoint).
    sig = s.on_bar(_bar(T0 + timedelta(minutes=2 * 24),
                        o=90.65, h=91.0, l=90.0, c=90.75, v=40),
                   state=state, profile=TOPSTEP_50K,
                   current_position=0, current_balance_unrealized=50_000)
    assert sig is not None
    assert sig.side == "buy"


# ════════════ direction policies ══════════════════════════════════════


def test_long_only_blocks_short():
    cfg = BoundaryConfig(allow_shorts=False)
    s = BoundaryStrategy(config=cfg)
    s.set_levels(_make_levels(pdh=110.0))
    state = _state()
    for i in range(22):
        s.on_bar(_filler(T0 + timedelta(minutes=2 * i)),
                 state=state, profile=TOPSTEP_50K,
                 current_position=0, current_balance_unrealized=50_000)
    s.on_bar(_bar(T0 + timedelta(minutes=2 * 23),
                  o=109.5, h=110.0, l=109.4, c=109.9, v=100),
             state=state, profile=TOPSTEP_50K,
             current_position=0, current_balance_unrealized=50_000)
    sig = s.on_bar(_bar(T0 + timedelta(minutes=2 * 24),
                        o=109.65, h=110.0, l=109.0, c=109.25, v=40),
                   state=state, profile=TOPSTEP_50K,
                   current_position=0, current_balance_unrealized=50_000)
    assert sig is None


# ════════════ already-in-position guard ═══════════════════════════════


def test_no_signal_when_already_in_position():
    s = BoundaryStrategy()
    s.set_levels(_make_levels(pdh=110.0))
    state = _state()
    for i in range(22):
        s.on_bar(_filler(T0 + timedelta(minutes=2 * i)),
                 state=state, profile=TOPSTEP_50K,
                 current_position=0, current_balance_unrealized=50_000)
    s.on_bar(_bar(T0 + timedelta(minutes=2 * 23),
                  o=109.5, h=110.0, l=109.4, c=109.9, v=100),
             state=state, profile=TOPSTEP_50K,
             current_position=0, current_balance_unrealized=50_000)
    # Already long 1 — should not emit anything even on a clean fade setup
    sig = s.on_bar(_bar(T0 + timedelta(minutes=2 * 24),
                        o=109.35, h=110.0, l=109.0, c=109.25, v=40),
                   state=state, profile=TOPSTEP_50K,
                   current_position=1, current_balance_unrealized=50_000)
    assert sig is None


# ════════════ fleet-coordination close times ═══════════════════════════


def _bar_at_ct(hh: int, mm: int) -> Bar:
    """Bar starting at (hh, mm) CT on an arbitrary recent weekday."""
    ct_t = datetime(2026, 5, 13, hh, mm, tzinfo=CT)
    return _bar(ct_t.astimezone(UTC), o=100, h=100, l=100, c=100, v=1)


def test_wants_force_flat_fires_at_1659_ct():
    """16:58→17:00 CT bar contains 16:59 → wants_force_flat is True."""
    s = BoundaryStrategy()
    assert s.wants_force_flat(_bar_at_ct(16, 58)) is True


def test_wants_force_flat_fires_at_0828_ct():
    """08:28→08:30 CT bar contains 08:29 → wants_force_flat is True."""
    s = BoundaryStrategy()
    assert s.wants_force_flat(_bar_at_ct(8, 28)) is True


def test_wants_force_flat_silent_at_other_times():
    """No coordination time falls inside 12:00→12:02 CT — silent."""
    s = BoundaryStrategy()
    assert s.wants_force_flat(_bar_at_ct(12, 0)) is False
    assert s.wants_force_flat(_bar_at_ct(17, 0)) is False
    assert s.wants_force_flat(_bar_at_ct(8, 30)) is False


def test_wants_force_flat_can_be_disabled():
    """Empty tuple disables the rule — preserves pre-coordination behavior."""
    s = BoundaryStrategy(config=BoundaryConfig(
        fleet_coordination_close_times_ct=(),
    ))
    assert s.wants_force_flat(_bar_at_ct(16, 58)) is False
    assert s.wants_force_flat(_bar_at_ct(8, 28)) is False
