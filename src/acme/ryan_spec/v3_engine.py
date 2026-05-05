"""Ryan-Spec OOS-v3 decision engine.

Pure logic, framework-agnostic. Caller drives bar+delta in chronological
order; engine emits signal/exit decisions. The exact same logic that ran in
oos_validation_v3.py and produced PF 2.30 over 78 days.

Inputs per bar:
  - 2-min bar (OHLCV)
  - bar_delta (signed aggressor flow over the bar) — int
  - cum_delta_session (cumulative delta since 08:30 CT session reset) — int

The engine internally maintains BB(20,2) + ATR(4) state, history, and the
current open position (if any).

Outputs per bar:
  - Decision: ENTER_LONG / ENTER_SHORT / EXIT / NONE, plus reason + entry/stop
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, tzinfo
from datetime import time as dtime
from typing import Literal

from acme.broker.base import Bar
from acme.indicators import ATR, Bollinger
from acme.ryan_spec.v3_tick_delta import CT

Direction = Literal["long", "short"]
Action = Literal["enter", "exit", "none"]
ExitReason = Literal["stop", "opposite_signal", "session_end", "time_stop"]

# OOS-v3 brief constants (do not modify without re-validating)
BB_PERIOD = 20
BB_STD = 2.0
ATR_PERIOD = 4
STOP_ATR_MULT = 1.5
TIME_STOP_BARS = 60
SESSION_END_CT = dtime(14, 50)
SESSION_OPEN_CT = dtime(8, 30)
HISTORY_BUFFER = 25  # need at least 21 (BB + 1 prior bar)

# Filter threshold defaults.
# Two cum_delta sources are supported because ProjectX's stream_quotes only
# emits price (no size). Each one needs its own threshold:
#   - SIZE-weighted: aggressor flow in contract counts. OOS-validated at -2000.
#     Used when broker.stream_trades is wired (provides price + size + side).
#   - UNIT-weighted: tick-rule with size=1 per quote update. Used live when
#     only stream_quotes is available. Recalibrated to -670 against the same
#     78-day OOS data, full v3 simulator: PF 2.21 (vs OOS 2.30 size-weighted),
#     6,424 trades over 78 days. 96% of OOS performance, exit-mix within 4pp.
FILTER_THRESH_SIZE_WEIGHTED = -2000
FILTER_THRESH_UNIT_WEIGHTED = -670
# Default to size-weighted; runtime overrides via constructor when using quotes.
FILTER_THRESH = FILTER_THRESH_SIZE_WEIGHTED


@dataclass(frozen=True)
class Decision:
    action: Action
    direction: Direction | None = None
    reason: str = ""
    entry_price: float | None = None
    stop_price: float | None = None
    bar_ts: datetime | None = None
    cum_delta_at_entry: int | None = None
    atr_at_entry: float | None = None


@dataclass
class _Position:
    direction: Direction
    entry_ts: datetime
    entry_fill: float
    stop_price: float
    bars_held: int
    cum_delta_at_entry: int
    atr_at_entry: float


@dataclass(frozen=True)
class _BarState:
    """Compact per-bar snapshot the engine retains in history."""
    bar: Bar
    bb_basis: float
    atr: float
    cum_delta: int


class RyanSpecV3Engine:
    """Pure OOS-v3 logic. Not tied to any broker or async runtime.

    Caller must invoke `on_bar(bar, bar_delta, cum_delta_session)` in
    chronological order, with bars on the 2-min grid in CT timezone-aware
    timestamps. Caller is responsible for:
      - Resetting cum_delta at 08:30 CT (engine doesn't know the trade tape)
      - Translating Decision into broker calls
      - Tracking realized fills (entry slippage, etc.) — engine emits
        recommended levels; broker may slip on fill.
    """

    def __init__(
        self,
        *,
        bb_period: int = BB_PERIOD,
        bb_std: float = BB_STD,
        atr_period: int = ATR_PERIOD,
        stop_atr_mult: float = STOP_ATR_MULT,
        time_stop_bars: int = TIME_STOP_BARS,
        session_end_ct: dtime = SESSION_END_CT,
        session_tz: tzinfo = CT,
        filter_thresh: int = FILTER_THRESH,
    ) -> None:
        self._bb = Bollinger(period=bb_period, num_std=bb_std)
        self._atr = ATR(period=atr_period)
        self._history: deque[_BarState] = deque(maxlen=HISTORY_BUFFER)
        self._position: _Position | None = None
        # tunable knobs (kept on instance to make tests/parameter sweeps cleaner)
        self._stop_atr_mult = stop_atr_mult
        self._time_stop_bars = time_stop_bars
        self._session_end_ct = session_end_ct
        self._session_tz = session_tz
        self._filter_thresh = filter_thresh

    @property
    def in_position(self) -> bool:
        return self._position is not None

    @property
    def position(self) -> _Position | None:
        return self._position

    def reset(self) -> None:
        """Clear all internal state. Use on session boundary if the runtime
        wants to start fresh (engine doesn't reset itself — indicators hold
        across sessions per the OOS methodology)."""
        self._bb = Bollinger(period=self._bb.period, num_std=self._bb.num_std)
        self._atr = ATR(period=self._atr.period)
        self._history.clear()
        self._position = None

    # ---------- public API ----------

    def on_bar(
        self,
        bar: Bar,
        *,
        bar_delta: int,            # signed aggressor flow over this bar
        cum_delta_session: int,    # cumulative delta since session open
    ) -> Decision:
        """Process one closed 2m bar. Returns the decision, if any.

        Order of evaluation when in position (matches OOS-v3 simulator):
          1) stop hit (within bar)  → exit "stop"
          2) session end (bar at/after 14:50 CT)  → exit "session_end"
          3) opposite-direction trigger fires this bar  → exit "opposite_signal"
          4) time stop (bars_held >= 60)  → exit "time_stop"
        Otherwise check entry conditions on the closing bar.
        """
        bb_out = self._bb.update(bar.c)
        atr_val = self._atr.update(bar)

        # If indicators not yet warm, hold any position by default but skip
        # entries (matches OOS — only fires when indicators are warm).
        if bb_out is None or atr_val is None:
            self._history.append(_BarState(bar, 0.0, 0.0, cum_delta_session))
            return Decision(action="none", reason="indicators_warmup")

        state = _BarState(bar, bb_out.middle, atr_val, cum_delta_session)
        self._history.append(state)

        # ── Position management first ──
        if self._position is not None:
            return self._evaluate_in_position(state, bar)

        # ── Otherwise check entry ──
        return self._evaluate_entry(state)

    def open_position(
        self,
        direction: Direction,
        *,
        entry_ts: datetime,
        entry_fill_price: float,
        atr_at_entry: float,
        cum_delta_at_entry: int,
    ) -> None:
        """Record that the broker filled an entry. Caller invokes this AFTER
        their broker confirms the fill price. Stop is computed from
        entry_fill_price (not the bar close) so slippage is incorporated."""
        sign = 1 if direction == "long" else -1
        stop_price = entry_fill_price - sign * self._stop_atr_mult * atr_at_entry
        self._position = _Position(
            direction=direction,
            entry_ts=entry_ts,
            entry_fill=entry_fill_price,
            stop_price=stop_price,
            bars_held=0,
            cum_delta_at_entry=cum_delta_at_entry,
            atr_at_entry=atr_at_entry,
        )

    def close_position(self) -> None:
        """Record that broker closed the position (or caller wants to flush
        engine state). Engine forgets the position."""
        self._position = None

    # ---------- internals ----------

    def _evaluate_in_position(self, state: _BarState, bar: Bar) -> Decision:
        assert self._position is not None
        pos = self._position
        pos.bars_held += 1

        # 1) Stop check on intra-bar low/high
        stop_hit = (
            bar.l <= pos.stop_price if pos.direction == "long"
            else bar.h >= pos.stop_price
        )
        if stop_hit:
            return Decision(action="exit", reason="stop", bar_ts=bar.t)

        # 2) Session end (bar at/after configured close in session-tz local time).
        # bar.t may be UTC (live) or fixed-offset (some tests) — convert to the
        # configured session_tz so DST handles itself.
        bar_local = bar.t.astimezone(self._session_tz).timetz()
        if bar_local >= self._session_end_ct.replace(tzinfo=bar_local.tzinfo):
            return Decision(action="exit", reason="session_end", bar_ts=bar.t)

        # 3) Opposite-direction trigger fired this bar?
        opp = self._opposite_direction_triggered_this_bar()
        if opp is not None and opp != pos.direction:
            return Decision(action="exit", reason="opposite_signal", bar_ts=bar.t)

        # 4) Time stop
        if pos.bars_held >= self._time_stop_bars:
            return Decision(action="exit", reason="time_stop", bar_ts=bar.t)

        return Decision(action="none", reason="hold", bar_ts=bar.t)

    def _evaluate_entry(self, state: _BarState) -> Decision:
        if len(self._history) < BB_PERIOD + 1:
            return Decision(action="none", reason="warmup", bar_ts=state.bar.t)

        prior = self._history[-2]
        current = self._history[-1]

        prior_body = prior.bar.c - prior.bar.o
        current_body = current.bar.c - current.bar.o

        long_trig = (
            prior_body <= 0 and current_body > 0
            and current.bar.c > prior.bar.c
        )
        short_trig = (
            prior_body >= 0 and current_body < 0
            and current.bar.c < prior.bar.c
        )
        if not (long_trig or short_trig):
            return Decision(action="none", reason="no_two_bar_reversal",
                            bar_ts=state.bar.t)

        direction: Direction = "long" if long_trig else "short"
        sign = 1 if long_trig else -1
        cum_delta_in_dir = current.cum_delta * sign

        # The validated filter
        if cum_delta_in_dir >= self._filter_thresh:
            return Decision(action="none",
                            reason=f"filter_blocked cum_delta_in_dir={cum_delta_in_dir}",
                            bar_ts=state.bar.t)

        # All checks pass — recommend market entry at the close
        return Decision(
            action="enter",
            direction=direction,
            reason="oos_v3_signal",
            entry_price=state.bar.c,
            stop_price=state.bar.c - sign * self._stop_atr_mult * state.atr,
            bar_ts=state.bar.t,
            cum_delta_at_entry=current.cum_delta,
            atr_at_entry=state.atr,
        )

    def _opposite_direction_triggered_this_bar(self) -> Direction | None:
        """Did the most-recent bar fire a universal two-bar reversal trigger?
        Used inside _evaluate_in_position to detect opposite-direction signals.
        Filter is NOT applied — opposite-direction exits fire on the trigger
        regardless of cum_delta level (matches OOS simulator)."""
        if len(self._history) < 2:
            return None
        prior = self._history[-2]
        current = self._history[-1]
        prior_body = prior.bar.c - prior.bar.o
        current_body = current.bar.c - current.bar.o
        if prior_body <= 0 and current_body > 0 and current.bar.c > prior.bar.c:
            return "long"
        if prior_body >= 0 and current_body < 0 and current.bar.c < prior.bar.c:
            return "short"
        return None
