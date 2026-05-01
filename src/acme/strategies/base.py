from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from acme.broker.base import Bar, BracketSpec
from acme.contracts import FuturesContract
from acme.risk import DailyState, EvalProfile

Side = Literal["buy", "sell"]
RegimeLabel = Literal["trending", "ranging", "volatile", "quiet"]
LifecycleState = Literal["BACKTEST", "REPLAY", "SHADOW", "PILOT", "LIVE", "BENCH", "RETIRED"]


@dataclass(frozen=True)
class Signal:
    side: Side
    size: int
    bracket: BracketSpec | None = None
    reason: str = ""


@dataclass(frozen=True)
class StrategyMetadata:
    """Static descriptive metadata about a strategy. Used by the conductor for
    arbitration, the registry for lifecycle defaults, and the UI for display.
    """
    tier: int                                # 1 = highest priority, 3 = lowest
    regime_fit: dict[RegimeLabel, float]     # e.g. {"trending": 1.0, "ranging": 0.2}
    time_buckets: list[str]                  # CT windows the strategy prefers, e.g. ["08:30-14:45"]
    default_lifecycle: LifecycleState        # where this strategy starts on first registration
    timeframe_minutes: int                   # 1, 5, 15, etc.

    @staticmethod
    def default_full_rth() -> list[str]:
        return ["08:30-14:45"]


class Strategy(Protocol):
    name: str
    version: str
    contract: FuturesContract
    timeframe_minutes: int
    metadata: StrategyMetadata

    def required_history_bars(self) -> int: ...

    def on_bar(
        self,
        bar: Bar,
        *,
        state: DailyState,
        profile: EvalProfile,
        current_position: int,
        current_balance_unrealized: float,
    ) -> Signal | None: ...
