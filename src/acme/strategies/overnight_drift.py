"""OVERNIGHT_DRIFT — long-only, weak-bullish-bias overnight hold.

Distilled from a 2-year overnight-carry exploration:
  - Strong-body bias bars (|body|>3pt) lose more than weak ones.
  - Long-on-weak is the only positive cohort.
  - 8pt stops on 24pt targets fail asymmetrically (~70% stop / 18%
    target), but the survive-to-08:00 cohort closes 80% in profit —
    so the bias has real directional content; it just doesn't
    develop within a 24pt envelope in 15h.
  - With a band of 2pt <= body <= 3pt, 20pt catastrophic stop, no
    target, and 08:00 force-close, the survive cohort lifts to
    81% WR / PF 22.4 and the strategy lands at PF 1.88.

Logic:
  1. Bias window 15:30-16:00 CT. body = close - open.
  2. Filter: only trade if |body| <= weak_body_threshold (default 3pt)
     AND body > 0 (bullish). Skip everything else.
  3. Entry at 17:00 CT, LONG.
  4. Stop: catastrophic 20pt from entry — sized to survive overnight
     noise, not to bracket the trade.
  5. No profit target. Time is the exit (08:00 CT force-close).
  6. One trade per session.

The bracket interface still needs a target, so we set it absurdly
far (250pt) so it cannot fire in practice — only the stop or the
08:00 force-close ever exits.

Lifecycle: SHADOW.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from typing import Any
from zoneinfo import ZoneInfo

from acme.broker.base import Bar, BracketSpec
from acme.contracts import MES, FuturesContract
from acme.risk import DailyState, EvalProfile, can_open_new_position, dollars_to_contracts
from acme.strategies.base import EvalResult, Signal, StrategyMetadata
from acme.strategies.params import ParameterSpec

CT = ZoneInfo("America/Chicago")

# Unreachable in 15h on MES: 1000 ticks = 250 points.
_UNREACHABLE_TARGET_TICKS = 1000


def _to_minutes(t: time) -> int:
    return t.hour * 60 + t.minute


@dataclass
class OvernightDriftConfig:
    bias_window_start_ct: time = field(default_factory=lambda: time(15, 30))
    bias_window_end_ct: time = field(default_factory=lambda: time(16, 0))
    # Body band: only trade if min_body <= |body| <= max_body.
    # 2-year breakdown showed the 0-1pt bucket loses, 1-2pt is
    # breakeven, and 2-3pt has PF 1.88. Filter to the productive band.
    min_body_points: float = 2.0
    weak_body_threshold_points: float = 3.0
    entry_time_ct: time = field(default_factory=lambda: time(17, 0))
    hard_close_ct: time = field(default_factory=lambda: time(8, 0))
    catastrophic_stop_points: float = 20.0
    # 5-contract sizing for Topstep: 5 × 20pt × $5 + 5 × $1.24 = $506.20.
    # $510 budget gives exactly 5 contracts.
    risk_dollars_per_trade: float = 510.0


class _DayState:
    """Per-CT-calendar-date bias + entry bookkeeping."""
    __slots__ = ("bias_date", "bias_open", "bias_close", "entered")

    def __init__(self, bias_date: date) -> None:
        self.bias_date = bias_date
        self.bias_open: float | None = None
        self.bias_close: float | None = None
        self.entered = False


class OvernightDriftStrategy:
    name = "overnight_drift"
    version = "1"
    metadata = StrategyMetadata(
        tier=2,
        regime_fit={"trending": 0.8, "volatile": 0.4, "ranging": 0.6, "quiet": 1.0},
        time_buckets=["17:00-08:00 CT"],
        default_lifecycle="SHADOW",
        timeframe_minutes=2,
    )

    @classmethod
    def tunable_params(cls) -> list[ParameterSpec]:
        return [
            ParameterSpec("weak_body_threshold_points", float, 3.0, 0.5, 10.0, 0.5,
                          "Max |body| (points) to qualify as 'weak'"),
            ParameterSpec("catastrophic_stop_points", float, 20.0, 5.0, 50.0, 1.0,
                          "Catastrophic stop distance (points)"),
            ParameterSpec("risk_dollars_per_trade", float, 510.0, 25.0, 2000.0, 10.0,
                          "Risk per trade ($) — sized for 5 contracts at default"),
        ]

    def __init__(
        self,
        config: OvernightDriftConfig | None = None,
        contract: FuturesContract = MES,
    ) -> None:
        self.config = config or OvernightDriftConfig()
        self.contract = contract
        self.timeframe_minutes = self.metadata.timeframe_minutes
        self._day: _DayState | None = None

        self._bias_start_min = _to_minutes(self.config.bias_window_start_ct)
        self._bias_end_min = _to_minutes(self.config.bias_window_end_ct)
        self._entry_min = _to_minutes(self.config.entry_time_ct)
        self._hard_close_min = _to_minutes(self.config.hard_close_ct)

        if not (self._bias_start_min < self._bias_end_min
                <= self._entry_min):
            raise ValueError(
                "OvernightDriftConfig: require "
                "bias_start < bias_end <= entry_time"
            )
        if not (0 <= self._hard_close_min < self._entry_min):
            raise ValueError(
                "OvernightDriftConfig: hard_close must precede "
                "entry_time within the same CT day"
            )

    def required_history_bars(self) -> int:
        return 0

    # ───────────────────────── helpers ─────────────────────────

    def _compute_charge_pct(self, body: float | None) -> float:
        """Triangle function on body magnitude.

        - body <= min_body_points: 0  (too weak / doji / bearish)
        - body == weak_body_threshold_points: 1  (peak — just inside the band)
        - body >  weak_body_threshold_points: 0  (too strong)
        - in between: linear ramp from 0 (at min) to 1 (at weak threshold)
        """
        if body is None:
            return 0.0
        min_b = self.config.min_body_points
        max_b = self.config.weak_body_threshold_points
        if body < min_b or body > max_b:
            return 0.0
        if max_b <= min_b:
            return 1.0
        return (body - min_b) / (max_b - min_b)

    def _ct_parts(self, bar: Bar) -> tuple[date, int]:
        ct = bar.t.astimezone(CT)
        return ct.date(), ct.hour * 60 + ct.minute

    def _ensure_day_state(self, bias_date: date) -> _DayState:
        if self._day is None or self._day.bias_date != bias_date:
            self._day = _DayState(bias_date)
        return self._day

    def _in_bias_window(self, minute_of_day: int) -> bool:
        return self._bias_start_min <= minute_of_day < self._bias_end_min

    # ───────────────────────── harness hook ─────────────────────

    def wants_force_flat(self, bar: Bar) -> bool:
        """True during [08:00, 17:00) CT — daytime window where any
        still-open overnight position must be flattened."""
        _, minute = self._ct_parts(bar)
        return self._hard_close_min <= minute < self._entry_min

    # ───────────────────────── on_bar ──────────────────────────

    def on_bar(
        self,
        bar: Bar,
        *,
        state: DailyState,
        profile: EvalProfile,
        current_position: int,
        current_balance_unrealized: float,
    ) -> Signal | EvalResult | None:
        ct_date, ct_minute = self._ct_parts(bar)
        day_snapshot = self._day                            # snapshot before bias-window mutation
        bias_open_now = day_snapshot.bias_open if day_snapshot else None
        bias_close_now = day_snapshot.bias_close if day_snapshot else None
        body_now = (
            bias_close_now - bias_open_now
            if (bias_open_now is not None and bias_close_now is not None) else None
        )

        gate_values: dict[str, Any] = {
            "ct_minute": ct_minute,
            "bias_open": bias_open_now,
            "bias_close": bias_close_now,
            "bias_body_points": (round(body_now, 4) if body_now is not None else None),
            "min_body_points": self.config.min_body_points,
            "weak_body_threshold_points": self.config.weak_body_threshold_points,
            "entry_min": self._entry_min,
            "bias_window_start_min": self._bias_start_min,
            "bias_window_end_min": self._bias_end_min,
            "day_entered": bool(day_snapshot and day_snapshot.entered),
            "charge_pct": round(self._compute_charge_pct(body_now), 4),
        }
        self._last_gate_values = gate_values

        # HOLD — in a position; bracket / force-flat handles exits.
        if current_position != 0:
            return EvalResult(
                outcome="HOLD", gate_failed=None, near_miss=False,
                signal_side=None, gate_values=gate_values,
                reason="HOLD — long position open, bracket/force-flat handles exit",
            )

        # Build the bias bar across 15:30-16:00 CT. This is "still working" —
        # PASS with the build_bias gate so the dashboard knows we're alive.
        if self._in_bias_window(ct_minute):
            day = self._ensure_day_state(ct_date)
            if day.bias_open is None:
                day.bias_open = bar.o
            day.bias_close = bar.c
            gate_values["bias_open"] = day.bias_open
            gate_values["bias_close"] = day.bias_close
            partial_body = day.bias_close - day.bias_open
            gate_values["bias_body_points"] = round(partial_body, 4)
            gate_values["charge_pct"] = round(self._compute_charge_pct(partial_body), 4)
            return EvalResult(
                outcome="PASS", gate_failed="building_bias", near_miss=False,
                signal_side=None, gate_values=gate_values,
                reason=(
                    f"Building bias bar (15:30-16:00 CT) — partial body "
                    f"{partial_body:+.2f}pt at minute {ct_minute}"
                ),
            )

        # Maintenance break 16:00-16:59 CT — bias is sealed, waiting for entry.
        if ct_minute < self._entry_min:
            return EvalResult(
                outcome="PASS", gate_failed="pre_entry_window", near_miss=False,
                signal_side=None, gate_values=gate_values,
                reason=(
                    f"Pre-entry — bias sealed, waiting for 17:00 CT "
                    f"(currently {ct_minute // 60:02d}:{ct_minute % 60:02d} CT)"
                ),
            )

        # Entry phase: at or after 17:00 CT on the same calendar date as the
        # bias bar. (No bias today → no trade today.)
        day = self._day
        if day is None or day.bias_date != ct_date:
            return EvalResult(
                outcome="PASS", gate_failed="no_bias_today", near_miss=False,
                signal_side=None, gate_values=gate_values,
                reason="No bias bar built for today's date (missed 15:30-16:00 CT window)",
            )
        if day.entered:
            # NEAR — daily entry slot already consumed. The "external"
            # blocker here is the once-per-session rule.
            return EvalResult(
                outcome="NEAR", gate_failed=None, near_miss=True,
                signal_side="buy", gate_values=gate_values,
                reason="All gates PASS — daily entry slot already consumed today",
            )
        if day.bias_open is None or day.bias_close is None:
            return EvalResult(
                outcome="PASS", gate_failed="bias_incomplete", near_miss=False,
                signal_side=None, gate_values=gate_values,
                reason="Bias bar incomplete — missing open or close",
            )

        body = day.bias_close - day.bias_open
        gate_values["bias_body_points"] = round(body, 4)
        gate_values["charge_pct"] = round(self._compute_charge_pct(body), 4)

        # WEAK BULLISH BAND. Reject bearish/doji, too small, too strong.
        if body <= 0:
            return EvalResult(
                outcome="PASS", gate_failed="bias_body_direction", near_miss=False,
                signal_side="buy", gate_values=gate_values,
                reason=f"Bias bar bearish/doji ({body:+.2f}pt) — needed > 0",
            )
        if body < self.config.min_body_points:
            shortfall = self.config.min_body_points - body
            return EvalResult(
                outcome="PASS", gate_failed="bias_body_too_weak", near_miss=False,
                signal_side="buy", gate_values=gate_values,
                reason=(
                    f"Bias body too weak — {body:+.2f}pt, "
                    f"needed >= {self.config.min_body_points:.1f}pt (short {shortfall:.2f}pt)"
                ),
            )
        if body > self.config.weak_body_threshold_points:
            overshoot = body - self.config.weak_body_threshold_points
            return EvalResult(
                outcome="PASS", gate_failed="bias_body_too_strong", near_miss=False,
                signal_side="buy", gate_values=gate_values,
                reason=(
                    f"Bias body too strong — {body:+.2f}pt, "
                    f"needed <= {self.config.weak_body_threshold_points:.1f}pt "
                    f"(over by {overshoot:.2f}pt — conviction bullish, not weak bullish)"
                ),
            )

        stop_distance = self.config.catastrophic_stop_points
        tick = self.contract.tick_size
        stop_ticks = max(1, int(round(stop_distance / tick)))

        round_turn_fee = profile.round_turn_fees.get(self.contract.symbol, 0.0)
        size = dollars_to_contracts(
            self.config.risk_dollars_per_trade,
            stop_distance,
            self.contract.point_value,
            round_turn_fee=round_turn_fee,
        )
        if size <= 0:
            return EvalResult(
                outcome="PASS", gate_failed="size_zero", near_miss=False,
                signal_side="buy", gate_values=gate_values,
                reason=f"Bias in band ({body:+.2f}pt) but sizing returned 0 contracts",
            )

        allowed, block_reason = can_open_new_position(
            profile, state, current_balance_unrealized,
            self.contract.symbol, size, current_position,
        )
        if not allowed:
            return Signal(side="buy", size=0,
                          reason=f"blocked: {block_reason}")

        day.entered = True
        # Encode body in reason as integer hundredths (e.g. 1.50 → 150)
        # so the breakdown can bucket trades by body size precisely.
        body_int = int(round(body * 100))
        return Signal(
            side="buy",
            size=size,
            bracket=BracketSpec(
                stop_loss_offset_ticks=stop_ticks,
                # No real target — 250pt is unreachable in 15h on MES.
                take_profit_offset_ticks=_UNREACHABLE_TARGET_TICKS,
            ),
            reason=f"overnight_drift_long_b{body_int:04d}",
        )
