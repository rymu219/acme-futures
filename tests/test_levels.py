"""Tests for the session-level computation module."""
from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from acme.broker.base import Bar
from acme.levels import (
    CT,
    compute_day_levels,
    nearest_level_distance,
    trading_date_ct,
)


def _ct(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=CT)


def _bar(ts_ct: datetime, *, h: float, l: float, c: float | None = None,  # noqa: E741 — matches Bar.l field
         v: int = 100) -> Bar:
    return Bar(t=ts_ct.astimezone(UTC), o=c if c is not None else (h + l) / 2,
               h=h, l=l, c=c if c is not None else (h + l) / 2, v=v)


# ════════════ trading_date_ct ═════════════════════════════════════════


def test_trading_date_before_globex_open_is_today():
    # 14:00 CT on Monday → trading date is Monday
    ts = _ct(date(2026, 5, 4), time(14, 0))
    assert trading_date_ct(ts) == date(2026, 5, 4)


def test_trading_date_at_globex_open_rolls_to_tomorrow():
    # 17:00 CT on Monday → trading date is Tuesday
    ts = _ct(date(2026, 5, 4), time(17, 0))
    assert trading_date_ct(ts) == date(2026, 5, 5)


def test_trading_date_overnight_belongs_to_tomorrow():
    # 22:00 CT on Monday → trading date is Tuesday
    ts = _ct(date(2026, 5, 4), time(22, 0))
    assert trading_date_ct(ts) == date(2026, 5, 5)


# ════════════ compute_day_levels ══════════════════════════════════════


def test_compute_levels_with_full_data():
    """Synthetic bars covering prior RTH, overnight, and current opening
    range. Verify each level resolves to the expected max/min."""
    trade_date = date(2026, 5, 5)
    prior = date(2026, 5, 4)

    # Prior day RTH: 08:30-15:00 CT — high=200, low=100
    bars: list[Bar] = []
    bars.append(_bar(_ct(prior, time(9, 0)), h=200, l=190))
    bars.append(_bar(_ct(prior, time(12, 0)), h=150, l=100))
    # Prior day after RTH close (15:00-17:00) — should be IGNORED
    bars.append(_bar(_ct(prior, time(16, 0)), h=999, l=999))
    # Overnight (17:00 prior - 08:30 current) — high=250, low=120
    bars.append(_bar(_ct(prior, time(18, 0)), h=250, l=240))
    bars.append(_bar(_ct(trade_date, time(2, 0)), h=140, l=120))
    bars.append(_bar(_ct(trade_date, time(7, 0)), h=160, l=150))
    # Opening range (08:30-09:00 current) — high=180, low=170
    bars.append(_bar(_ct(trade_date, time(8, 30)), h=175, l=170))
    bars.append(_bar(_ct(trade_date, time(8, 45)), h=180, l=172))
    # After OR — should NOT affect ORH/ORL
    bars.append(_bar(_ct(trade_date, time(10, 0)), h=300, l=160))

    levels = compute_day_levels(bars, trade_date, contract="MES")
    assert levels.pdh == 200
    assert levels.pdl == 100
    assert levels.onh == 250
    assert levels.onl == 120
    assert levels.orh == 180
    assert levels.orl == 170


def test_compute_levels_handles_missing_sessions():
    """No prior-day bars → pdh/pdl are None; everything else still works."""
    trade_date = date(2026, 5, 5)
    bars = [
        _bar(_ct(trade_date, time(8, 30)), h=180, l=170),
        _bar(_ct(date(2026, 5, 4), time(20, 0)), h=160, l=140),
    ]
    levels = compute_day_levels(bars, trade_date)
    assert levels.pdh is None
    assert levels.pdl is None
    assert levels.onh == 160
    assert levels.onl == 140
    assert levels.orh == 180
    assert levels.orl == 170


def test_compute_levels_returns_all_none_on_empty():
    levels = compute_day_levels([], date(2026, 5, 5))
    assert all(v is None for v in (
        levels.pdh, levels.pdl, levels.onh, levels.onl, levels.orh, levels.orl
    ))


# ════════════ nearest_level_distance ══════════════════════════════════


def test_nearest_level_finds_closest():
    levels = compute_day_levels([
        _bar(_ct(date(2026, 5, 4), time(9, 0)), h=100.0, l=99.0),
    ], date(2026, 5, 5))
    # Only PDH=100, PDL=99 valid.
    name, ticks = nearest_level_distance(99.05, levels, tick_size=0.25)
    assert name == "pdl"
    # 99.05 is 0.05 points above 99.0 → 0.2 ticks
    assert ticks == pytest.approx(0.2)


def test_nearest_level_above_returns_negative_ticks():
    levels = compute_day_levels([
        _bar(_ct(date(2026, 5, 4), time(9, 0)), h=100.0, l=99.0),
    ], date(2026, 5, 5))
    # price below PDH means PDH > price → ticks negative
    name, ticks = nearest_level_distance(99.95, levels, tick_size=0.25)
    assert name == "pdh"
    assert ticks == pytest.approx(-0.2)


def test_nearest_level_none_when_no_levels():
    levels = compute_day_levels([], date(2026, 5, 5))
    name, ticks = nearest_level_distance(100.0, levels, tick_size=0.25)
    assert name is None
    assert ticks is None
