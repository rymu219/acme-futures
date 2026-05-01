"""Opening Range Breakout (Williams / Crabel lineage).

Spec:
  1. Build the opening range from the first N minutes of the RTH session
     (default: first three 5-minute bars after 08:30 CT = 15-min OR).
  2. After the OR window closes, look for the first 5-min bar that closes
     above the OR-high (long) or below the OR-low (short).
  3. Volume confirmation: the breakout bar's volume must be ≥ 1.2x the
     average volume of the OR bars (avoids low-conviction breakouts).
  4. Stop: opposite side of the OR.
  5. Target: 1x the OR width projected from the breakout level.
  6. One trade per side per session — once the long side fires (or fails),
     no more longs that day. Same for shorts.

5-minute bars. Strategy resets state at the start of each new RTH session
(detected by a bar whose CT hour:minute < 08:30 OR a >2 hour gap).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from zoneinfo import ZoneInfo

from acme.broker.base import Bar, BracketSpec
from acme.contracts import MES, FuturesContract
from acme.risk import DailyState, EvalProfile, can_open_new_position, dollars_to_contracts
from acme.strategies.base import Signal, StrategyMetadata

CT = ZoneInfo("America/Chicago")


@dataclass
class ORBConfig:
    or_minutes: int = 15
    bar_minutes: int = 5
    rth_open_hh: int = 8
    rth_open_mm: int = 30
    volume_multiple: float = 1.2
    target_or_width_multiple: float = 1.0
    risk_dollars_per_trade: float = 25.0


@dataclass
class _SessionState:
    or_high: float = float("-inf")
    or_low: float = float("inf")
    or_volumes: list[int] = field(default_factory=list)
    or_complete: bool = False
    long_taken: bool = False
    short_taken: bool = False
    or_bars_collected: int = 0
    last_bar_date: str = ""

    def reset(self) -> None:
        self.or_high = float("-inf")
        self.or_low = float("inf")
        self.or_volumes = []
        self.or_complete = False
        self.long_taken = False
        self.short_taken = False
        self.or_bars_collected = 0


class OpeningRangeBreakoutStrategy:
    name = "orb"
    version = "1"
    metadata = StrategyMetadata(
        tier=2,
        regime_fit={"trending": 1.0, "volatile": 0.9, "ranging": 0.4, "quiet": 0.3},
        time_buckets=["08:45-10:30"],
        default_lifecycle="SHADOW",
        timeframe_minutes=5,
    )

    def __init__(self, config: ORBConfig | None = None, contract: FuturesContract = MES) -> None:
        self.config = config or ORBConfig()
        self.contract = contract
        self.timeframe_minutes = self.metadata.timeframe_minutes
        self._session = _SessionState()
        self._or_bars_required = max(1, self.config.or_minutes // self.config.bar_minutes)

    def required_history_bars(self) -> int:
        # Strategy doesn't need historical warmup — it builds the OR live each session.
        return 0

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
        rth_open = time(self.config.rth_open_hh, self.config.rth_open_mm)

        # Reset session on new calendar day
        if self._session.last_bar_date and self._session.last_bar_date != bar_date:
            self._session.reset()
        self._session.last_bar_date = bar_date

        # Skip bars before RTH open
        if bar_ct.time() < rth_open:
            return None

        # Build the opening range
        if not self._session.or_complete:
            self._session.or_high = max(self._session.or_high, bar.h)
            self._session.or_low = min(self._session.or_low, bar.l)
            self._session.or_volumes.append(bar.v)
            self._session.or_bars_collected += 1
            if self._session.or_bars_collected >= self._or_bars_required:
                self._session.or_complete = True
            return None

        if current_position != 0:
            return None

        # Look for breakout
        avg_or_vol = sum(self._session.or_volumes) / len(self._session.or_volumes)
        vol_ok = bar.v >= avg_or_vol * self.config.volume_multiple

        side = None
        if (bar.c > self._session.or_high
                and not self._session.long_taken
                and vol_ok):
            side = "buy"
            self._session.long_taken = True
            stop_price = self._session.or_low
        elif (bar.c < self._session.or_low
                and not self._session.short_taken
                and vol_ok):
            side = "sell"
            self._session.short_taken = True
            stop_price = self._session.or_high

        if side is None:
            return None

        or_width = self._session.or_high - self._session.or_low
        target_distance = or_width * self.config.target_or_width_multiple

        stop_distance_points = bar.c - stop_price if side == "buy" else stop_price - bar.c

        if stop_distance_points <= 0:
            return None

        stop_ticks = max(1, int(round(stop_distance_points / self.contract.tick_size)))
        target_ticks = max(1, int(round(target_distance / self.contract.tick_size)))

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
            reason=f"orb_breakout_{side}_or={or_width:.2f}",
        )
