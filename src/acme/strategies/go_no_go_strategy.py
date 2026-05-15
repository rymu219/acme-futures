"""GO/NO-GO standalone strategy — analysis-only copy.

This is a COPY of the GO/NO-GO entry logic from `ignition.py`, stripped
to the minimum viable strategy: when all GO/NO-GO gates pass, take the
trade in the direction the gates indicate. No min-bars-held opposite-
exit policy, no audit-driven time windows by default.

Purpose: isolate GO/NO-GO as its own strategy class so we can backtest
its raw edge — answering "does the GO/NO-GO filter, used as a signal,
beat the SESSION baseline?"

Carried over from IGNITION (defaults match `IgnitionConfig`):
  - ATR-multiple stop:    1.5 × ATR
  - ATR-multiple target:  2.5 × ATR (1.67R reward)
  - ATR period:           4
  - Risk per trade:       $25

Differences from IGNITION (intentional, for the standalone test):
  - **Both directions allowed by default** (long + short). IGNITION's
    long-only default comes from v3 audit §4 — a finding about a
    *different* cluster of strategies, not about GO/NO-GO itself.
    Restricting direction here would carry over that prior decision.
    Phase 2 will show whether shorts are worth keeping.
  - **No min-bars-held opposite-exit.** IGNITION's `min_bars_before_
    opposite_exit=2` was audit §3 noise control. Without it, opposite
    gates reverse immediately. Tests the raw signal, not a smoothed
    version of it.
  - **Default time gating empty.** Same as IGNITION's default (fires any
    hour) — the UI / backtest does time-bucket slicing. Phase 2 RUN B
    will set `time_windows = ((08, 12),)` explicitly.

This module is NEVER imported by `fleet_runner.py` or registered with
the Supabase `strategies` table by `register_new_fleet.py`. It exists
solely as a backtest target.
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
class GoNoGoTimeWindow:
    """Half-open [start, end) clock window in US/Central. Same shape as
    IgnitionTimeWindow — copied locally to avoid coupling the standalone
    strategy to ignition.py."""
    start: time
    end: time

    def contains(self, ts: datetime) -> bool:
        ct = ts.astimezone(CT).time()
        return self.start <= ct < self.end

    @classmethod
    def from_hours(cls, start_hour: int, end_hour: int) -> GoNoGoTimeWindow:
        return cls(time(start_hour, 0), time(end_hour, 0))


@dataclass
class GoNoGoStrategyConfig:
    """Standalone GO/NO-GO config.

    Defaults intentionally match `IgnitionConfig` for the bracket /
    sizing / direction-policy fields so a comparison against IGNITION's
    historical numbers is apples-to-apples on everything except the
    layered-on bits (min-bars exit, audit-driven long-only default)."""

    # GO/NO-GO feature engine config (uses its own defaults — Pine match)
    go_no_go: GoNoGoConfig = field(default_factory=GoNoGoConfig)

    # Time windows the strategy is allowed to fire in (US/Central).
    # Empty tuple () means no time gating — the strategy fires any hour.
    # Phase 2 RUN B will pass ((08, 12),).
    time_windows: tuple[GoNoGoTimeWindow, ...] = ()

    # ATR-multiple bracket — copied from IGNITION defaults.
    atr_period: int = 4
    atr_stop_multiple: float = 1.5
    atr_target_multiple: float = 2.5

    # Direction policy. Default = bidirectional (see module docstring).
    allow_longs: bool = True
    allow_shorts: bool = True

    # Sizing — copied from IGNITION default.
    risk_dollars_per_trade: float = 25.0


class GoNoGoStrategy:
    """GO/NO-GO as a standalone strategy. Implements the Strategy Protocol."""

    name = "go_no_go"
    version = "1"
    metadata = StrategyMetadata(
        tier=2,
        regime_fit={"trending": 1.0, "volatile": 0.7, "ranging": 0.3, "quiet": 0.2},
        time_buckets=[],          # no preferred buckets — fires any hour by default
        default_lifecycle="SHADOW",
        timeframe_minutes=2,
    )

    @classmethod
    def tunable_params(cls) -> list[ParameterSpec]:
        return [
            ParameterSpec("atr_stop_multiple", float, 1.5, 0.5, 4.0, 0.1,
                          "ATR multiple for stop loss"),
            ParameterSpec("atr_target_multiple", float, 2.5, 1.0, 6.0, 0.1,
                          "ATR multiple for take profit"),
            ParameterSpec("risk_dollars_per_trade", float, 25.0, 10.0, 200.0, 5.0,
                          "Risk per trade ($)"),
        ]

    def __init__(
        self,
        config: GoNoGoStrategyConfig | None = None,
        contract: FuturesContract = MES,
    ) -> None:
        self.config = config or GoNoGoStrategyConfig()
        self.contract = contract
        self.timeframe_minutes = self.metadata.timeframe_minutes
        self._gng = GoNoGoEngine(self.config.go_no_go)
        self._atr = ATR(self.config.atr_period)

    def required_history_bars(self) -> int:
        return max(
            self._gng.required_history_bars(),
            self.config.atr_period,
        ) + 2

    # ───────────────────────── helpers ─────────────────────────

    def _in_window(self, ts: datetime) -> bool:
        if not self.config.time_windows:
            return True
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
        gng = self._gng.update(bar)
        self._atr.update(bar)

        if gng is None or not self._atr.is_warm:
            return None

        signal_raw = gng.signal           # +1 / -1 / 0

        # ────── In-position: reverse on opposite signal (no min-bars gate) ──────
        if current_position != 0:
            if signal_raw == 0:
                return None
            holding_long = current_position > 0
            new_long = signal_raw == 1
            if holding_long == new_long:
                return None              # same direction — don't pyramid
            # Opposite signal — emit reversal. Conductor's flat-first FSM
            # handles the close-then-reopen sequence; the bracket on the
            # closing trade is what gives us the realised P&L.
            side = "buy" if new_long else "sell"
            return self._build_signal(
                bar=bar, side=side, state=state, profile=profile,
                current_position=current_position,
                current_balance_unrealized=current_balance_unrealized,
                reason="gng_reverse",
            )

        # ────── Flat: enter on a fresh signal ──────
        if signal_raw == 0:
            return None
        if not self._in_window(bar.t):
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
            side=side,                # type: ignore[arg-type]
            size=size,
            bracket=BracketSpec(
                stop_loss_offset_ticks=stop_ticks,
                take_profit_offset_ticks=target_ticks,
            ),
            reason=reason,
        )
