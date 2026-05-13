"""Tests for SESSION's bias-decay guard.

After the 2026-05-12 18:00 CT incident — SESSION entered LONG five
times in 7 minutes while price marched down 8pt, each stopped at -1pt —
SESSION now suppresses entries when the most recent 30 min of bars
moves against the overnight bias by at least 0.5×ATR.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from acme.broker.base import Bar
from acme.risk import TOPSTEP_50K, DailyState
from acme.strategies.session import SessionConfig, SessionStrategy


def _state():
    return DailyState(
        trade_date=datetime.now(UTC).date(),
        starting_balance=50_000.0,
        peak_balance_eod=50_000.0,
        max_loss_limit=48_000.0,
        daily_loss_limit=1_000.0,
    )


def _bar(t, c, *, v=100):
    return Bar(t=t, o=c, h=c + 0.1, l=c - 0.1, c=c, v=v)


def _build_uptrend_then_reversal(start_t, *, n_uptrend=50, n_reversal=20,
                                  up_step=0.5, down_step=0.3):
    """Long-running uptrend (sets bias='trend_up') then a reversal.

    Returns the bars list. The bias classifier reads the whole buffer,
    so the 50-bar uptrend dominates → bias=trend_up. The 20-bar
    reversal accumulates and the decay guard should fire.
    """
    bars = []
    t = start_t
    price = 100.0
    for _ in range(n_uptrend):
        bars.append(_bar(t, price))
        t += timedelta(minutes=2)
        price += up_step
    for _ in range(n_reversal):
        bars.append(_bar(t, price))
        t += timedelta(minutes=2)
        price -= down_step
    return bars


# ════════════ guard fires when recent move opposes bias ═══════════


def test_bias_decay_suppresses_when_recent_move_opposes():
    """50-bar up-ramp → 20-bar reversal. Bias should still be trend_up
    (slow classifier) but the last 15 bars (decay lookback) show a
    clear drop. Guard suppresses."""
    cfg = SessionConfig(
        bias_decay_lookback_bars=15, bias_decay_atr_thresh=0.5,
    )
    s = SessionStrategy(config=cfg)
    state = _state()
    bars = _build_uptrend_then_reversal(
        datetime(2026, 5, 15, 8, 0, tzinfo=UTC),
        n_uptrend=50, n_reversal=20,
    )
    last_sig = None
    for b in bars:
        last_sig = s.on_bar(b, state=state, profile=TOPSTEP_50K,
                            current_position=0, current_balance_unrealized=50_000)
    # Last bar is well into the reversal — guard should be active,
    # no entry signal even though bias is still up
    assert last_sig is None


def test_bias_decay_disabled_via_config():
    """Setting bias_decay_atr_thresh=0 disables the guard — restores
    pre-2026-05-12 behavior."""
    cfg = SessionConfig(
        bias_decay_lookback_bars=15, bias_decay_atr_thresh=0.0,
    )
    s = SessionStrategy(config=cfg)
    state = _state()
    bars = _build_uptrend_then_reversal(
        datetime(2026, 5, 15, 8, 0, tzinfo=UTC),
        n_uptrend=50, n_reversal=20,
    )
    seen_sig = None
    for b in bars:
        sig = s.on_bar(b, state=state, profile=TOPSTEP_50K,
                       current_position=0, current_balance_unrealized=50_000)
        if sig is not None:
            seen_sig = sig
    # With the guard off, a signal should have fired at some point —
    # likely during the reversal when bias was still up
    # (this is the bug we fixed; preserved as an opt-out)
    assert seen_sig is not None


def test_bias_decay_allows_entry_when_recent_move_supports_bias():
    """Up-bias + recent move continues up → no suppression."""
    cfg = SessionConfig(
        bias_decay_lookback_bars=15, bias_decay_atr_thresh=0.5,
    )
    s = SessionStrategy(config=cfg)
    state = _state()
    t = datetime(2026, 5, 15, 8, 0, tzinfo=UTC)
    # Long uptrend continuing through the entry bar — no reversal
    bars = []
    price = 100.0
    for _ in range(70):
        bars.append(_bar(t, price))
        t += timedelta(minutes=2)
        price += 0.5
    seen_sig = None
    for b in bars:
        sig = s.on_bar(b, state=state, profile=TOPSTEP_50K,
                       current_position=0, current_balance_unrealized=50_000)
        if sig is not None and sig.size > 0:
            seen_sig = sig
            break
    # Should have fired at least once during the sustained uptrend
    assert seen_sig is not None
    assert seen_sig.side == "buy"
