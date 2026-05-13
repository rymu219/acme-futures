"""GAP_FILL — fade the overnight gap toward prior session's 16:00 CT close.

Logic:
  1. Track the close of the 15:58-16:00 CT bar each day → prior_close.
  2. At the 08:30 CT bar, gap = bar.o − prior_close. If |gap| <
     min_gap_points, skip the session.
  3. Direction:
        gap > 0 (opened above prior close) → SHORT (fade down)
        gap < 0 (opened below prior close) → LONG  (fade up)
  4. Entry: close of the first bar at or after 08:30 CT.
  5. Target: prior_close itself — the actual gap-fill level. Bracket
     target_offset = |entry − prior_close| in ticks.
  6. Stop: stop_gap_multiple × |gap| on the wrong side of entry.
  7. Hard close at 13:00 CT via `wants_force_flat`.
  8. One trade per session; one direction; no re-entries.

If the 08:30 bar moves through prior_close before its close (entry on
the wrong side of the fill target), the trade is skipped — target
distance would be <= 0.

Lifecycle: SHADOW.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time, timedelta
from zoneinfo import ZoneInfo

from acme.broker.base import Bar, BracketSpec
from acme.contracts import MES, FuturesContract
from acme.risk import DailyState, EvalProfile, can_open_new_position, dollars_to_contracts
from acme.strategies.base import Signal, StrategyMetadata
from acme.strategies.params import ParameterSpec

CT = ZoneInfo("America/Chicago")


def _to_minutes(t: time) -> int:
    return t.hour * 60 + t.minute


@dataclass
class GapFillConfig:
    # Filter. The by-gap-bucket breakdown showed 4-12pt cohorts net
    # negative (PF 0.88-0.90) while 12pt+ delivered PF 1.68 / +$1,701.
    # Defaulted to the productive bucket.
    min_gap_points: float = 12.0
    # Stop = entry +/- stop_gap_multiple * |gap| on the wrong side.
    stop_gap_multiple: float = 1.0
    # Timing
    prior_close_time_ct: time = field(default_factory=lambda: time(15, 58))
    entry_time_ct: time = field(default_factory=lambda: time(8, 30))
    hard_close_ct: time = field(default_factory=lambda: time(13, 0))
    # Sizing — spec didn't list this, $25 sizes most gaps to 0 contracts;
    # $100 supports 1 contract for stops up to ~20pt.
    risk_dollars_per_trade: float = 100.0
    # If set, overrides dollars_to_contracts and forces a fixed
    # contract count regardless of stop distance. Used in live
    # trading where predictable position sizing matters more than
    # per-trade risk budgeting (combined-fleet analysis sized GAP_FILL
    # at fixed 3 contracts for Topstep MLL compliance).
    fixed_contracts: int | None = None
    allow_longs: bool = True
    allow_shorts: bool = True


class _DayState:
    """Per-CT-calendar-date entry bookkeeping. Bias (prior_close) lives
    in the strategy's _prior_closes dict, keyed by date."""
    __slots__ = ("session_date", "entered")

    def __init__(self, session_date: date) -> None:
        self.session_date = session_date
        self.entered = False


