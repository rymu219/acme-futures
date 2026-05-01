"""Supertrend — ATR-based trend follower with band-locking trend flips.

Spec:
  1. Compute Supertrend(period=10, multiplier=3.0) per bar (see indicators.Supertrend).
  2. Entry on the bar where the indicator's `flipped` flag is True:
       - new trend = +1 → enter LONG
       - new trend = -1 → enter SHORT
  3. Stop: distance from bar.c to the current Supertrend line. Target: 2.0x ATR
     (1:1+ R reward; the line is the protective stop in classic usage, but our
     bracket model fixes both legs at entry — if a runner wants to ride trend
     longer, future Phase C work can add trailing-stop semantics here).
  4. No `_fired_today` gate: trend flips are infrequent on 5-minute bars and
     each flip is a fresh signal. Position guard prevents stacking.

5-minute bars. No session reset (trend persists across days conceptually).
"""

from __future__ import annotations

from dataclasses import dataclass

from acme.broker.base import Bar, BracketSpec
from acme.contracts import MES, FuturesContract
from acme.indicators import ATR, Supertrend
from acme.risk import DailyState, EvalProfile, can_open_new_position, dollars_to_contracts
from acme.strategies.base import Signal, StrategyMetadata


@dataclass
class SupertrendConfig:
    atr_period: int = 10
    multiplier: float = 3.0
    atr_target_multiple: float = 2.0
    risk_dollars_per_trade: float = 25.0


class SupertrendStrategy:
    name = "supertrend"
    version = "1"
    metadata = StrategyMetadata(
        tier=2,
        regime_fit={"trending": 1.0, "volatile": 0.6, "ranging": 0.2, "quiet": 0.4},
        time_buckets=["08:30-14:45"],
        default_lifecycle="SHADOW",
        timeframe_minutes=5,
    )

    def __init__(self, config: SupertrendConfig | None = None, contract: FuturesContract = MES) -> None:
        self.config = config or SupertrendConfig()
        self.contract = contract
        self.timeframe_minutes = self.metadata.timeframe_minutes
        self._supertrend = Supertrend(period=self.config.atr_period, multiplier=self.config.multiplier)
        self._atr = ATR(self.config.atr_period)

    def required_history_bars(self) -> int:
        return self.config.atr_period + 5

    def on_bar(
        self,
        bar: Bar,
        *,
        state: DailyState,
        profile: EvalProfile,
        current_position: int,
        current_balance_unrealized: float,
    ) -> Signal | None:
        st_out = self._supertrend.update(bar)
        self._atr.update(bar)

        if st_out is None or not self._atr.is_warm:
            return None

        if not st_out.flipped:
            return None

        if current_position != 0:
            return None

        side = "buy" if st_out.trend == 1 else "sell"

        stop_distance_points = abs(bar.c - st_out.line)
        if stop_distance_points <= 0:
            return None

        atr_val = self._atr.value
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

        return Signal(
            side=side,
            size=size,
            bracket=BracketSpec(
                stop_loss_offset_ticks=stop_ticks,
                take_profit_offset_ticks=target_ticks,
            ),
            reason=f"supertrend_flip_{side}",
        )
