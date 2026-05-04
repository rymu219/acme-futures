"""Turtles System 2 — slower 55-bar Donchian breakout variant.

Spec:
  1. Track the highest high and lowest low over the last 55 bars (System 2's
     longer-term lookback vs. System 1's 20).
  2. Long entry: bar closes above prior 55-bar high. Short entry: bar closes
     below prior 55-bar low. Same first-of-day filter as System 1.
  3. Stop: 2.5x ATR(20). Target: 4.0x ATR(20).
     - The classic Turtles exit is "price hits opposite 20-bar extreme" — we
       can't replicate that with a fixed bracket (entry stop/target are set
       once and don't move). The wider ATR target is the closest single-
       bracket approximation, sized to give the slower-timeframe trend room.
  4. **First breakout of the trading session only** — same death-by-cuts gate
     as DonchianBreakoutStrategy. Single trade per day.

5-minute bars. Session reset on new calendar day.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from acme.broker.base import Bar, BracketSpec
from acme.contracts import MES, FuturesContract
from acme.indicators import ATR
from acme.risk import DailyState, EvalProfile, can_open_new_position, dollars_to_contracts
from acme.strategies.base import Signal, StrategyMetadata
from acme.strategies.params import ParameterSpec


@dataclass
class TurtlesSystem2Config:
    lookback: int = 55
    atr_period: int = 20
    atr_stop_multiple: float = 2.5
    atr_target_multiple: float = 4.0
    risk_dollars_per_trade: float = 25.0


class TurtlesSystem2Strategy:
    name = "turtles_system2"
    version = "1"
    metadata = StrategyMetadata(
        tier=2,
        regime_fit={"trending": 1.0, "volatile": 0.7, "ranging": 0.2, "quiet": 0.4},
        time_buckets=["09:00-13:00"],
        default_lifecycle="SHADOW",
        timeframe_minutes=5,
    )

    @classmethod
    def tunable_params(cls) -> list[ParameterSpec]:
        return [
            ParameterSpec("lookback", int, 55, 30, 100, 1, "Channel lookback bars"),
            ParameterSpec("atr_period", int, 20, 10, 40, 1, "ATR period"),
            ParameterSpec("atr_stop_multiple", float, 2.5, 1.5, 4.0, 0.1, "Stop as ATR multiple"),
            ParameterSpec("atr_target_multiple", float, 4.0, 2.0, 6.0, 0.1, "Target as ATR multiple"),
            ParameterSpec("risk_dollars_per_trade", float, 25.0, 10.0, 200.0, 5.0, "Risk per trade ($)"),
        ]

    def __init__(self, config: TurtlesSystem2Config | None = None, contract: FuturesContract = MES) -> None:
        self.config = config or TurtlesSystem2Config()
        self.contract = contract
        self.timeframe_minutes = self.metadata.timeframe_minutes
        self._highs: deque[float] = deque(maxlen=self.config.lookback)
        self._lows: deque[float] = deque(maxlen=self.config.lookback)
        self._atr = ATR(self.config.atr_period)
        self._last_bar_date: str = ""
        self._fired_today: bool = False

    def required_history_bars(self) -> int:
        return max(self.config.lookback, self.config.atr_period) + 5

    def on_bar(
        self,
        bar: Bar,
        *,
        state: DailyState,
        profile: EvalProfile,
        current_position: int,
        current_balance_unrealized: float,
    ) -> Signal | None:
        from zoneinfo import ZoneInfo
        bar_date = bar.t.astimezone(ZoneInfo("America/Chicago")).date().isoformat()
        if self._last_bar_date and self._last_bar_date != bar_date:
            self._fired_today = False
        self._last_bar_date = bar_date

        prior_high = max(self._highs) if len(self._highs) == self.config.lookback else None
        prior_low = min(self._lows) if len(self._lows) == self.config.lookback else None

        self._highs.append(bar.h)
        self._lows.append(bar.l)
        self._atr.update(bar)

        if prior_high is None or prior_low is None or not self._atr.is_warm:
            return None

        if self._fired_today or current_position != 0:
            return None

        side = None
        if bar.c > prior_high:
            side = "buy"
        elif bar.c < prior_low:
            side = "sell"
        if side is None:
            return None

        atr_val = self._atr.value
        stop_distance_points = atr_val * self.config.atr_stop_multiple
        target_distance_points = atr_val * self.config.atr_target_multiple

        stop_ticks = max(1, int(round(stop_distance_points / self.contract.tick_size)))
        target_ticks = max(1, int(round(target_distance_points / self.contract.tick_size)))

        round_turn_fee = profile.round_turn_fees.get(self.contract.symbol, 0.0)
        size = dollars_to_contracts(
            self.config.risk_dollars_per_trade,
            stop_distance_points,
            self.contract.point_value,
            round_turn_fee=round_turn_fee,
        )
        if size <= 0:
            return None

        allowed, reason = can_open_new_position(
            profile, state, current_balance_unrealized,
            self.contract.symbol, size, current_position,
        )
        if not allowed:
            return Signal(side=side, size=0, reason=f"blocked: {reason}")

        self._fired_today = True
        return Signal(
            side=side,
            size=size,
            bracket=BracketSpec(
                stop_loss_offset_ticks=stop_ticks,
                take_profit_offset_ticks=target_ticks,
            ),
            reason=f"turtles_s2_breakout_{side}",
        )
