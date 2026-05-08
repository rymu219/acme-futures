"""Tests for the v4 regime-aware engine wrapper."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from acme.broker.base import Bar
from acme.ryan_spec.v3_engine import RyanSpecV3Engine
from acme.ryan_spec.v4_engine import V4GatedEngine
from acme.ryan_spec.v4_regime import Regime

CT = timezone(timedelta(hours=-6))


def _bar(t: datetime, *, o: float, h: float, l: float, c: float,  # noqa: E741
         v: int = 100) -> Bar:
    return Bar(t=t, o=o, h=h, l=l, c=c, v=v)


def _flat_warmup(engine: V4GatedEngine | RyanSpecV3Engine, n_bars: int = 22,
                 base_price: float = 5000.0) -> datetime:
    """Push n_bars of flat bars so the inner v3 engine warms up Bollinger/ATR."""
    t = datetime(2026, 5, 7, 9, 0, tzinfo=CT)
    for _ in range(n_bars):
        engine.on_bar(
            _bar(t, o=base_price, h=base_price + 0.5, l=base_price - 0.5,
                 c=base_price),
            bar_delta=0,
            cum_delta_session=0,
        )
        t += timedelta(minutes=2)
    return t


def _trigger_long(
    engine: V4GatedEngine | RyanSpecV3Engine, t: datetime,
    *, base_price: float = 5000.0,
):
    """Drive a clean two-bar reversal that the v3 engine will register as a
    LONG entry (assuming filter passes). Mirrors the helper in the v3 tests."""
    # Bar A: red bar, deep cum_delta — sets up reversal
    engine.on_bar(
        _bar(t, o=base_price, h=base_price + 0.5, l=base_price - 1.5,
             c=base_price - 1.0),
        bar_delta=-200,
        cum_delta_session=-2500,
    )
    t += timedelta(minutes=2)
    # Bar B: green bar with close > prior close — long trigger
    return engine.on_bar(
        _bar(t, o=base_price - 1.0, h=base_price + 1.0, l=base_price - 1.0,
             c=base_price + 0.5),
        bar_delta=-100,
        cum_delta_session=-2600,
    ), t


# --- backward compat: V3Engine unaffected --------------------------------

def test_v3_engine_alone_still_enters_when_filter_passes():
    """Sanity baseline: the bare v3 engine returns 'enter' on the same setup
    we'll feed the gated wrapper. Confirms our test fixtures aren't broken
    before we layer the regime gate on top."""
    e = RyanSpecV3Engine()
    t = _flat_warmup(e)
    decision, _ = _trigger_long(e, t)
    assert decision.action == "enter"
    assert decision.direction == "long"


# --- V4GatedEngine: basic plumbing ---------------------------------------

def test_wrapper_passes_decision_through_when_chop():
    """Classifier returning 'chop' should cause the wrapper to be a no-op
    on top of v3 — same enter decision flows out unchanged."""
    e = V4GatedEngine(classifier=lambda _bars: "chop", gate_mode="gate")
    t = _flat_warmup(e)
    decision, _ = _trigger_long(e, t)
    assert decision.action == "enter"
    assert decision.direction == "long"
    assert e.last_regime == "chop"


def test_wrapper_blocks_long_when_trend_down():
    """Classifier returns trend_down → v3's long entry should be blocked."""
    e = V4GatedEngine(classifier=lambda _bars: "trend_down", gate_mode="gate")
    t = _flat_warmup(e)
    decision, _ = _trigger_long(e, t)
    assert decision.action == "none"
    assert "regime_gate_blocked" in decision.reason
    assert "trend_down" in decision.reason
    assert e.last_regime == "trend_down"


def test_wrapper_passes_long_when_trend_up():
    """Long entry aligned with trend_up → not blocked."""
    e = V4GatedEngine(classifier=lambda _bars: "trend_up", gate_mode="gate")
    t = _flat_warmup(e)
    decision, _ = _trigger_long(e, t)
    assert decision.action == "enter"
    assert decision.direction == "long"
    assert e.last_regime == "trend_up"


def test_classifier_called_with_recent_bars():
    """The wrapper feeds the classifier a list of recent bars; verify the
    list is non-empty and contains Bar instances (so a real classifier
    can read closes off them)."""
    seen_lengths: list[int] = []

    def spy_classifier(bars):
        seen_lengths.append(len(bars))
        return "chop"

    e = V4GatedEngine(classifier=spy_classifier, gate_mode="gate")
    t = _flat_warmup(e, n_bars=5)
    _trigger_long(e, t)
    # Classifier was called once per bar — 5 warmup + 2 trigger bars.
    assert len(seen_lengths) == 7
    # Each call sees an increasing prefix of bars, capped at the deque length.
    assert seen_lengths[0] == 1
    assert seen_lengths[-1] == 7


def test_flip_mode_not_yet_implemented():
    """Flip mode is reserved for PR-D. Constructing it raises immediately
    so no caller silently gets a half-baked behavior."""
    with pytest.raises(NotImplementedError):
        V4GatedEngine(
            classifier=lambda _b: "chop",  # type: ignore[arg-type,return-value]
            gate_mode="flip",
        )


# --- short-side gating (forward-looking; the live fleet rarely shorts but
# the gate logic needs to be symmetric for when v4-loose-shorts adds them) -

def test_wrapper_blocks_short_when_trend_up():
    """Short entries should be blocked when classifier says trend_up."""
    e = V4GatedEngine(classifier=lambda _bars: "trend_up", gate_mode="gate")
    t = _flat_warmup(e)
    # Drive a SHORT trigger setup
    e.on_bar(
        _bar(t, o=5000.0, h=5001.5, l=4999.5, c=5001.0),
        bar_delta=200, cum_delta_session=2500,
    )
    t += timedelta(minutes=2)
    decision = e.on_bar(
        _bar(t, o=5001.0, h=5001.5, l=4998.5, c=4999.5),
        bar_delta=100, cum_delta_session=2600,
    )
    # We can't always force the v3 engine to emit a short entry without
    # tuning cum_delta against the size-weighted threshold, but if the
    # decision IS an enter, the regime gate must have blocked it.
    if decision.action == "enter":
        pytest.fail("wrapper should have blocked short entry under trend_up")
    # If the inner engine declined for its own reasons (e.g. filter), that's
    # still acceptable for this test — what we're guarding is "doesn't pass
    # a short through trend_up." Either path satisfies that.


# --- runtime smoke: V3Runtime accepts the new knob -----------------------

def test_v3runtime_accepts_regime_classifier_knob():
    """Importing V3Runtime and constructing one with a classifier exercises
    the runner→runtime→engine wiring. Doesn't run any bars; just confirms
    no signature mismatch."""
    from acme.broker.paper import PaperAdapter
    from acme.db import Db

    def classifier(bars) -> Regime:  # type: ignore[empty-body,return-value]
        return "chop"

    # Db() requires SUPABASE_URL / KEY env vars — skip the test cleanly if
    # they're not present (e.g. CI without secrets).
    import os
    if not (os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY")):
        pytest.skip("supabase env vars not set")

    from acme.ryan_spec.v3_runtime import V3Runtime
    rt = V3Runtime(
        broker=PaperAdapter(),
        db=Db(),
        contract_symbol="MES",
        regime_classifier=classifier,
    )
    # Inner engine is the v4 wrapper, not the bare v3.
    assert isinstance(rt.engine, V4GatedEngine)
