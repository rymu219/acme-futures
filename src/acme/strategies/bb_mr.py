"""Bollinger Bands mean reversion (range-regime contrarian).

Spec:
  1. BB(20, 2σ) on closes.
  2. ADX(14) < 20 required (trade only in ranging regimes; trend bots own the
     trending regimes — this is a hedge against them all being wrong at once).
  3. RSI(2) for entry trigger:
       - Long entry when low touches lower band AND RSI(2) < 5.
       - Short entry when high touches upper band AND RSI(2) > 95.
  4. Stop: 1.5x ATR(14) beyond entry.
  5. Target: middle band (20-SMA, the BB midline).
  6. Daily cap: max 2 trades per day (avoid the "death by mean reversion in a
     trend" failure mode).

5-minute bars, mid-day only (10:30-14:00 CT) to avoid open + close volatility.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo

from acme.broker.base import Bar, BracketSpec
from acme.contracts import MES, FuturesContract
from acme.indicators import ADX, ATR, RSI, Bollinger
from acme.risk import DailyState, EvalProfile, can_open_new_position, dollars_to_contracts
from acme.strategies.base import Signal, StrategyMetadata

CT = ZoneInfo("America/Chicago")


@dataclass
class BBMRConfig:
    bb_period: int = 20
    bb_std: float = 2.0
    rsi_period: int = 2
    rsi_long_threshold: float = 5.0
    rsi_short_threshold: float = 95.0
    adx_period: int = 14
    adx_max_for_range: float = 20.0
    atr_period: int = 14
    stop_atr_multiple: float = 1.5
    daily_trade_cap: int = 2
    earliest_hh: int = 10
    earliest_mm: int = 30
    latest_hh: int = 14
    latest_mm: int = 0
    risk_dollars_per_trade: float = 25.0


class BollingerMeanReversionStrategy:
    name = "bb_mr"
    version = "1"
    metadata = StrategyMetadata(
        tier=3,
        regime_fit={"ranging": 1.0, "quiet": 0.7, "trending": 0.1, "volatile": 0.3},
        time_buckets=["10:30-14:00"],
        default_lifecycle="SHADOW",
        timeframe_minutes=5,
    )

    def __init__(self, config: BBMRConfig | None = None, contract: FuturesContract = MES) -> None:
        self.config = config or BBMRConfig()
        self.contract = contract
        self.timeframe_minutes = self.metadata.timeframe_minutes
        self._bb = Bollinger(self.config.bb_period, self.config.bb_std)
        self._rsi = RSI(self.config.rsi_period)
        self._adx = ADX(self.config.adx_period)
        self._atr = ATR(self.config.atr_period)
        self._last_bar_date: str = ""
        self._trades_today: int = 0

    def required_history_bars(self) -> int:
        return max(self.config.bb_period, self.config.adx_period, self.config.atr_period) * 2

    def on_bar(
        self,
        bar: Bar,
        *,
        state: DailyState,
        profile: EvalProfile,
        current_position: int,
        current_balance_unrealized: float,
    ) -> Signal | None:
        bar_ct = bar.t.astimezone(CT)
        bar_date = bar_ct.date().isoformat()
        if self._last_bar_date and self._last_bar_date != bar_date:
            self._trades_today = 0
        self._last_bar_date = bar_date

        bb = self._bb.update(bar.c)
        self._rsi.update(bar.c)
        self._adx.update(bar)
        self._atr.update(bar)

        if bb is None or not self._rsi.is_warm or not self._adx.is_warm or not self._atr.is_warm:
            return None
        if current_position != 0:
            return None
        if self._trades_today >= self.config.daily_trade_cap:
            return None

        # Time window: mid-day only
        earliest = time(self.config.earliest_hh, self.config.earliest_mm)
        latest = time(self.config.latest_hh, self.config.latest_mm)
        if bar_ct.time() < earliest or bar_ct.time() > latest:
            return None

        # Range regime check
        if self._adx.value > self.config.adx_max_for_range:
            return None

        rsi_val = self._rsi.value
        side = None
        if bar.l <= bb.lower and rsi_val < self.config.rsi_long_threshold:
            side = "buy"
        elif bar.h >= bb.upper and rsi_val > self.config.rsi_short_threshold:
            side = "sell"
        if side is None:
            return None

        atr_val = self._atr.value
        stop_distance_points = atr_val * self.config.stop_atr_multiple
        # Target is the middle band — distance from bar.c
        target_distance_points = bb.middle - bar.c if side == "buy" else bar.c - bb.middle

        if stop_distance_points <= 0 or target_distance_points <= 0:
            return None

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

        self._trades_today += 1
        return Signal(
            side=side,
            size=size,
            bracket=BracketSpec(
                stop_loss_offset_ticks=stop_ticks,
                take_profit_offset_ticks=target_ticks,
            ),
            reason=f"bb_mr_{side}_adx={self._adx.value:.1f}",
        )