class GapFillStrategy:
    name = "gap_fill"
    version = "1"
    metadata = StrategyMetadata(
        tier=2,
        regime_fit={"trending": 0.4, "volatile": 0.8, "ranging": 1.0, "quiet": 0.5},
        time_buckets=["08:30-13:00 CT"],
        default_lifecycle="SHADOW",
        timeframe_minutes=2,
    )

    @classmethod
    def tunable_params(cls) -> list[ParameterSpec]:
        return [
            ParameterSpec("min_gap_points", float, 4.0, 1.0, 30.0, 0.5,
                          "Minimum |gap| (points) to qualify"),
            ParameterSpec("stop_gap_multiple", float, 1.0, 0.25, 3.0, 0.25,
                          "Stop distance as multiple of |gap|"),
            ParameterSpec("risk_dollars_per_trade", float, 100.0, 25.0, 1000.0, 10.0,
                          "Risk per trade ($)"),
        ]

    def __init__(
        self,
        config: GapFillConfig | None = None,
        contract: FuturesContract = MES,
    ) -> None:
        self.config = config or GapFillConfig()
        self.contract = contract
        self.timeframe_minutes = self.metadata.timeframe_minutes
        self._day: _DayState | None = None
        # Cache of prior-day 16:00 CT close, keyed by the date that bar
        # belongs to. Looked up at next 08:30 entry.
        self._prior_closes: dict[date, float] = {}

        self._prior_close_min = _to_minutes(self.config.prior_close_time_ct)
        self._entry_min = _to_minutes(self.config.entry_time_ct)
        self._hard_close_min = _to_minutes(self.config.hard_close_ct)

        if not (self._entry_min < self._hard_close_min):
            raise ValueError("GapFillConfig: entry_time must precede hard_close")

    def required_history_bars(self) -> int:
        return 0

    # ───────────────────────── helpers ─────────────────────────

    def _ct_parts(self, bar: Bar) -> tuple[date, int]:
        ct = bar.t.astimezone(CT)
        return ct.date(), ct.hour * 60 + ct.minute

    def _ensure_day_state(self, session_date: date) -> _DayState:
        if self._day is None or self._day.session_date != session_date:
            self._day = _DayState(session_date)
        return self._day

    def _lookup_prior_close(self, session_date: date) -> float | None:
        """Walk back up to 7 calendar days to find the most recent
        recorded close (handles weekends and holidays)."""
        for delta in range(1, 8):
            d = session_date - timedelta(days=delta)
            if d in self._prior_closes:
                return self._prior_closes[d]
        return None

    # ───────────────────────── harness hook ─────────────────────

    def wants_force_flat(self, bar: Bar) -> bool:
        _, minute = self._ct_parts(bar)
        return minute >= self._hard_close_min

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
        ct_date, ct_minute = self._ct_parts(bar)

        # Always track the 15:58 CT bar's close (= 16:00 CT print).
        # Runs even when in position so we don't miss the recording.
        if ct_minute == self._prior_close_min:
            self._prior_closes[ct_date] = bar.c

        if current_position != 0:
            return None

        # Only fire on the bar at the entry time.
        if ct_minute != self._entry_min:
            return None

        day = self._ensure_day_state(ct_date)
        if day.entered:
            return None

        prior_close = self._lookup_prior_close(ct_date)
        if prior_close is None:
            return None

        gap = bar.o - prior_close
        if abs(gap) < self.config.min_gap_points:
            return None

        # Entry on the bar's close (harness convention).
        entry = bar.c
        if gap > 0:
            # Gap-up → fade short. Target is prior_close (below entry).
            if not self.config.allow_shorts:
                return None
            target_distance = entry - prior_close
            stop_distance = self.config.stop_gap_multiple * abs(gap)
            side = "sell"
        else:
            # Gap-down → fade long. Target is prior_close (above entry).
            if not self.config.allow_longs:
                return None
            target_distance = prior_close - entry
            stop_distance = self.config.stop_gap_multiple * abs(gap)
            side = "buy"

        if target_distance <= 0 or stop_distance <= 0:
            # Bar already crossed back through prior_close before close —
            # fill happened during the entry bar; skip.
            return None

        tick = self.contract.tick_size
        stop_ticks = max(1, int(round(stop_distance / tick)))
        target_ticks = max(1, int(round(target_distance / tick)))

        round_turn_fee = profile.round_turn_fees.get(self.contract.symbol, 0.0)
        if self.config.fixed_contracts is not None:
            size = self.config.fixed_contracts
        else:
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
            return Signal(side=side, size=0,  # type: ignore[arg-type]
                          reason=f"blocked: {block_reason}")

        day.entered = True
        direction = "long" if side == "buy" else "short"
        # Encode gap (signed, integer hundredths) in reason for analysis.
        gap_int = int(round(gap * 100))
        return Signal(
            side=side,  # type: ignore[arg-type]
            size=size,
            bracket=BracketSpec(
                stop_loss_offset_ticks=stop_ticks,
                take_profit_offset_ticks=target_ticks,
            ),
            reason=f"gap_fill_{direction}_g{gap_int:+06d}",
        )
