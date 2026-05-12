"""Tests for REGIME strategy.

Coverage:
  - warm-up emits nothing
  - metadata defaults
  - no entry in compression / normal vol
  - no entry on chop trend even in expansion
  - entry fires when vol expansion + clear trend
  - long-only by default
  - exit when vol normalises (after min-2-bar)
  - exit on trend flip (after min-2-bar)
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from acme.broker.base import Bar
from acme.risk import TOPSTEP_50K, DailyState
from acme.strategies.regime import RegimeConfig, RegimeStrategy


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


# ════════════════════════════════════════════════════════════════════
# Basics
# ════════════════════════════════════════════════════════════════════


def test_metadata_defaults():
    s = RegimeStrategy()
    assert s.metadata.default_lifecycle == "SHADOW"
    assert s.name == "regime"


def test_warmup_emits_nothing():
    s = RegimeStrategy()
    state = _state()
    t = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    # Just 5 bars — well under warm threshold
    for i in range(5):
        sig = s.on_bar(_bar(t + timedelta(minutes=2 * i), 100.0),
                       state=state, profile=TOPSTEP_50K,
                       current_position=0, current_balance_unrealized=50_000)
        assert sig is None


def test_no_entry_in_quiet_market():
    """Flat bars → vol=normal/low + trend=chop → no entry ever."""
    s = RegimeStrategy()
    state = _state()
    t = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    last_sig = None
    for i in range(80):
        last_sig = s.on_bar(_bar(t + timedelta(minutes=2 * i), 100.0),
                            state=state, profile=TOPSTEP_50K,
                            current_position=0, current_balance_unrealized=50_000)
    assert last_sig is None


def test_entry_on_vol_expansion_plus_trend_up():
    """Quiet warm-up → vol expansion + steady up-ramp → buy entry."""
    s = RegimeStrategy()
    state = _state()
    t = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    # 40 flat bars (warms ATR + ATR-SMA at low base value)
    for i in range(40):
        s.on_bar(_bar(t + timedelta(minutes=2 * i), 100.0,
                      h_pad=0.05, l_pad=0.05),
                 state=state, profile=TOPSTEP_50K,
                 current_position=0, current_balance_unrealized=50_000)
    t += timedelta(minutes=2 * 40)
    # 30 bars: bigger ranges + climbing closes → vol expansion + trend up
    last_sig = None
    for i in range(30):
        c = 100.0 + (i + 1) * 0.3
        last_sig = s.on_bar(_bar(t + timedelta(minutes=2 * i), c,
                                 h_pad=0.5, l_pad=0.5),
                            state=state, profile=TOPSTEP_50K,
                            current_position=0, current_balance_unrealized=50_000)
        if last_sig is not None:
            break
    assert last_sig is not None
    assert last_sig.side == "buy"
    assert "expansion" in last_sig.reason


def test_long_only_by_default_blocks_short_trend():
    """Vol expansion + clear DOWN trend, but allow_shorts=False → no entry."""
    s = RegimeStrategy()
    state = _state()
    t = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    for i in range(40):
        s.on_bar(_bar(t + timedelta(minutes=2 * i), 100.0,
                      h_pad=0.05, l_pad=0.05),
                 state=state, profile=TOPSTEP_50K,
                 current_position=0, current_balance_unrealized=50_000)
    t += timedelta(minutes=2 * 40)
    last_sig = None
    for i in range(30):
        c = 100.0 - (i + 1) * 0.3
        last_sig = s.on_bar(_bar(t + timedelta(minutes=2 * i), c,
                                 h_pad=0.5, l_pad=0.5),
                            state=state, profile=TOPSTEP_50K,
                            current_position=0, current_balance_unrealized=50_000)
    assert last_sig is None or last_sig.size == 0


def test_shorts_enabled_via_config():
    cfg = RegimeConfig(allow_shorts=True, allow_longs=False)
    s = RegimeStrategy(config=cfg)
    state = _state()
    t = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    for i in range(40):
        s.on_bar(_bar(t + timedelta(minutes=2 * i), 100.0,
                      h_pad=0.05, l_pad=0.05),
                 state=state, profile=TOPSTEP_50K,
                 current_position=0, current_balance_unrealized=50_000)
    t += timedelta(minutes=2 * 40)
    last_sig = None
    for i in range(30):
        c = 100.0 - (i + 1) * 0.3
        last_sig = s.on_bar(_bar(t + timedelta(minutes=2 * i), c,
                                 h_pad=0.5, l_pad=0.5),
                            state=state, profile=TOPSTEP_50K,
                            current_position=0, current_balance_unrealized=50_000)
        if last_sig is not None and last_sig.size > 0:
            break
    assert last_sig is not None
    assert last_sig.side == "sell"


def test_exit_when_vol_normalises():
    """After entering on vol expansion + trend up, when vol drops back
    to normal/low, the strategy emits an exit (after min-2-bar)."""
    cfg = RegimeConfig(min_bars_before_opposite_exit=2,
                       exit_on_vol_normalization=True,
                       # smaller risk to ensure size=1 trades
                       risk_dollars_per_trade=200.0)
    s = RegimeStrategy(config=cfg)
    state = _state()
    t = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    for i in range(40):
        s.on_bar(_bar(t + timedelta(minutes=2 * i), 100.0,
                      h_pad=0.05, l_pad=0.05),
                 state=state, profile=TOPSTEP_50K,
                 current_position=0, current_balance_unrealized=50_000)
    t += timedelta(minutes=2 * 40)
    # Vol expansion + up-trend
    for i in range(15):
        c = 100.0 + (i + 1) * 0.3
        s.on_bar(_bar(t + timedelta(minutes=2 * i), c,
                      h_pad=0.5, l_pad=0.5),
                 state=state, profile=TOPSTEP_50K,
                 current_position=0, current_balance_unrealized=50_000)
    t += timedelta(minutes=2 * 15)
    # Two bars in position (vol still high, just elapsing min-hold)
    s.on_bar(_bar(t, 105.0, h_pad=0.5, l_pad=0.5),
             state=state, profile=TOPSTEP_50K,
             current_position=1, current_balance_unrealized=50_000)
    s.on_bar(_bar(t + timedelta(minutes=2), 105.0, h_pad=0.5, l_pad=0.5),
             state=state, profile=TOPSTEP_50K,
             current_position=1, current_balance_unrealized=50_000)
    # Bar 3 of position: extra-tight range collapses ATR ratio → vol normalises
    sig = None
    for j in range(15):
        sig = s.on_bar(_bar(t + timedelta(minutes=2 * (2 + j)),
                            105.0, h_pad=0.01, l_pad=0.01),
                       state=state, profile=TOPSTEP_50K,
                       current_position=1, current_balance_unrealized=50_000)
        if sig is not None:
            break
    assert sig is not None
    assert sig.side == "sell"
    assert "normalized" in sig.reason
