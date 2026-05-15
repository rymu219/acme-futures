from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from acme.broker.base import Bar, BracketSpec
from acme.contracts import FuturesContract
from acme.risk import DailyState, EvalProfile

Side = Literal["buy", "sell"]
RegimeLabel = Literal["trending", "ranging", "volatile", "quiet"]
LifecycleState = Literal["BACKTEST", "REPLAY", "SHADOW", "PILOT", "LIVE", "BENCH", "RETIRED"]
EvalOutcome = Literal["PASS", "NEAR", "ENTRY", "HOLD"]


@dataclass(frozen=True)
class Signal:
    side: Side
    size: int
    bracket: BracketSpec | None = None
    reason: str = ""


@dataclass(frozen=True)
class EvalResult:
    """Returned by `on_bar` when the strategy evaluated the bar but did
    not fire. The conductor logs one row per EvalResult to the `eval_log`
    Supabase table — surfacing the work that happens between trades.

    Outcomes:
      PASS  — one of the strategy's own gates failed (`gate_failed`
              names the first blocking gate).
      NEAR  — every entry gate passed cleanly but an *external* condition
              blocked the trade (outside time window, daily slot already
              consumed, direction policy override, daily loss limit hit).
              The `near_miss` flag mirrors this — see the eval_log
              migration comment for why this is the primary
              invisible-work signal.
      HOLD  — strategy is already in a position (or in a session-locked
              state) and chose not to act; bracket/force-flat handles exit.

    Note `ENTRY` is also a valid string the *conductor* writes to
    eval_log when a Signal fires, but strategies themselves never
    *return* an EvalResult with outcome=ENTRY — they return a Signal
    for that case. The conductor turns the Signal into the ENTRY row.

    All fields are required (no defaults). near_miss in particular has
    no default because PostgREST overrides Postgres column defaults with
    explicit nulls on missing JSON keys, which violates the eval_log
    NOT NULL constraint; making it required at the dataclass level
    forces every call site to be explicit.
    """
    outcome: EvalOutcome
    gate_failed: str | None
    near_miss: bool
    signal_side: Side | None
    gate_values: dict[str, Any]
    reason: str


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
    ) -> Signal | EvalResult | None: ...
