"""Tests for the live 2-min bar + cum-delta builder."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from zoneinfo import ZoneInfo

from acme.ryan_spec.v3_tick_delta import LiveBarDeltaBuilder

CT = timezone(timedelta(hours=-6))
CHI = ZoneInfo("America/Chicago")  # observes DST


def test_builder_emits_bar_on_bucket_flip():
    out = []
    b = LiveBarDeltaBuilder(on_bar=out.append)
    t0 = datetime(2026, 5, 5, 9, 0, tzinfo=CT)
    # 3 ticks in 9:00-9:01 bucket
    b.add_quote_tick(t0, 5000.0)
    b.add_quote_tick(t0 + timedelta(seconds=30), 5001.0)  # +1
    b.add_quote_tick(t0 + timedelta(seconds=59), 5000.5)  # -1
    # First tick of next bucket — closes the prior bar
    b.add_quote_tick(t0 + timedelta(minutes=2, seconds=1), 5001.0)
    assert len(out) == 1
    bar = out[0]
    assert bar.bar.t == t0
    assert bar.bar.o == 5000.0
    assert bar.bar.h == 5001.0
    assert bar.bar.l == 5000.0
    assert bar.bar.c == 5000.5
    # Tick rule with unit weight: +1, -1 → delta 0 (first tick has no prior, sign=0)
    assert bar.delta == 0


def test_builder_trade_mode_uses_size_and_side():
    out = []
    b = LiveBarDeltaBuilder(on_bar=out.append)
    t0 = datetime(2026, 5, 5, 9, 0, tzinfo=CT)
    b.add_trade(t0, 5000.0, size=10, side="B")
    b.add_trade(t0 + timedelta(seconds=10), 5001.0, size=5, side="A")
    b.add_trade(t0 + timedelta(seconds=20), 5000.5, size=3, side="B")
    # Flip bucket
    b.add_trade(t0 + timedelta(minutes=2, seconds=1), 5001.0, size=1, side="B")
    assert len(out) == 1
    bar = out[0]
    # 10 buy + (-5 sell) + 3 buy = +8
    assert bar.delta == 8
    assert bar.bar.v == 18  # total volume


def test_builder_cum_delta_resets_at_session_open():
    out = []
    b = LiveBarDeltaBuilder(on_bar=out.append)
    # Two bars BEFORE 08:30 CT — accumulate to cum_delta
    pre1 = datetime(2026, 5, 5, 7, 0, tzinfo=CT)
    b.add_trade(pre1, 5000.0, size=100, side="B")
    b.add_trade(pre1 + timedelta(minutes=1), 5001.0, size=100, side="B")
    # Force bar close
    b.force_close_current()
    assert len(out) == 1
    pre_cum = out[0].cum_delta_session
    assert pre_cum == 200

    # Bar AT 08:30 CT — should reset cum_delta to just this bar's delta
    open_ts = datetime(2026, 5, 5, 8, 30, tzinfo=CT)
    b.add_trade(open_ts, 5005.0, size=50, side="A")
    b.force_close_current()
    assert len(out) == 2
    post_cum = out[1].cum_delta_session
    assert post_cum == -50


def test_builder_tick_rule_carries_forward_on_equal_price():
    out = []
    b = LiveBarDeltaBuilder(on_bar=out.append)
    t0 = datetime(2026, 5, 5, 9, 0, tzinfo=CT)
    b.add_quote_tick(t0, 5000.0)                          # sign=0 (no prior)
    b.add_quote_tick(t0 + timedelta(seconds=10), 5001.0)  # +1, last_dir=+1
    b.add_quote_tick(t0 + timedelta(seconds=20), 5001.0)  # equal → carry +1
    b.add_quote_tick(t0 + timedelta(seconds=30), 5001.0)  # equal → carry +1
    # Bucket flip
    b.add_quote_tick(t0 + timedelta(minutes=2, seconds=1), 5002.0)
    assert len(out) == 1
    # +0 (first) +1 +1 +1 = +3
    assert out[0].delta == 3


def test_builder_force_close_emits_partial_bar():
    out = []
    b = LiveBarDeltaBuilder(on_bar=out.append)
    t0 = datetime(2026, 5, 5, 9, 0, tzinfo=CT)
    b.add_trade(t0, 5000.0, size=5, side="B")
    b.force_close_current()
    assert len(out) == 1
    assert out[0].bar.v == 5


def test_builder_session_open_ct_is_configurable():
    """Custom session_open_ct shifts the cum_delta reset boundary.

    Uses real Chicago time so the boundary is precise regardless of DST.
    """
    out = []
    b = LiveBarDeltaBuilder(on_bar=out.append, session_open_ct=dtime(9, 0))
    # Pre-09:00 Chicago — anchor is yesterday's 09:00
    pre = datetime(2026, 5, 5, 8, 45, tzinfo=CHI)
    b.add_trade(pre, 5000.0, size=100, side="B")
    b.force_close_current()
    # At 09:00 Chicago — anchor flips to today's 09:00 → reset
    post = datetime(2026, 5, 5, 9, 0, tzinfo=CHI)
    b.add_trade(post, 5005.0, size=50, side="A")
    b.force_close_current()
    assert len(out) == 2
    assert out[0].cum_delta_session == 100
    assert out[1].cum_delta_session == -50


def test_builder_default_session_open_holds_within_session():
    """Default 08:30 reset: 08:45 → 09:00 stays in the same Chicago session
    so cum_delta accumulates rather than resetting."""
    out = []
    b = LiveBarDeltaBuilder(on_bar=out.append)  # default 08:30
    pre = datetime(2026, 5, 5, 8, 45, tzinfo=CHI)
    b.add_trade(pre, 5000.0, size=100, side="B")
    b.force_close_current()
    post = datetime(2026, 5, 5, 9, 0, tzinfo=CHI)
    b.add_trade(post, 5005.0, size=50, side="A")
    b.force_close_current()
    assert out[0].cum_delta_session == 100
    assert out[1].cum_delta_session == 50  # 100 + (-50), no reset
