"""GO/NO-GO with BOUNDARY-style level proximity — fourth fleet keeper.

Composition:
  - GO/NO-GO three-AND gate (EMA(9/14) separation, volume-ratio,
    slope alignment) — same engine as `go_no_go.py`
  - Direction-correlated key-level proximity:
      LONG  signal needs bar.low  within `level_buffer_ticks` of a
                          low-side level (PDL, ONL, ORL)
      SHORT signal needs bar.high within `level_buffer_ticks` of a
                          high-side level (PDH, ONH, ORH)
  - Time gate: 08:00-12:00 CT (morning window)
  - Bracket: 1.5 × ATR stop, 2.5 × ATR target — same as IGNITION
  - Direction: bidirectional by default
  - No min-bars-held opposite-exit; reverses immediately on opposite signal

Day-levels (PDH/PDL/ONH/ONL/ORH/ORL) are supplied via `set_levels()`
at trade-date rollover, same contract as BOUNDARY. The conductor /
backtest harness owns the rollover wiring.

Lifecycle: SHADOW. Promotion through PILOT → LIVE goes through
PerfTracker as usual.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from acme.broker.base import Bar, BracketSpec
from acme.contracts import MES, FuturesContract
from acme.indicators import ATR
from acme.levels import DayLevels
from acme.risk import DailyState, EvalProfile, can_open_new_position, dollars_to_contracts
from acme.strategies.base import EvalResult, Signal, StrategyMetadata
from acme.strategies.go_no_go import GoNoGoConfig, GoNoGoEngine
from acme.strategies.params import ParameterSpec

CT = ZoneInfo("America/Chicago")


@dataclass(frozen=True)
class LevelsTimeWindow:
    start: time
    end: time

    def contains(self, ts: datetime) -> bool:
        ct = ts.astimezone(CT).time()
        return self.start <= ct < self.end

    @classmethod
    def from_hours(cls, start_hour: int, end_hour: int) -> LevelsTimeWindow:
        return cls(time(start_hour, 0), time(end_hour, 0))


@dataclass
class GoNoGoLevelsConfig:
    go_no_go: GoNoGoConfig = field(default_factory=GoNoGoConfig)

    # Morning window — on by default; the level-proximity edge is
    # window-specific (08-12 CT morning thesis).
    time_windows: tuple[LevelsTimeWindow, ...] = field(
        default_factory=lambda: (LevelsTimeWindow.from_hours(8, 12),),
    )

    # Level proximity — same default as BoundaryConfig.level_buffer_ticks.
    level_buffer_ticks: int = 4

    # Bracket / sizing
    atr_period: int = 4
    atr_stop_multiple: float = 1.5
    atr_target_multiple: float = 2.5

    # Direction policy — bidirectional
    allow_longs: bool = True
    allow_shorts: bool = True

    # Sizing
    risk_dollars_per_trade: float = 25.0


class GoNoGoLevelsStrategy:
    """GO/NO-GO + direction-correlated key-level proximity."""

    name = "go_no_go_levels"
    version = "1"
    metadata = StrategyMetadata(
        tier=2,
        regime_fit={"trending": 1.0, "volatile": 0.7, "ranging": 0.5, "quiet": 0.2},
        time_buckets=["08:00-12:00 CT"],
        default_lifecycle="SHADOW",
        timeframe_minutes=2,
    )

    @classmethod
    def tunable_params(cls) -> list[ParameterSpec]:
        return [
            ParameterSpec("level_buffer_ticks", int, 4, 1, 16, 1,
                          "Bar high/low must be within this many ticks of a level"),
            ParameterSpec("atr_stop_multiple", float, 1.5, 0.5, 4.0, 0.1,
                          "ATR multiple for stop loss"),
            ParameterSpec("atr_target_multiple", float, 2.5, 1.0, 6.0, 0.1,
                          "ATR multiple for take profit"),
            ParameterSpec("risk_dollars_per_trade", float, 25.0, 10.0, 200.0, 5.0,
                          "Risk per trade ($)"),
        ]

    def __init__(
        self,
        config: GoNoGoLevelsConfig | None = None,
        contract: FuturesContract = MES,
    ) -> None:
        self.config = config or GoNoGoLevelsConfig()
        self.contract = contract
        self.timeframe_minutes = self.metadata.timeframe_minutes
        self._gng = GoNoGoEngine(self.config.go_no_go)
        self._atr = ATR(self.config.atr_period)
        self._levels: DayLevels | None = None

    def required_history_bars(self) -> int:
        return max(
            self._gng.required_history_bars(),
            self.config.atr_period,
        ) + 2

    # ───────────────────────── charge_pct ──────────────────────────

    def _compute_charge_pct(
        self, gng_state, level_dist_ticks: float | None,
    ) -> float:
        """Mean of the four normalized gate values, clamped to [0, 1]:

          sep_term     = min(abs_sep / sep_thr, 1.0)
          rvol_term    = min(vr / vr_thr, 1.0)
          slope_term   = 1.0 if (up_aligned or down_aligned) else 0.0
          level_term   = max(0, 1 - level_dist_ticks / buffer_ticks)
                         (0 if no level set)
          charge_pct   = mean(sep, rvol, slope, level)
        """
        cfg = self.config.go_no_go
        sep_term = min(gng_state.abs_sep / cfg.sep_thr, 1.0) if cfg.sep_thr > 0 else 1.0
        rvol_term = min(gng_state.vr / cfg.vr_thr, 1.0) if cfg.vr_thr > 0 else 1.0
        slope_term = 1.0 if (gng_state.up_aligned or gng_state.down_aligned) else 0.0
        if level_dist_ticks is None or self.config.level_buffer_ticks <= 0:
            level_term = 0.0
        else:
            level_term = max(0.0, 1.0 - level_dist_ticks / self.config.level_buffer_ticks)
        charge = (sep_term + rvol_term + slope_term + level_term) / 4.0
        return min(1.0, max(0.0, charge))

    # ───────────────────────── levels ──────────────────────────

    def set_levels(self, levels: DayLevels) -> None:
        """Called at each trading-day rollover with the day's six levels."""
        self._levels = levels

    # ───────────────────────── helpers ─────────────────────────

    def _in_window(self, ts: datetime) -> bool:
        if not self.config.time_windows:
            return True
        return any(w.contains(ts) for w in self.config.time_windows)

    def _nearest_level_for_side(
        self, bar: Bar, side: str,
    ) -> tuple[str | None, float | None, float | None]:
        """For a given trade side, find the closest direction-correlated
        level and its distance in ticks. Returns (name, value, dist_ticks).

        Returns (None, None, None) when no levels are set or no level on
        the relevant side is defined.
        """
        if self._levels is None:
            return None, None, None
        if side == "buy":
            named = {"PDL": self._levels.pdl,
                     "ONL": self._levels.onl,
                     "ORL": self._levels.orl}
            price = bar.l
        else:
            named = {"PDH": self._levels.pdh,
                     "ONH": self._levels.onh,
                     "ORH": self._levels.orh}
            price = bar.h
        valid = [(n, v) for n, v in named.items() if v is not None]
        if not valid:
            return None, None, None
        name, lvl = min(valid, key=lambda kv: abs(price - kv[1]))
        ticks = abs(price - lvl) / self.contract.tick_size
        return name, lvl, ticks

    def _level_proximity_ok(self, bar: Bar, side: str) -> bool:
        _, _, ticks = self._nearest_level_for_side(bar, side)
        return ticks is not None and ticks <= self.config.level_buffer_ticks

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
        gng = self._gng.update(bar)
        self._atr.update(bar)

        # Warmup — return bare None; the conductor doesn't log warmup bars.
        if gng is None or not self._atr.is_warm:
            return None

        signal_raw = gng.signal
        in_window = self._in_window(bar.t)
        cfg = self.config.go_no_go

        # Pre-compute level proximity for the side the signal would take
        # (or both sides if signal_raw == 0, for diagnostics).
        if signal_raw == 1:
            lvl_side = "buy"
        elif signal_raw == -1:
            lvl_side = "sell"
        else:
            # No directional signal; report low-side proximity by convention.
            lvl_side = "buy"
        lvl_name, lvl_value, lvl_dist_ticks = self._nearest_level_for_side(bar, lvl_side)
        level_proximity_ok = (
            lvl_dist_ticks is not None
            and lvl_dist_ticks <= self.config.level_buffer_ticks
        )

        gate_values: dict[str, Any] = {
            "signal_raw": signal_raw,
            "abs_sep": round(gng.abs_sep, 4),
            "sep_thr": cfg.sep_thr,
            "sep_ok": gng.sep_ok,
            "vr": round(gng.vr, 4),
            "vr_thr": cfg.vr_thr,
            "vr_rising": gng.vr_rising,
            "vr_ok": gng.vr_ok,
            "slope_fast": round(gng.slope_fast, 6),
            "slope_slow": round(gng.slope_slow, 6),
            "up_aligned": gng.up_aligned,
            "down_aligned": gng.down_aligned,
            "ema_fast": round(gng.ema_fast, 4),
            "ema_slow": round(gng.ema_slow, 4),
            "in_window": in_window,
            "side_checked": lvl_side,
            "nearest_level_name": lvl_name,
            "nearest_level_value": lvl_value,
            "level_dist_ticks": (
                round(lvl_dist_ticks, 2) if lvl_dist_ticks is not None else None
            ),
            "buffer_ticks": self.config.level_buffer_ticks,
            "level_proximity_ok": level_proximity_ok,
            "charge_pct": round(self._compute_charge_pct(gng, lvl_dist_ticks), 4),
        }
        self._last_gate_values = gate_values

        # ────── In-position branches — all HOLD ──────
        if current_position != 0:
            holding_long = current_position > 0
            if signal_raw == 0:
                return EvalResult(
                    outcome="HOLD", gate_failed=None, near_miss=False,
                    signal_side=None, gate_values=gate_values,
                    reason="HOLD — in position, GO/NO-GO WAIT (no opposite signal)",
                )
            new_long = signal_raw == 1
            if holding_long == new_long:
                return EvalResult(
                    outcome="HOLD", gate_failed=None, near_miss=False,
                    signal_side="buy" if new_long else "sell",
                    gate_values=gate_values,
                    reason="HOLD — in position, same-direction signal (no pyramiding)",
                )
            # Opposite signal while in position — reverse if all gates pass.
            side = "buy" if new_long else "sell"
            # Recompute level proximity for the reversal side (may differ from
            # the side we computed above if signal_raw flipped).
            r_name, r_value, r_dist = self._nearest_level_for_side(bar, side)
            gate_values["side_checked"] = side
            gate_values["nearest_level_name"] = r_name
            gate_values["nearest_level_value"] = r_value
            gate_values["level_dist_ticks"] = (
                round(r_dist, 2) if r_dist is not None else None
            )
            r_prox_ok = r_dist is not None and r_dist <= self.config.level_buffer_ticks
            gate_values["level_proximity_ok"] = r_prox_ok
            gate_values["charge_pct"] = round(self._compute_charge_pct(gng, r_dist), 4)
            if not r_prox_ok:
                return EvalResult(
                    outcome="HOLD", gate_failed="level_proximity",
                    near_miss=False, signal_side=side, gate_values=gate_values,
                    reason=(
                        f"HOLD — opposite signal but level too far "
                        f"({r_name or '—'} {r_dist:.1f}t > {self.config.level_buffer_ticks}t buffer)"
                        if r_dist is not None
                        else "HOLD — opposite signal but no levels defined"
                    ),
                )
            if not in_window:
                return EvalResult(
                    outcome="HOLD", gate_failed=None, near_miss=False,
                    signal_side=side, gate_values=gate_values,
                    reason="HOLD — opposite signal + level OK, but outside reversal window",
                )
            return self._build_signal(
                bar=bar, side=side, state=state, profile=profile,
                current_position=current_position,
                current_balance_unrealized=current_balance_unrealized,
                reason="gng_levels_reverse",
            )

        # ────── Flat: evaluate every gate, then classify ──────

        # Internal gate 1: GO/NO-GO produced a directional signal.
        if signal_raw == 0:
            # Identify which sub-gate of GO/NO-GO failed first.
            if not gng.sep_ok:
                sub_gate = "sep_ok"
                sub_reason = (
                    f"GO/NO-GO WAIT — EMA sep {gng.abs_sep:.2f} below threshold {cfg.sep_thr:.2f}"
                )
            elif not gng.vr_ok:
                sub_gate = "vr_ok"
                sub_reason = (
                    f"GO/NO-GO WAIT — RVOL {gng.vr:.2f} below threshold {cfg.vr_thr:.2f}"
                    + ("" if gng.vr_rising else " (and not rising)")
                )
            else:
                sub_gate = "slope_aligned"
                sub_reason = (
                    f"GO/NO-GO WAIT — slopes split "
                    f"(fast {gng.slope_fast:+.3f}, slow {gng.slope_slow:+.3f})"
                )
            return EvalResult(
                outcome="PASS", gate_failed=sub_gate, near_miss=False,
                signal_side=None, gate_values=gate_values,
                reason=sub_reason,
            )

        side = "buy" if signal_raw == 1 else "sell"

        # Internal gate 2: level proximity for the signaled side.
        if not level_proximity_ok:
            return EvalResult(
                outcome="PASS", gate_failed="level_proximity",
                near_miss=False, signal_side=side, gate_values=gate_values,
                reason=(
                    f"Level gate miss — nearest {lvl_name or '—'} "
                    f"{lvl_dist_ticks:.1f}t away (buffer {self.config.level_buffer_ticks}t)"
                    if lvl_dist_ticks is not None
                    else "Level gate miss — no day-levels set yet"
                ),
            )

        # All internal gates pass. Check externals.
        # External 1: time window (08:00-12:00 CT by default).
        if not in_window:
            return EvalResult(
                outcome="NEAR", gate_failed=None, near_miss=True,
                signal_side=side, gate_values=gate_values,
                reason=(
                    f"All gates PASS · {lvl_name} {lvl_dist_ticks:.1f}t — "
                    f"outside 08-12 CT trading window"
                ),
            )
        # External 2: direction policy.
        if signal_raw == 1 and not self.config.allow_longs:
            return EvalResult(
                outcome="NEAR", gate_failed=None, near_miss=True,
                signal_side="buy", gate_values=gate_values,
                reason=(
                    f"All gates PASS · {lvl_name} {lvl_dist_ticks:.1f}t — "
                    f"longs disabled by direction policy"
                ),
            )
        if signal_raw == -1 and not self.config.allow_shorts:
            return EvalResult(
                outcome="NEAR", gate_failed=None, near_miss=True,
                signal_side="sell", gate_values=gate_values,
                reason=(
                    f"All gates PASS · {lvl_name} {lvl_dist_ticks:.1f}t — "
                    f"shorts disabled by direction policy"
                ),
            )

        # Every gate passes. Fire.
        return self._build_signal(
            bar=bar, side=side, state=state, profile=profile,
            current_position=current_position,
            current_balance_unrealized=current_balance_unrealized,
            reason="gng_levels_entry",
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
