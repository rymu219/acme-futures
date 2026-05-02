"""9/21 EMA crossover on 1-minute bars. Phase A's only strategy.

Picked because it has zero session-time dependence (works the moment EMAs warm up),
state is tiny (two EMAs + last cross direction), and the spec is small enough that
correctness is verifiable by inspection. Phase A is exercising rails, not making money.

Bracket orders go via the broker (`stopLossBracket` / `takeProfitBracket`) so the
exchange owns stop/target — robust if the bot crashes mid-trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from acme.broker.base import Bar, BracketSpec
from acme.contracts import MES, FuturesContract
from acme.risk import DailyState, EvalProfile, can_open_new_position, dollars_to_contracts
from acme.strategies.base import Signal, StrategyMetadata
from acme.strategies.params import ParameterSpec

Cross = Literal["up", "down", "none"]


@dataclass
class EmaCrossConfig:
    fast: int = 9
    slow: int = 21
    stop_ticks: int = 8           # 8 * 0.25 = 2.0 pts = $10 risk per contract on MES
    target_ticks: int = 16        # 2R
    risk_dollars_per_trade: float = 25.0


def _ema_step(prev: float | None, value: float, period: int) -> float:
    if prev is None:
        return value
    k = 2.0 / (period + 1.0)
    return value * k + prev * (1.0 - k)


class EmaCrossStrategy:
    name = "ema_cross"
    version = "1"
    metadata = StrategyMetadata(
        tier=2,
        regime_fit={"trending": 1.0, "ranging": 0.3, "volatile": 0.6, "quiet": 0.4},
        time_buckets=StrategyMetadata.default_full_rth(),
        default_lifecycle="PILOT",      # already validated by Phase A round-trip
        timeframe_minutes=1,
    )

    @classmethod
    def tunable_params(cls) -> list[ParameterSpec]:
        return [
            ParameterSpec("fast", int, 9, 3, 30, 1, "Fast EMA period"),
            ParameterSpec("slow", int, 21, 10, 100, 1, "Slow EMA period"),
            ParameterSpec("stop_ticks", int, 8, 4, 40, 1, "Stop distance in ticks"),
            ParameterSpec("target_ticks", int, 16, 4, 60, 1, "Target distance in ticks"),
            ParameterSpec("risk_dollars_per_trade", float, 25.0, 10.0, 200.0, 5.0, "Risk per trade ($)"),
        ]

    def __init__(self, config: EmaCrossConfig | None = None, contract: FuturesContract = MES) -> None:
        self.config = config or EmaCrossConfig()
        self.contract = contract
        self.timeframe_minutes = self.metadata.timeframe_minutes
        self._fast: float | None = None
        self._slow: float | None = None
        self._last_relation: Cross = "none"   # 'up' = fast > slow, 'down' = fast < slow
        self._bars_seen = 0

    def required_history_bars(self) -> int:
        return self.config.slow * 3

    def _update(self, close: float) -> Cross:
        self._fast = _ema_step(self._fast, close, self.config.fast)
        self._slow = _ema_step(self._slow, close, self.config.slow)
        self._bars_seen += 1
        if self._bars_seen < self.config.slow:
            return "none"
        relation: Cross = "up" if self._fast > self._slow else "down"
        if self._last_relation == "none":
            self._last_relation = relation
            return "none"
        if relation != self._last_relation:
            self._last_relation = relation
            return relation
        return "none"

    def on_bar(
        self,
        bar: Bar,
        *,
        state: DailyState,
        profile: EvalProfile,
        current_position: int,
        current_balance_unrealized: float,
    ) -> Signal | None:
        cross = self._update(bar.c)
        if cross == "none":
            return None
        if current_position != 0:
            return None
        side = "buy" if cross == "up" else "sell"
        stop_distance_points = self.config.stop_ticks * self.contract.tick_size
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
            profile,
            state,
            current_balance_unrealized,
            self.contract.symbol,
            size,
            current_position,
        )
        if not allowed:
            return Signal(side=side, size=0, reason=f"blocked: {reason}")
        return Signal(
            side=side,
            size=size,
            bracket=BracketSpec(
                stop_loss_offset_ticks=self.config.stop_ticks,
                take_profit_offset_ticks=self.config.target_ticks,
            ),
            reason=f"ema_cross_{cross}",
        )
