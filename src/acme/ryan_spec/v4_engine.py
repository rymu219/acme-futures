"""v4 regime-aware engine wrapper.

Wraps `RyanSpecV3Engine` with a regime classifier and an action mode. On
each bar we run the v3 entry logic; if it produces an `enter` decision, we
then apply the regime gate before returning the decision to the runtime.

Action modes:
    "gate" — skip entries that go *against* the regime. Long signal in
             "trend_down" → return `none` with reason `regime_gate_*`.
             Short signal in "trend_up" → likewise.
             Chop regime → no override, take v3 as-is.
             Bounded blast radius: only ever reduces trade count.

    "flip" — *invert* counter-regime entries instead of dropping them. Long
             signal in "trend_down" becomes a short with the same entry.
             Reserved for PR-D once gate-mode has shown the classifier is
             reliable; not yet implemented here.

This wrapper holds its own bar history (the v3 engine's `_history` deque is
sized for BB+1, too small for an EMA(20)+lookback regime calc). Same
`on_bar` signature as the v3 engine so the runtime can use it as a
drop-in replacement.

Per docs/2026-05-07-trading-day-analysis.md (PR plan A→D), this is PR-B:
classifier + wrapper, plus the first gate-mode variant `v4-trend-gate`.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
from typing import Any, Literal

from acme.broker.base import Bar

from .v3_engine import Decision, RyanSpecV3Engine
from .v4_regime import Regime

GateMode = Literal["gate", "flip"]

# How many bars the wrapper retains for regime classification. 60 covers
# the EMA(20) + 10-bar lookback that classify_trend_ema needs (with margin)
# and stays small enough to keep memory trivial.
DEFAULT_REGIME_HISTORY = 60


class V4GatedEngine:
    """v3 engine + regime gate. Same on_bar interface as `RyanSpecV3Engine`.

    Backward compat: not used unless the runtime is constructed with a
    classifier. v3 variants stay on the bare RyanSpecV3Engine and behave
    identically to before this PR.
    """

    def __init__(
        self,
        *,
        classifier: Callable[[Sequence[Bar]], Regime],
        gate_mode: GateMode = "gate",
        regime_history_bars: int = DEFAULT_REGIME_HISTORY,
        **engine_kwargs: Any,
    ) -> None:
        if gate_mode == "flip":
            raise NotImplementedError(
                "flip mode is reserved for PR-D — not yet implemented"
            )
        self._engine = RyanSpecV3Engine(**engine_kwargs)
        self._classifier = classifier
        self._gate_mode: GateMode = gate_mode
        self._bars: deque[Bar] = deque(maxlen=regime_history_bars)
        # Last computed regime — exposed for logging / observability.
        self._last_regime: Regime = "chop"

    # -- pass-through accessors so the runtime can treat us as the engine --

    @property
    def position(self) -> Any:
        return self._engine.position

    @property
    def in_position(self) -> bool:
        return self._engine.in_position

    @property
    def last_regime(self) -> Regime:
        return self._last_regime

    def open_position(self, *args: Any, **kwargs: Any) -> Any:
        return self._engine.open_position(*args, **kwargs)

    def close_position(self, *args: Any, **kwargs: Any) -> Any:
        return self._engine.close_position(*args, **kwargs)

    # -- bar processing with regime gate ----------------------------------

    def on_bar(
        self,
        bar: Bar,
        *,
        bar_delta: int,
        cum_delta_session: int,
    ) -> Decision:
        """Compute v3 decision then gate by regime. Updates regime *after*
        the v3 call so the inner engine still gets the most recent bar
        as part of its history before classification reads it."""
        self._bars.append(bar)
        decision = self._engine.on_bar(
            bar,
            bar_delta=bar_delta,
            cum_delta_session=cum_delta_session,
        )
        # Recompute regime each bar — cheap (O(window)) and the wrapper has
        # no event-loop concerns, just synchronous bar processing.
        self._last_regime = self._classifier(list(self._bars))
        if decision.action != "enter":
            return decision
        return self._apply_gate(decision)

    # -- gate logic -------------------------------------------------------

    def _apply_gate(self, decision: Decision) -> Decision:
        regime = self._last_regime
        # Chop is "no override" — strategy runs v3-as-is. This is the safe
        # default during early-session warmup when the classifier doesn't
        # have enough bars yet.
        if regime == "chop":
            return decision
        # Counter-regime: regime says trend_up but decision is short, or
        # regime says trend_down but decision is long. Block.
        counter = (
            (regime == "trend_up" and decision.direction == "short")
            or (regime == "trend_down" and decision.direction == "long")
        )
        if not counter:
            return decision
        # Replace the enter decision with a `none` carrying a self-describing
        # reason — visible in the trade log / dashboard.
        return Decision(
            action="none",
            reason=f"regime_gate_blocked_{regime}_{decision.direction}",
            bar_ts=decision.bar_ts,
        )
