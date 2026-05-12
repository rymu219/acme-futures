"""IGNITION — first strategy of the new fleet (Part 2 successor to v3).

Composition:
  - **Entry**: GO/NO-GO Box 4-gate filter
    ([`go_no_go.py`](go_no_go.py)) + audit-driven time windows.
  - **Exit policy**: min-2-bar hold on opposite signals (audit §3),
    ATR-multiple stop via bracket.
  - **Direction**: long-only by default. The 7d audit showed the fleet
    was 99% long (43 shorts vs 4,631 longs over 7d, §4); short edge is
    unproven. Configurable.

The strategy emits an opposite-side Signal to *reverse* (conductor's
flat-first FSM handles the close-then-reverse sequence). Bare exits
without re-entry are handled by the bracket's stop — strategy code
doesn't emit "close-only" signals.

Audit-driven default time windows: 03:00–05:00 CT (European session,
fleet PF ~2.0) and 08:00–09:00 CT (US RTH open, fleet PF 1.6–1.7).
The Pine indicator's afternoon window (13:00–14:15 CT) is *excluded*
— audit §2 showed it was the worst 2-hour window in the entire
7d data set.

Lifecycle: SHADOW by default. Promotion through PILOT → LIVE goes
through PerfTracker as usual.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo

from acme.broker.base import Bar, BracketSpec
from acme.contracts import MES, FuturesContract
from acme.indicators import ATR
from acme.risk import DailyState, EvalProfile, can_open_new_position, dollars_to_contracts
from acme.strategies.base import Signal, StrategyMetadata
from acme.strategies.go_no_go import GoNoGoConfig, GoNoGoEngine
from acme.strategies.params import ParameterSpec

CT = ZoneInfo("America/Chicago")


@dataclass(frozen=True)
class IgnitionTimeWindow:
    """Half-open [start, end) clock window in US/Central."""
    start: time
    end: time

    def contains(self, ts: datetime) -> bool:
        ct = ts.astimezone(CT).time()
        # Windows are assumed to be intra-day (start < end). Overnight
        # windows split into two are out of scope; the audit-driven
        # defaults fit comfortably inside a day.
        return self.start <= ct < self.end

    @classmethod
    def from_hours(cls, start_hour: int, end_hour: int) -> "IgnitionTimeWindow":
        return cls(time(start_hour, 0), time(end_hour, 0))


# Audit §2 winners. NOT the Pine indicator's defaults — those put the
# afternoon prime window over 13:00 CT, which the audit shows is the
# single worst hour in the fleet's 7d history.
DEFAULT_TIME_WINDOWS: tuple[IgnitionTimeWindow, ...] = (
    IgnitionTimeWindow.from_hours(3, 5),    # 03:00–05:00 CT — Euro session
    IgnitionTimeWindow.from_hours(8, 9),    # 08:00–09:00 CT — US RTH open
)


@dataclass
class IgnitionConfig:
    """IGNITION configuration. Defaults are audit-driven."""
    # Entry filter
    go_no_go: GoNoGoConfig = field(default_factory=GoNoGoConfig)
    # Time windows the strategy is allowed to fire in (US/Central)
    time_windows: tuple[IgnitionTimeWindow, ...] = DEFAULT_TIME_WINDOWS
    # Exit policy
    min_bars_before_opposite_exit: int = 2   # audit §3 fix
    atr_period: int = 4                       # match PULSE Pine atrLen default
    atr_stop_multiple: float = 1.5            # stop = N * ATR
    atr_target_multiple: float = 2.5          # target = N * ATR (~1.7R reward)
    # Direction policy
    allow_longs: bool = True
    allow_shorts: bool = False                # audit §4: short edge unproven
    # Sizing
    risk_dollars_per_trade: float = 25.0


class IgnitionStrategy:
    """Implements the Strategy Protocol (acme.strategies.base.Strategy)."""

    name = "ignition"
    version = "1"
    metadata = StrategyMetadata(
        tier=2,
        regime_fit={"trending": 1.0, "volatile": 0.7, "ranging": 0.3, "quiet": 0.2},
        time_buckets=["03:00-05:00 CT", "08:00-09:00 CT"],
        default_lifecycle="SHADOW",
        timeframe_minutes=2,
    )

    @classmethod
    def tunable_params(cls) -> list[ParameterSpec]:
        return [
            ParameterSpec("min_bars_before_opposite_exit", int, 2, 1, 5, 1,
                          "Minimum bars held before opposite-signal exit fires"),
            ParameterSpec("atr_stop_multiple", float, 1.5, 0.5, 4.0, 0.1,
                          "ATR multiple for stop loss"),
            ParameterSpec("atr_target_multiple", float, 2.5, 1.0, 6.0, 0.1,
                          "ATR multiple for take profit"),
            ParameterSpec("risk_dollars_per_trade", float, 25.0, 10.0, 200.0, 5.0,
                          "Risk per trade ($)"),
        ]

    def __init__(
        self,
        config: IgnitionConfig | None = None,
        contract: FuturesContract = MES,
    ) -> None:
        self.config = config or IgnitionConfig()
        self.contract = contract
        self.timeframe_minutes = self.metadata.timeframe_minutes
        self._gng = GoNoGoEngine(self.config.go_no_go)
        self._atr = ATR(self.config.atr_period)
        # Tracks bars since current position was entered. Resets to 0
        # when current_position transitions to 0. Used to gate
        # opposite-signal exits per audit §3.
        self._bars_held = 0
        self._prev_position = 0

    def required_history_bars(self) -> int:
        return max(
            self._gng.required_history_bars(),
            self.config.atr_period,
        ) + 2

    # ───────────────────────── helpers ─────────────────────────

    def _in_window(self, ts: datetime) -> bool:
        return any(w.contains(ts) for w in self.config.time_windows)

    def _update_bars_held(self, current_position: int) -> None:
        """Maintains self._bars_held so opposite-signal exits can be
        gated by the audit's min-2-bar rule.

        - position transitions 0 → ±1: just entered → bars_held = 1
        - position stays non-zero: bars_held += 1
        - position transitions ±1 → 0: was just closed → reset
        """
        if current_position == 0:
            self._bars_held = 0
        elif self._prev_position == 0 and current_position != 0:
            # Just entered on this bar (likely from our entry signal last bar)
            self._bars_held = 1
        else:
            self._bars_held += 1
        self._prev_position = current_position

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
        gng = self._gng.update(bar)
        self._atr.update(bar)
        self._update_bars_held(current_position)

        if gng is None or not self._atr.is_warm:
            return None

        in_window = self._in_window(bar.t)
        signal_raw = gng.signal  # +1 / -1 / 0

        # ────── Exit / reversal path: we're already in a position ──────
        if current_position != 0:
            if signal_raw == 0:
                return None
            # Determine if the new signal is opposite to our current side
            holding_long = current_position > 0
            new_long = signal_raw == 1
            if holding_long == new_long:
                return None  # same direction — don't pyramid
            # Opposite signal. Audit §3: suppress before bar 2.
            if self._bars_held < self.config.min_bars_before_opposite_exit:
                return None
            # Emit reversal — conductor's flat-first FSM handles the
            # close-then-evaluate sequence.
            side = "buy" if new_long else "sell"
            return self._build_signal(
                bar=bar, side=side, state=state, profile=profile,
                current_position=current_position,
                current_balance_unrealized=current_balance_unrealized,
                reason="gng_reverse_after_min_hold",
            )

        # ────── Entry path: we're flat ──────
        if signal_raw == 0:
            return None
        if not in_window:
            return None
        if signal_raw == 1 and not self.config.allow_longs:
            return None
        if signal_raw == -1 and not self.config.allow_shorts:
            return None
        side = "buy" if signal_raw == 1 else "sell"
        return self._build_signal(
            bar=bar, side=side, state=state, profile=profile,
            current_position=current_position,
            current_balance_unrealized=current_balance_unrealized,
            reason="gng_entry",
        )

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
