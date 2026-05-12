"""SESSION — time-window strategy gated by overnight bias.

Composition:
  - **Entry**: at the first bar inside a configured time window WHERE the
    overnight bias (12h rolling buffer of bars) classifies as a clear
    trend direction (not chop). Enter in that direction.
  - **Exit**: when the bar leaves the time window (window-close exit), or
    when the ATR-multiple stop is hit (handled by the bracket).
  - **No opposite-signal exit.** SESSION is a time-bounded play; the
    overnight bias picks direction and we ride to the window close.

Audit-driven time windows: 03:00–05:00 CT and 08:00–09:00 CT (audit §2 best).
Reuses `acme.ryan_spec.v4_regime.classify_overnight_bias` for the
direction call (12-hour buffer = 360 2-min bars).

Lifecycle: SHADOW by default.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo

from acme.broker.base import Bar, BracketSpec
from acme.contracts import MES, FuturesContract
from acme.indicators import ATR
from acme.risk import DailyState, EvalProfile, can_open_new_position, dollars_to_contracts
from acme.ryan_spec.v4_regime import (
    DEFAULT_OVERNIGHT_MIN_BARS,
    DEFAULT_OVERNIGHT_THRESHOLD_ATR,
    classify_overnight_bias,
)
from acme.strategies.base import Signal, StrategyMetadata
from acme.strategies.ignition import IgnitionTimeWindow
from acme.strategies.params import ParameterSpec

CT = ZoneInfo("America/Chicago")


# Audit §2 winners — kept as a named constant so callers can opt in
# explicitly via `SessionConfig(time_windows=AUDIT_WINDOWS)`. NOT
# applied by default (lifted 2026-05-11). With empty windows SESSION
# fires whenever overnight bias is clear, regardless of clock — and
# the UI bucket selector lets you slice the resulting performance.
AUDIT_WINDOWS: tuple[IgnitionTimeWindow, ...] = (
    IgnitionTimeWindow.from_hours(3, 5),
    IgnitionTimeWindow.from_hours(8, 9),
)
DEFAULT_SESSION_WINDOWS: tuple[IgnitionTimeWindow, ...] = ()


# 12 hours of 2-min bars = 360 bars; spans Globex into RTH. Matches
# v4-overnight-bias variant's regime_history_bars.
DEFAULT_BIAS_BUFFER_BARS = 360


@dataclass
class SessionConfig:
    # Empty tuple means no time gating — fires whenever overnight bias
    # is clear, any hour. Set via AUDIT_WINDOWS to opt back into the
    # audit's best-hour gating.
    time_windows: tuple[IgnitionTimeWindow, ...] = ()
    # Bias-decay guard. The overnight-bias classifier reads a 12h
    # buffer; when intraday direction reverses (e.g. RTH rally fades
    # after 15:00 CT), the classifier doesn't update fast enough and
    # SESSION can repeatedly enter against the new direction.
    # Suppress entries when the recent `bias_decay_lookback_bars`
    # bars show a move opposing the bias by at least
    # `bias_decay_atr_thresh` x ATR.
    # 2026-05-12 incident: 5 consecutive 1-bar stops at 18:00 CT.
    bias_decay_lookback_bars: int = 15        # 30 min of 2-min bars
    bias_decay_atr_thresh: float = 0.5        # 0 disables the guard
    bias_buffer_bars: int = DEFAULT_BIAS_BUFFER_BARS
    bias_threshold_atr: float = DEFAULT_OVERNIGHT_THRESHOLD_ATR
    bias_min_bars: int = DEFAULT_OVERNIGHT_MIN_BARS
    atr_period: int = 4
    atr_stop_multiple: float = 1.5
    atr_target_multiple: float = 2.5
    risk_dollars_per_trade: float = 25.0
    # Direction policy. Default mirrors IGNITION (long-only) for the same
    # reason: audit §4 — short edge is unproven on this signal source.
    allow_longs: bool = True
    allow_shorts: bool = False


class SessionStrategy:
    """Strategy implementing the time-window + overnight-bias pattern."""

    name = "session"
    version = "1"
    metadata = StrategyMetadata(
        tier=2,
        regime_fit={"trending": 1.0, "volatile": 0.5, "ranging": 0.3, "quiet": 0.4},
        time_buckets=["03:00-05:00 CT", "08:00-09:00 CT"],
        default_lifecycle="SHADOW",
        timeframe_minutes=2,
    )

    @classmethod
    def tunable_params(cls) -> list[ParameterSpec]:
        return [
            ParameterSpec("bias_threshold_atr", float, 1.0, 0.5, 3.0, 0.1,
                          "Overnight bias threshold (ATR multiples)"),
            ParameterSpec("atr_stop_multiple", float, 1.5, 0.5, 4.0, 0.1,
                          "ATR multiple for stop loss"),
            ParameterSpec("atr_target_multiple", float, 2.5, 1.0, 6.0, 0.1,
                          "ATR multiple for take profit"),
            ParameterSpec("risk_dollars_per_trade", float, 25.0, 10.0, 200.0, 5.0,
                          "Risk per trade ($)"),
        ]

    def __init__(
        self,
        config: SessionConfig | None = None,
        contract: FuturesContract = MES,
    ) -> None:
        self.config = config or SessionConfig()
        self.contract = contract
        self.timeframe_minutes = self.metadata.timeframe_minutes
        self._atr = ATR(self.config.atr_period)
        self._buffer: deque[Bar] = deque(maxlen=self.config.bias_buffer_bars)
        self._prev_in_window = False

    def required_history_bars(self) -> int:
        return max(self.config.bias_buffer_bars, self.config.bias_min_bars + 5)

    # ───────────────────────── helpers ─────────────────────────

    def _in_window(self, ts: datetime) -> bool:
        if not self.config.time_windows:
            return True       # no gating — fires any hour
        return any(w.contains(ts) for w in self.config.time_windows)

    # ───────────────────────── on_bar ──────────────────────────

    def on_bar(
        self,
        bar: Bar,
        *,
        state: DailyState,
        profile: EvalProfile,
        current_position: int,
        current_balance_unrealized: float,
    ) -> Signal | None:
        self._buffer.append(bar)
        self._atr.update(bar)

        in_window = self._in_window(bar.t)
        was_in_window = self._prev_in_window
        self._prev_in_window = in_window

        if not self._atr.is_warm:
            return None

        # ────── Exit path: in position, just left the window ──────
        if current_position != 0:
            if was_in_window and not in_window:
                # Window-close exit. Emit opposite-direction Signal so the
                # conductor's flat-first FSM closes. Next bar we're flat and
                # out of window → no re-entry.
                side = "sell" if current_position > 0 else "buy"
                return self._build_signal(
                    bar=bar, side=side, state=state, profile=profile,
                    current_position=current_position,
                    current_balance_unrealized=current_balance_unrealized,
                    reason="session_window_close",
                )
            return None

        # ────── Entry path: we're flat ──────
        if not in_window:
            return None
        if len(self._buffer) < self.config.bias_min_bars:
            return None

        bias = classify_overnight_bias(
            list(self._buffer),
            threshold_atr=self.config.bias_threshold_atr,
            min_bars=self.config.bias_min_bars,
        )
        if bias == "chop":
            return None

        # Bias-decay guard: if the last N bars moved AGAINST the bias by
        # more than `bias_decay_atr_thresh` x ATR, suppress. The slow
        # 12h-window classifier hasn't caught up to an intraday flip yet.
        if self._recent_move_opposes_bias(bias):
            return None

        if bias == "trend_up" and self.config.allow_longs:
            side = "buy"
        elif bias == "trend_down" and self.config.allow_shorts:
            side = "sell"
        else:
            return None

        return self._build_signal(
            bar=bar, side=side, state=state, profile=profile,
            current_position=current_position,
            current_balance_unrealized=current_balance_unrealized,
            reason=f"session_entry_{bias}",
        )

    def _recent_move_opposes_bias(self, bias: str) -> bool:
        """True iff the last `bias_decay_lookback_bars` bars moved against
        `bias` by at least `bias_decay_atr_thresh` × ATR."""
        n = self.config.bias_decay_lookback_bars
        thresh = self.config.bias_decay_atr_thresh
        if n <= 0 or thresh <= 0:
            return False
        if len(self._buffer) <= n:
            return False
        atr_val = self._atr.value
        if atr_val is None or atr_val <= 0:
            return False
        recent_move = self._buffer[-1].c - self._buffer[-1 - n].c
        # Up-bias suppressed if recent move <= -thresh * ATR
        # Down-bias suppressed if recent move >= +thresh * ATR
        opposing_drop = atr_val * thresh
        if bias == "trend_up" and recent_move <= -opposing_drop:
            return True
        if bias == "trend_down" and recent_move >= opposing_drop:
            return True
        return False

    # ───────────────────────── signal construction ──────────────

    def _build_signal(
        self, *, bar: Bar, side: str, state: DailyState, profile: EvalProfile,
        current_position: int, current_balance_unrealized: float,
        reason: str,
    ) -> Signal | None:
        atr_val = self._atr.value
        if atr_val is None or atr_val <= 0:
            return None

        stop_distance = atr_val * self.config.atr_stop_multiple
        target_distance = atr_val * self.config.atr_target_multiple
        if stop_distance <= 0:
            return None

        stop_ticks = max(1, int(round(stop_distance / self.contract.tick_size)))
        target_ticks = max(1, int(round(target_distance / self.contract.tick_size)))

        round_turn_fee = profile.round_turn_fees.get(self.contract.symbol, 0.0)
        size = dollars_to_contracts(
            self.config.risk_dollars_per_trade,
            stop_distance,
            self.contract.point_value,
            round_turn_fee=round_turn_fee,
        )
        if size <= 0:
            return None

        allowed, block_reason = can_open_new_position(
            profile, state, current_balance_unrealized,
            self.contract.symbol, size, current_position,
        )
        if not allowed:
            return Signal(side=side, size=0, reason=f"blocked: {block_reason}")  # type: ignore[arg-type]

        return Signal(
            side=side,  # type: ignore[arg-type]
            size=size,
            bracket=BracketSpec(
                stop_loss_offset_ticks=stop_ticks,
                take_profit_offset_ticks=target_ticks,
            ),
            reason=reason,
        )
