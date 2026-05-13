"""Tests for the IGNITION strategy.

Coverage:
  - warm-up emits nothing
  - inside a time window + healthy GO/NO-GO setup → emits a buy
  - outside time windows → no entry even with healthy setup
  - long-only by default (short signal returns no entry)
  - min-2-bar hold: opposite signal at bar 1 is suppressed
  - min-2-bar hold: opposite signal at bar 2+ fires reversal
  - bracket has both stop and target offsets set
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from acme.broker.base import Bar
from acme.risk import TOPSTEP_50K, DailyState
from acme.strategies.go_no_go import GoNoGoConfig
from acme.strategies.ignition import AUDIT_WINDOWS, IgnitionConfig, IgnitionStrategy


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


# 03:00 CT = 08:00 UTC (CT is UTC-5 in summer; we use a date in May which is
# UT-5 so the math is simple).
T_IN_WINDOW = datetime(2026, 5, 15, 8, 0, tzinfo=UTC)     # 03:00 CT — inside window
T_OUT_WINDOW = datetime(2026, 5, 15, 18, 0, tzinfo=UTC)   # 13:00 CT — explicitly bad


def _warmup_then_setup(start_t: datetime, n_warm: int = 50,
                       n_signal_bars: int = 6, step: float = 0.5,
                       up: bool = True) -> list[Bar]:
    """Flat warm-up bars then a clean ramp that satisfies GO/NO-GO."""
    bars: list[Bar] = []
    t = start_t
    for _ in range(n_warm):
        bars.append(_bar(t, 100.0, v=100))
        t += timedelta(minutes=2)
    sign = 1 if up else -1
    # Ramp with a volume dip + spike on the last bar to satisfy vr_rising
    for i in range(n_signal_bars - 2):
        c = 100.0 + sign * (i + 1) * step
        bars.append(_bar(t, c, v=100))
        t += timedelta(minutes=2)
    # Dip
    c = 100.0 + sign * (n_signal_bars - 1) * step
    bars.append(_bar(t, c, v=70))
    t += timedelta(minutes=2)
    # Spike (closes the entry bar)
    c = 100.0 + sign * n_signal_bars * step
    bars.append(_bar(t, c, v=300))
    return bars


# ════════════════════════════════════════════════════════════════════
# Basic guarantees
# ════════════════════════════════════════════════════════════════════


def test_warmup_emits_nothing():
    s = IgnitionStrategy()
    state = _state()
    # 30 flat bars (less than required_history_bars)
    bars = [_bar(T_IN_WINDOW + timedelta(minutes=2 * i), 100.0) for i in range(30)]
    for b in bars:
        sig = s.on_bar(b, state=state, profile=TOPSTEP_50K,
                       current_position=0, current_balance_unrealized=50_000)
        assert sig is None


def test_metadata_default_lifecycle_shadow():
    s = IgnitionStrategy()
    assert s.metadata.default_lifecycle == "SHADOW"
    assert s.timeframe_minutes == 2
    assert s.name == "ignition"


# ════════════════════════════════════════════════════════════════════
# Entry: inside window + healthy setup
# ════════════════════════════════════════════════════════════════════


def test_entry_fires_inside_window_with_healthy_uptrend():
    s = IgnitionStrategy()
    state = _state()
    bars = _warmup_then_setup(T_IN_WINDOW, up=True)
    sig = None
    for b in bars:
        sig = s.on_bar(b, state=state, profile=TOPSTEP_50K,
                       current_position=0, current_balance_unrealized=50_000)
    assert sig is not None
    assert sig.side == "buy"
    assert sig.size > 0
    assert sig.bracket is not None
    assert sig.bracket.stop_loss_offset_ticks > 0
    assert sig.bracket.take_profit_offset_ticks > 0
    assert "gng_entry" in sig.reason


def test_no_entry_outside_time_window():
    # Opt into the audit windows explicitly — default is no gating now.
    cfg = IgnitionConfig(time_windows=AUDIT_WINDOWS)
    s = IgnitionStrategy(config=cfg)
    state = _state()
    # Healthy ramp at 13:00 CT — outside every audit window.
    bars = _warmup_then_setup(T_OUT_WINDOW, up=True)
    last_sig = None
    for b in bars:
        last_sig = s.on_bar(b, state=state, profile=TOPSTEP_50K,
                            current_position=0, current_balance_unrealized=50_000)
    # Either None or size=0 (risk-blocked) — but never a tradeable buy
    assert last_sig is None or last_sig.size == 0


def test_default_config_fires_outside_audit_windows():
    """With time gating lifted by default, a healthy setup outside the
    audit windows should still produce an entry."""
    s = IgnitionStrategy()   # default config: time_windows=()
    state = _state()
    bars = _warmup_then_setup(T_OUT_WINDOW, up=True)
    last_sig = None
    for b in bars:
        last_sig = s.on_bar(b, state=state, profile=TOPSTEP_50K,
                            current_position=0, current_balance_unrealized=50_000)
    assert last_sig is not None
    assert last_sig.side == "buy"
    assert last_sig.size > 0


def test_long_only_by_default():
    s = IgnitionStrategy()
    state = _state()
    bars = _warmup_then_setup(T_IN_WINDOW, up=False)   # downtrend
    last_sig = None
    for b in bars:
        last_sig = s.on_bar(b, state=state, profile=TOPSTEP_50K,
                            current_position=0, current_balance_unrealized=50_000)
    # allow_shorts defaults False → no entry on a short signal
    assert last_sig is None or last_sig.size == 0


def test_shorts_enabled_via_config():
    cfg = IgnitionConfig(allow_shorts=True)
    s = IgnitionStrategy(config=cfg)
    state = _state()
    bars = _warmup_then_setup(T_IN_WINDOW, up=False)
    sig = None
    for b in bars:
        sig = s.on_bar(b, state=state, profile=TOPSTEP_50K,
                       current_position=0, current_balance_unrealized=50_000)
    assert sig is not None
    assert sig.side == "sell"


# ════════════════════════════════════════════════════════════════════
# Min-2-bar hold rule (audit §3)
# ════════════════════════════════════════════════════════════════════


def test_opposite_signal_suppressed_at_bar_1():
    """The strategy is long for one bar; an opposite signal arrives. The
    min-2-bar rule means the strategy emits nothing yet."""
    # Lower sep_thr so a modest reversal still triggers GO/NO-GO short
    # signal — we're isolating the min-2-bar logic, not exercising the
    # default Pine separation threshold.
    gng = GoNoGoConfig(sep_thr=0.10, require_vr_rising=False)
    cfg = IgnitionConfig(min_bars_before_opposite_exit=2, allow_shorts=True,
                         go_no_go=gng, risk_dollars_per_trade=500.0)
    s = IgnitionStrategy(config=cfg)
    state = _state()
    bars = _warmup_then_setup(T_IN_WINDOW, up=True)
    for b in bars:
        s.on_bar(b, state=state, profile=TOPSTEP_50K,
                 current_position=0, current_balance_unrealized=50_000)
    t = bars[-1].t + timedelta(minutes=2)
    # Down bar that produces a GO/NO-GO SHORT signal
    sig = s.on_bar(_bar(t, c=98.0, v=500, h_pad=0.2, l_pad=0.2),
                   state=state, profile=TOPSTEP_50K,
                   current_position=1, current_balance_unrealized=50_000)
    # bars_held = 1 → suppressed regardless of GO/NO-GO state
    assert sig is None


def test_opposite_signal_fires_at_bar_2():
    """After 2 bars held, an opposite signal does fire a reversal."""
    # require_vr_rising=False keeps the volume gate from blocking the
    # synthetic test sequence. Inflated risk_dollars covers the elevated
    # ATR caused by the big synthetic price gaps.
    gng = GoNoGoConfig(sep_thr=0.10, require_vr_rising=False)
    cfg = IgnitionConfig(
        min_bars_before_opposite_exit=2, allow_shorts=True,
        go_no_go=gng, risk_dollars_per_trade=500.0,
    )
    s = IgnitionStrategy(config=cfg)
    state = _state()
    bars = _warmup_then_setup(T_IN_WINDOW, up=True)
    for b in bars:
        s.on_bar(b, state=state, profile=TOPSTEP_50K,
                 current_position=0, current_balance_unrealized=50_000)
    # Bar 1 of position: opposite GO/NO-GO signal but suppressed by min-2-bar
    t = bars[-1].t + timedelta(minutes=2)
    s.on_bar(_bar(t, c=98.0, v=500, h_pad=0.2, l_pad=0.2),
             state=state, profile=TOPSTEP_50K,
             current_position=1, current_balance_unrealized=50_000)
    # Bar 2 of position: opposite signal now allowed through
    t += timedelta(minutes=2)
    sig = s.on_bar(_bar(t, c=96.0, v=500, h_pad=0.2, l_pad=0.2),
                   state=state, profile=TOPSTEP_50K,
                   current_position=1, current_balance_unrealized=50_000)
    assert sig is not None
    assert sig.side == "sell"
    assert "reverse" in sig.reason


def test_same_direction_does_not_pyramid():
    """If we're already long and a long signal fires, do not stack."""
    cfg = IgnitionConfig()
    s = IgnitionStrategy(config=cfg)
    state = _state()
    bars = _warmup_then_setup(T_IN_WINDOW, up=True)
    for b in bars:
        s.on_bar(b, state=state, profile=TOPSTEP_50K,
                 current_position=0, current_balance_unrealized=50_000)
    # Next bar: still a long-favorable bar
    t = bars[-1].t + timedelta(minutes=2)
    sig = s.on_bar(_bar(t, c=105.0, v=400),
                   state=state, profile=TOPSTEP_50K,
                   current_position=1, current_balance_unrealized=50_000)
    assert sig is None
