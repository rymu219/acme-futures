"""Tests for the SESSION strategy.

Coverage:
  - warm-up emits nothing
  - metadata defaults
  - entry fires inside window when overnight bias is up
  - no entry on chop bias inside window
  - no entry outside window even with clear bias
  - window-close emits an exit signal
  - long-only by default (down bias doesn't fire)
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from acme.broker.base import Bar
from acme.risk import TOPSTEP_50K, DailyState
from acme.strategies.session import AUDIT_WINDOWS, SessionConfig, SessionStrategy


def _state():
    return DailyState(
        trade_date=datetime.now(UTC).date(),
        starting_balance=50_000.0,
        peak_balance_eod=50_000.0,
        max_loss_limit=48_000.0,
        daily_loss_limit=1_000.0,
    )


def _bar(t, c, *, v=100, h_pad=0.1, l_pad=0.1):
    return Bar(t=t, o=c, h=c + h_pad, l=c - l_pad, c=c, v=v)


# 03:00 CT = 08:00 UTC (May is CDT, UTC-5)
T_WINDOW_OPEN = datetime(2026, 5, 15, 8, 0, tzinfo=UTC)
# 05:00 CT = 10:00 UTC — outside the 03-05 window
T_AFTER_WINDOW = datetime(2026, 5, 15, 10, 2, tzinfo=UTC)
# 13:00 CT = 18:00 UTC — outside both default windows
T_OUTSIDE = datetime(2026, 5, 15, 18, 0, tzinfo=UTC)


def _build_uptrend_bars(start: datetime, n: int = 50, start_price: float = 100.0,
                       step: float = 0.05) -> list[Bar]:
    """Bars climbing steadily — used to seed an overnight-up bias."""
    bars = []
    t = start
    for i in range(n):
        c = start_price + i * step
        bars.append(_bar(t, c))
        t += timedelta(minutes=2)
    return bars


def _build_downtrend_bars(start: datetime, n: int = 50, start_price: float = 100.0,
                         step: float = 0.05) -> list[Bar]:
    return _build_uptrend_bars(start, n=n, start_price=start_price, step=-step)


def _build_flat_bars(start: datetime, n: int = 50, price: float = 100.0) -> list[Bar]:
    bars = []
    t = start
    for _ in range(n):
        bars.append(_bar(t, price))
        t += timedelta(minutes=2)
    return bars


# ════════════════════════════════════════════════════════════════════
# Basics
# ════════════════════════════════════════════════════════════════════


def test_warmup_emits_nothing():
    s = SessionStrategy()
    state = _state()
    # Just 10 bars — under bias_min_bars
    for b in _build_flat_bars(T_WINDOW_OPEN, n=10):
        sig = s.on_bar(b, state=state, profile=TOPSTEP_50K,
                       current_position=0, current_balance_unrealized=50_000)
        assert sig is None


def test_metadata_defaults():
    s = SessionStrategy()
    assert s.metadata.default_lifecycle == "SHADOW"
    assert s.name == "session"
    assert s.timeframe_minutes == 2


# ════════════════════════════════════════════════════════════════════
# Entry on overnight-up bias inside window
# ════════════════════════════════════════════════════════════════════


def test_entry_on_up_bias_inside_window():
    s = SessionStrategy()
    state = _state()
    # Build 50 ascending bars BEFORE the window opens, then enter the window.
    pre_window_start = T_WINDOW_OPEN - timedelta(minutes=2 * 50)
    pre_bars = _build_uptrend_bars(pre_window_start, n=50)
    sig = None
    for b in pre_bars:
        sig = s.on_bar(b, state=state, profile=TOPSTEP_50K,
                       current_position=0, current_balance_unrealized=50_000)
    # The buffer now has 50 ascending bars; in_window is False during this prelude.
    # First bar inside window — bias should be trend_up, entry should fire.
    in_window_bar = _bar(T_WINDOW_OPEN + timedelta(minutes=2),
                        c=100.0 + 50 * 0.05 + 0.05)
    sig = s.on_bar(in_window_bar, state=state, profile=TOPSTEP_50K,
                   current_position=0, current_balance_unrealized=50_000)
    assert sig is not None
    assert sig.side == "buy"
    assert "trend_up" in sig.reason


def test_no_entry_outside_window():
    # Opt into audit windows for the windowing test — default is no gating.
    s = SessionStrategy(SessionConfig(time_windows=AUDIT_WINDOWS))
    state = _state()
    # Same ascending sequence but the "current" bar is outside any window.
    pre_window_start = T_OUTSIDE - timedelta(minutes=2 * 50)
    pre_bars = _build_uptrend_bars(pre_window_start, n=50)
    for b in pre_bars:
        s.on_bar(b, state=state, profile=TOPSTEP_50K,
                 current_position=0, current_balance_unrealized=50_000)
    sig = s.on_bar(_bar(T_OUTSIDE, c=102.5),
                   state=state, profile=TOPSTEP_50K,
                   current_position=0, current_balance_unrealized=50_000)
    assert sig is None


def test_no_entry_on_chop_bias():
    s = SessionStrategy()
    state = _state()
    # Flat bars → chop bias even inside the window
    pre_window_start = T_WINDOW_OPEN - timedelta(minutes=2 * 50)
    pre_bars = _build_flat_bars(pre_window_start, n=50)
    for b in pre_bars:
        s.on_bar(b, state=state, profile=TOPSTEP_50K,
                 current_position=0, current_balance_unrealized=50_000)
    sig = s.on_bar(_bar(T_WINDOW_OPEN + timedelta(minutes=2), c=100.0),
                   state=state, profile=TOPSTEP_50K,
                   current_position=0, current_balance_unrealized=50_000)
    assert sig is None


def test_long_only_by_default_blocks_down_bias():
    s = SessionStrategy()
    state = _state()
    pre_window_start = T_WINDOW_OPEN - timedelta(minutes=2 * 50)
    for b in _build_downtrend_bars(pre_window_start, n=50):
        s.on_bar(b, state=state, profile=TOPSTEP_50K,
                 current_position=0, current_balance_unrealized=50_000)
    sig = s.on_bar(_bar(T_WINDOW_OPEN + timedelta(minutes=2), c=97.5),
                   state=state, profile=TOPSTEP_50K,
                   current_position=0, current_balance_unrealized=50_000)
    # Down bias, but allow_shorts=False → no entry
    assert sig is None


def test_shorts_enabled_via_config():
    cfg = SessionConfig(allow_shorts=True)
    s = SessionStrategy(config=cfg)
    state = _state()
    pre_window_start = T_WINDOW_OPEN - timedelta(minutes=2 * 50)
    for b in _build_downtrend_bars(pre_window_start, n=50):
        s.on_bar(b, state=state, profile=TOPSTEP_50K,
                 current_position=0, current_balance_unrealized=50_000)
    sig = s.on_bar(_bar(T_WINDOW_OPEN + timedelta(minutes=2), c=97.5),
                   state=state, profile=TOPSTEP_50K,
                   current_position=0, current_balance_unrealized=50_000)
    assert sig is not None
    assert sig.side == "sell"
    assert "trend_down" in sig.reason


# ════════════════════════════════════════════════════════════════════
# Exit at window close
# ════════════════════════════════════════════════════════════════════


def test_window_close_exit_fires():
    # Window-close exit is only meaningful when windows are set.
    s = SessionStrategy(SessionConfig(time_windows=AUDIT_WINDOWS))
    state = _state()
    pre_window_start = T_WINDOW_OPEN - timedelta(minutes=2 * 50)
    for b in _build_uptrend_bars(pre_window_start, n=50):
        s.on_bar(b, state=state, profile=TOPSTEP_50K,
                 current_position=0, current_balance_unrealized=50_000)
    # Enter
    in_bar = _bar(T_WINDOW_OPEN + timedelta(minutes=2), c=102.55)
    s.on_bar(in_bar, state=state, profile=TOPSTEP_50K,
             current_position=0, current_balance_unrealized=50_000)
    # Stay in window — no exit
    sig = s.on_bar(_bar(T_WINDOW_OPEN + timedelta(minutes=4), c=102.6),
                   state=state, profile=TOPSTEP_50K,
                   current_position=1, current_balance_unrealized=50_000)
    assert sig is None
    # Now leave the window — exit fires
    sig = s.on_bar(_bar(T_AFTER_WINDOW, c=102.7),
                   state=state, profile=TOPSTEP_50K,
                   current_position=1, current_balance_unrealized=50_000)
    assert sig is not None
    assert sig.side == "sell"
    assert "window_close" in sig.reason
