"""VWAP_MOMENTUM — ride extensions away from intraday VWAP.

Thesis: when price extends `entry_threshold_pts` away from intraday VWAP in
the morning RTH session, the move has enough conviction to continue. Enter in
the direction of the extension, ride with a trailing stop, no fixed target.
The trailing stop captures the bulk of the move and exits before the
inevitable mean-reversion back through VWAP.

Logic:
  1. Compute intraday VWAP from the 08:30 CT session-open bar onward, weighted
     by volume on typical price (h+l+c)/3. Resets each CT calendar date.
  2. Entry: price closes `entry_threshold_pts` above VWAP → long; below →
     would be short, but long-only by default.
  3. Initial stop: `initial_stop_pts` below entry (long). Hard floor.
  4. Trailing stop: `trail_distance_pts` below the high watermark. Updates
     each new high. Effective stop = max(initial_stop, hw - trail).
  5. Hard close at 13:00 CT via `wants_force_flat`.
  6. One trade per session; once exited, no re-entry.

LIVE INTEGRATION CAVEAT — the existing Conductor's bracket model exits on a
fixed stop/target. It has no hook to tighten the stop as a position runs.
This strategy emits a Signal with `initial_stop_pts` as the bracket stop and
a far-unreachable target; in live mode the trailing stop is NOT honored, and
the position would only exit on the initial stop or the 13:00 force-flat.
The trailing stop logic lives in `scripts/backtest_vwap_momentum.py` for the
backtest, and a future conductor hook (e.g. `update_trailing_stop(pos, bar)`)
would be needed to make this strategy trade live with trailing exits.

Lifecycle: SHADOW (this is a research strategy; not in the live fleet yet).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from zoneinfo import ZoneInfo

from acme.broker.base import Bar, BracketSpec
from acme.contracts import MES, FuturesContract
from acme.risk import DailyState, EvalProfile, can_open_new_position
from acme.strategies.base import Signal, StrategyMetadata
from acme.strategies.params import ParameterSpec

CT = ZoneInfo("America/Chicago")


def _to_minutes(t: time) -> int:
    return t.hour * 60 + t.minute


@dataclass
class VWAPMomentumConfig:
    # Extension threshold (points) — distance from VWAP that triggers entry.
    entry_threshold_pts: float = 6.0
    # Initial stop offset (points) from entry price.
    initial_stop_pts: float = 8.0
    # Trailing stop offset (points) from the high watermark (long) or low
    # watermark (short). Honored in the backtest; NOT honored in live until
    # the conductor grows a trailing-stop hook (see module docstring).
    trail_distance_pts: float = 6.0
    # Session anchor — VWAP cumulates from this CT time each day.
    session_open_ct: time = field(default_factory=lambda: time(8, 30))
    # Hard close — `wants_force_flat` fires at or after this CT time.
    hard_close_ct: time = field(default_factory=lambda: time(13, 0))
    # Entry window (CT). Entries only fire when session_open <=
    # entry_window_start <= bar < entry_window_end. VWAP still cumulates
    # from session_open so the first entry-eligible bar sees a meaningful
    # VWAP. Exits and force-close run independently regardless of window.
    # Default 09:00-10:00 — per the by-hour breakdown, this hour carries
    # the bulk of the strategy's edge (PF 1.43 vs ~breakeven outside).
    entry_window_start_ct: time = field(default_factory=lambda: time(9, 0))
    entry_window_end_ct: time = field(default_factory=lambda: time(10, 0))
    # Sizing — fixed contracts for clean backtest comparison. PF/WR are
    # size-invariant; net/avg scale linearly. Bump after sweep results.
    fixed_contracts: int = 1
    # Direction gates. Default long-only per the initial spec; flip
    # allow_shorts when the symmetric variant is studied.
    allow_longs: bool = True
    allow_shorts: bool = False
    # Far-target offset (points) — placeholder so BracketSpec is well-formed.
    # Set wide enough that price never reaches it during a session; the real
    # exit comes from trailing stop or the 13:00 force-flat.
    far_target_pts: float = 200.0


class _VWAPState:
    """Cumulative typical-price × volume tracker for one CT calendar date."""
    __slots__ = ("session_date", "cum_tpv", "cum_v")

    def __init__(self, session_date: date) -> None:
        self.session_date = session_date
        self.cum_tpv: float = 0.0
        self.cum_v: float = 0.0

    def update(self, bar: Bar) -> None:
        # Standard typical price; volume weighting matches every commercial
        # VWAP implementation. Skip zero-volume bars (rare; pre-market gaps).
        if bar.v <= 0:
            return
        tp = (bar.h + bar.l + bar.c) / 3.0
        self.cum_tpv += tp * bar.v
        self.cum_v += bar.v

    @property
    def vwap(self) -> float | None:
        return (self.cum_tpv / self.cum_v) if self.cum_v > 0 else None


class _DayState:
    """Per-session entry bookkeeping. One-trade-per-session enforcement."""
    __slots__ = ("session_date", "entered")

    def __init__(self, session_date: date) -> None:
        self.session_date = session_date
        self.entered = False


class VWAPMomentumStrategy:
    name = "vwap_momentum"
    version = "1"
    metadata = StrategyMetadata(
        tier=3,
        # VWAP-momentum thesis aligns with trending intraday regimes; weak in
        # ranging chop where extensions snap back. Volatile is mixed — big
        # extensions but high false-positive rate.
        regime_fit={"trending": 1.0, "volatile": 0.6, "ranging": 0.2, "quiet": 0.4},
        time_buckets=["08:30-13:00 CT"],
        default_lifecycle="SHADOW",
        timeframe_minutes=2,
    )

    @classmethod
    def tunable_params(cls) -> list[ParameterSpec]:
        return [
            ParameterSpec("entry_threshold_pts", float, 6.0, 2.0, 20.0, 0.5,
                          "Points above/below VWAP that trigger entry"),
            ParameterSpec("initial_stop_pts", float, 8.0, 3.0, 15.0, 0.5,
                          "Initial stop distance (points) from entry"),
            ParameterSpec("trail_distance_pts", float, 6.0, 3.0, 20.0, 0.5,
                          "Trailing stop distance (points) from watermark"),
        ]

    def __init__(
        self,
        config: VWAPMomentumConfig | None = None,
        contract: FuturesContract = MES,
    ) -> None:
        self.config = config or VWAPMomentumConfig()
        self.contract = contract
        self.timeframe_minutes = self.metadata.timeframe_minutes
        self._vwap: _VWAPState | None = None
        self._day: _DayState | None = None

        self._session_open_min = _to_minutes(self.config.session_open_ct)
        self._hard_close_min = _to_minutes(self.config.hard_close_ct)
        self._entry_window_start_min = _to_minutes(self.config.entry_window_start_ct)
        self._entry_window_end_min = _to_minutes(self.config.entry_window_end_ct)

        if not (self._session_open_min < self._hard_close_min):
            raise ValueError(
                "VWAPMomentumConfig: session_open_ct must precede hard_close_ct"
            )
        if not (self._session_open_min <= self._entry_window_start_min
                < self._entry_window_end_min <= self._hard_close_min):
            raise ValueError(
                "VWAPMomentumConfig: entry window must lie within "
                "[session_open_ct, hard_close_ct]"
            )

    def required_history_bars(self) -> int:
        # VWAP cumulates intraday from 08:30 CT — no historical bars needed
        # across sessions.
        return 0

    # ───────────────────────── helpers ─────────────────────────

    def _ct_parts(self, bar: Bar) -> tuple[date, int]:
        ct = bar.t.astimezone(CT)
        return ct.date(), ct.hour * 60 + ct.minute

    def _ensure_session_state(self, session_date: date) -> None:
        """Reset VWAP + entry bookkeeping at each new CT calendar date."""
        if self._vwap is None or self._vwap.session_date != session_date:
            self._vwap = _VWAPState(session_date)
        if self._day is None or self._day.session_date != session_date:
            self._day = _DayState(session_date)

    # ───────────────────────── harness hook ─────────────────────

    def wants_force_flat(self, bar: Bar) -> bool:
        """True once the bar's CT minute is at or past the hard-close time.
        The harness flattens any open position at this bar's open price."""
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

        # Outside the entry window (before session open OR at/after hard close)
        # we don't even cumulate VWAP — VWAP only meaningful during the
        # 08:30-13:00 CT session per spec.
        if ct_minute < self._session_open_min or ct_minute >= self._hard_close_min:
            return None

        self._ensure_session_state(ct_date)
        assert self._vwap is not None and self._day is not None

        # Update VWAP with this bar (including the 08:30 bar itself, which
        # anchors the cumulative tally). Done BEFORE the signal check so the
        # 08:30 entry sees a meaningful VWAP (= typical price of bar 1).
        self._vwap.update(bar)

        # In-position or already-traded-this-session: nothing to do. The
        # backtest engine handles exits independently via trailing stop.
        if current_position != 0 or self._day.entered:
            return None

        # Entry-window gate. VWAP keeps cumulating outside this window
        # (so the first entry-eligible bar still has context) but we
        # don't emit signals on early or late bars.
        if not (self._entry_window_start_min <= ct_minute
                < self._entry_window_end_min):
            return None

        vwap = self._vwap.vwap
        if vwap is None:
            return None   # zero-volume bar at session open, vanishingly rare

        # Distance from VWAP (signed; positive = price above VWAP).
        distance = bar.c - vwap
        cfg = self.config

        # Long: price extended above VWAP by at least entry_threshold.
        if cfg.allow_longs and distance >= cfg.entry_threshold_pts:
            return self._build_signal(
                side="buy", entry=bar.c, distance=distance,
                state=state, profile=profile,
                current_position=current_position,
                current_balance_unrealized=current_balance_unrealized,
            )

        # Short: price extended below VWAP by at least entry_threshold. Long-
        # only by default; flip allow_shorts to enable the symmetric variant.
        if cfg.allow_shorts and distance <= -cfg.entry_threshold_pts:
            return self._build_signal(
                side="sell", entry=bar.c, distance=distance,
                state=state, profile=profile,
                current_position=current_position,
                current_balance_unrealized=current_balance_unrealized,
            )

        return None

    # ───────────────────────── signal construction ──────────────

    def _build_signal(
        self, *, side: str, entry: float, distance: float,
        state: DailyState, profile: EvalProfile,
        current_position: int, current_balance_unrealized: float,
    ) -> Signal | None:
        cfg = self.config
        tick = self.contract.tick_size
        stop_ticks = max(1, int(round(cfg.initial_stop_pts / tick)))
        # Far target — placeholder. Real exit is trailing stop or 13:00 close.
        # Live engine will treat this as a target; for the backtest harness
        # the target is ignored in favor of the trailing-stop logic in
        # scripts/backtest_vwap_momentum.py.
        target_ticks = max(1, int(round(cfg.far_target_pts / tick)))

        size = cfg.fixed_contracts
        if size <= 0:
            return None

        allowed, block_reason = can_open_new_position(
            profile, state, current_balance_unrealized,
            self.contract.symbol, size, current_position,
        )
        if not allowed:
            return Signal(side=side, size=0,  # type: ignore[arg-type]
                          reason=f"blocked: {block_reason}")

        # Mark the day as having entered — strictly enforces one-trade-per-
        # session regardless of how the engine reports the close.
        assert self._day is not None
        self._day.entered = True

        direction = "long" if side == "buy" else "short"
        # Encode entry distance (signed, integer hundredths of a point) in
        # the reason for breakdown analysis. Format mirrors gap_fill's
        # `gap_fill_long_g+000400` convention.
        dist_int = int(round(distance * 100))
        return Signal(
            side=side,  # type: ignore[arg-type]
            size=size,
            bracket=BracketSpec(
                stop_loss_offset_ticks=stop_ticks,
                take_profit_offset_ticks=target_ticks,
            ),
            reason=f"vwap_momentum_{direction}_d{dist_int:+07d}",
        )
