"""CONFLUENCE — multi-level zone hold in a trending session.

Thesis: when two or more independent institutional reference levels stack
within a tight band, that zone is high-conviction support/resistance.
Price testing the zone from the trend side and holding (next bar closes
back in the trend direction) gives high-probability continuation.

Levels tracked:
  - VWAP             — cumulative from 08:30 CT each session
  - Settlement       — prior RTH close (close of the bar ending 15:00 CT)
  - PDH / PDL        — prior day RTH high/low (from DayLevels)
  - ONH / ONL        — overnight Globex high/low (from DayLevels)

(VAH / VAL aren't computed in the codebase yet; the spec said skip if
unavailable. Easy to add later by extending `_collect_levels` once
value-area numbers exist in DayLevels.)

Signal logic:
  1. Find all confluence zones — groups of >= min_levels_in_zone levels
     within `confluence_ticks` of each other (`max - min <= band_width`).
  2. Trend filter: EMA(`ema_period`) slope over the last `slope_lookback`
     EMA values must be positive (longs) or negative (shorts).
  3. Two-bar touch-and-hold pattern. Bar N "touches" the zone from the
     trend side; bar N+1 "confirms" by closing back outside the zone:
       LONG  (uptrend):
         Bar N: bar.h >= zone.ceiling AND bar.l <= zone.ceiling
                (range crosses the ceiling — came from above)
         Bar N+1: bar.c >= zone.ceiling (closed back above)
       SHORT (downtrend):
         Bar N: bar.l <= zone.floor AND bar.h >= zone.floor
                (range crosses the floor — came from below)
         Bar N+1: bar.c <= zone.floor (closed back below)
  4. Entry at bar N+1's close.
  5. Initial stop: `stop_ticks` below zone.floor (long) or above
     zone.ceiling (short).
  6. Trailing stop: `trail_points` from high watermark (long) or low
     watermark (short). Backtest handles trailing; live engine would
     need a conductor hook (same caveat as VWAP_MOMENTUM).
  7. Hard close at `hard_close_ct` (default 15:00 CT — wider than
     VWAP_MOMENTUM's 13:00 because confluence setups develop later).
  8. One trade per direction per session — long and short are
     independent; a session can fire both.

Fleet coordination (live only — backtest tests isolation):
  - 08:30-13:00 CT: suppressed if GAP_FILL has open position.
  - 09:00-10:00 CT: suppressed if VWAP_MOMENTUM has open position.
  - Force flat by 16:59 CT to clear before OVERNIGHT_DRIFT entry.
The Conductor enforces these via `current_position` (the position-state
gate already lives in `on_bar`); the inter-strategy "suppress until
those strategies flat" rule needs a new arbitration hook similar to
boundary's `fleet_coordination_close_times_ct`. That's downstream of
the backtest result.

Lifecycle: SHADOW.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from zoneinfo import ZoneInfo

from acme.broker.base import Bar, BracketSpec
from acme.contracts import MES, FuturesContract
from acme.indicators import EMA
from acme.levels import DayLevels
from acme.risk import DailyState, EvalProfile, can_open_new_position
from acme.strategies.base import Signal, StrategyMetadata
from acme.strategies.params import ParameterSpec

CT = ZoneInfo("America/Chicago")


def _to_minutes(t: time) -> int:
    return t.hour * 60 + t.minute


@dataclass
class ConfluenceConfig:
    # Cluster width — levels within this many MES ticks of each other form
    # a zone. Default 6 ticks = 1.5 points.
    confluence_ticks: int = 6
    # Trend filter
    ema_period: int = 20
    slope_lookback: int = 10
    # Stop placement
    stop_ticks: int = 8                                  # 2pt below floor (long)
    trail_points: float = 6.0                            # from watermark
    # Session timing
    session_open_ct: time = field(default_factory=lambda: time(9, 0))
    hard_close_ct: time = field(default_factory=lambda: time(15, 0))
    # Cluster threshold
    min_levels_in_zone: int = 2
    # Sizing — fixed for clean sweep comparison
    fixed_contracts: int = 1
    # Direction gates
    allow_longs: bool = True
    allow_shorts: bool = True
    # Far-target placeholder for the BracketSpec (live engine consumes it
    # as a fixed target; backtest ignores it in favor of trailing-stop).
    far_target_pts: float = 200.0
    # Live-only fleet-coordination flatten time. Backtest uses
    # `hard_close_ct` only; the 16:59 force-flat is for production where
    # OVERNIGHT_DRIFT takes over at 17:00 CT.
    fleet_coord_flatten_ct: time = field(default_factory=lambda: time(16, 59))


# ───────────────────────── confluence detection ──────────────────────


def _collect_levels(
    levels: DayLevels | None,
    vwap: float | None,
    settlement: float | None,
) -> list[tuple[str, float]]:
    """Build the (name, price) list for confluence detection. Skips any
    level that's None. Names mirror the breakdown column labels so the
    reason field encodes them directly."""
    out: list[tuple[str, float]] = []
    if vwap is not None:
        out.append(("VWAP", vwap))
    if settlement is not None:
        out.append(("SETT", settlement))
    if levels is not None:
        if levels.pdh is not None: out.append(("PDH", levels.pdh))
        if levels.pdl is not None: out.append(("PDL", levels.pdl))
        if levels.onh is not None: out.append(("ONH", levels.onh))
        if levels.onl is not None: out.append(("ONL", levels.onl))
    return out


def _find_zones(
    levels: list[tuple[str, float]], band_width: float, min_members: int,
) -> list[dict]:
    """Find non-redundant level clusters. A cluster's spread (max - min
    price) must be <= band_width and it must contain >= min_members.

    Algorithm: sort by price, then for each starting index i, extend the
    cluster while the spread stays <= band_width. Skip the cluster if
    it's strictly contained in the previous one (avoids reporting nested
    subsets like {VWAP,PDH} when {VWAP,PDH,ONH} already qualified).
    """
    if len(levels) < min_members:
        return []
    sorted_levels = sorted(levels, key=lambda x: x[1])
    zones: list[dict] = []
    last_end = -1
    for i in range(len(sorted_levels)):
        j = i
        while (j + 1 < len(sorted_levels)
               and sorted_levels[j + 1][1] - sorted_levels[i][1] <= band_width):
            j += 1
        size = j - i + 1
        if size < min_members:
            continue
        # If this cluster ends at or before the prior cluster's end, it's
        # a strict subset; skip. (Non-subset overlaps where the new
        # cluster extends past the previous end are kept.)
        if j <= last_end:
            continue
        members = sorted_levels[i:j + 1]
        prices = [p for _, p in members]
        zones.append({
            "names":   tuple(n for n, _ in members),
            "prices":  prices,
            "floor":   min(prices),
            "ceiling": max(prices),
            "center":  sum(prices) / len(prices),
        })
        last_end = j
    return zones


def _nearest_zone(zones: list[dict], price: float) -> dict | None:
    """The zone whose center is closest to `price`. None if no zones."""
    if not zones:
        return None
    return min(zones, key=lambda z: abs(z["center"] - price))


# ───────────────────────── per-session state ─────────────────────────


class _SessionState:
    """Per CT-calendar-date state."""
    __slots__ = (
        "session_date", "cum_tpv", "cum_v", "ema", "ema_history",
        "long_entered", "short_entered",
        "pending_long_zone", "pending_short_zone",
    )

    def __init__(self, session_date: date, ema_period: int, slope_lookback: int) -> None:
        self.session_date = session_date
        self.cum_tpv: float = 0.0
        self.cum_v: float = 0.0
        self.ema = EMA(period=ema_period)
        # +1 to hold the value `slope_lookback` bars ago alongside current
        self.ema_history: deque[float] = deque(maxlen=slope_lookback + 1)
        self.long_entered = False
        self.short_entered = False
        # Pending touch from prior bar; consumed on the NEXT bar (either
        # confirms and fires entry, or fails and is discarded).
        self.pending_long_zone: dict | None = None
        self.pending_short_zone: dict | None = None


# ───────────────────────── strategy class ────────────────────────────


class ConfluenceStrategy:
    name = "confluence"
    version = "1"
    metadata = StrategyMetadata(
        tier=3,
        regime_fit={"trending": 1.0, "volatile": 0.5, "ranging": 0.3, "quiet": 0.4},
        time_buckets=["09:00-15:00 CT"],
        default_lifecycle="SHADOW",
        timeframe_minutes=2,
    )

    @classmethod
    def tunable_params(cls) -> list[ParameterSpec]:
        return [
            ParameterSpec("confluence_ticks", int, 6, 2, 20, 1,
                          "Cluster width (MES ticks)"),
            ParameterSpec("trail_points", float, 6.0, 2.0, 20.0, 0.5,
                          "Trailing stop distance from watermark (points)"),
            ParameterSpec("stop_ticks", int, 8, 2, 20, 1,
                          "Initial stop offset from zone edge (ticks)"),
            ParameterSpec("ema_period", int, 20, 5, 100, 1,
                          "EMA period for trend filter"),
            ParameterSpec("slope_lookback", int, 10, 3, 30, 1,
                          "Bars-ago EMA value used for slope sign"),
        ]

    def __init__(
        self,
        config: ConfluenceConfig | None = None,
        contract: FuturesContract = MES,
    ) -> None:
        self.config = config or ConfluenceConfig()
        self.contract = contract
        self.timeframe_minutes = self.metadata.timeframe_minutes

        self._session_open_min = _to_minutes(self.config.session_open_ct)
        self._hard_close_min = _to_minutes(self.config.hard_close_ct)
        # RTH closes at 15:00 CT — the bar starting at 14:58 (2-min cadence)
        # closes at 15:00 and gives us the "RTH close print" used as next
        # day's settlement. Same pattern gap_fill uses for the 15:58 bar.
        self._settlement_capture_min = _to_minutes(time(14, 58))

        self._levels: DayLevels | None = None
        # Prior RTH closes keyed by CT calendar date. Looked up at next day.
        self._settlements: dict[date, float] = {}
        self._session: _SessionState | None = None

    def required_history_bars(self) -> int:
        # EMA warmup + slope lookback. Cumulative VWAP needs no history
        # across sessions (resets each day).
        return self.config.ema_period + self.config.slope_lookback

    def set_levels(self, levels: DayLevels) -> None:
        """Caller updates day-levels at each trading-date rollover. Same
        contract as BoundaryStrategy.set_levels."""
        self._levels = levels

    # ───────────────────────── helpers ─────────────────────────

    def _ct_parts(self, bar: Bar) -> tuple[date, int]:
        ct = bar.t.astimezone(CT)
        return ct.date(), ct.hour * 60 + ct.minute

    def _ensure_session(self, session_date: date) -> _SessionState:
        if self._session is None or self._session.session_date != session_date:
            self._session = _SessionState(
                session_date, self.config.ema_period, self.config.slope_lookback,
            )
        return self._session

    def _lookup_settlement(self, session_date: date) -> float | None:
        """Walk back up to 7 days for the most recent recorded settlement
        (handles weekends and holidays)."""
        for delta in range(1, 8):
            d = session_date - timedelta(days=delta)
            if d in self._settlements:
                return self._settlements[d]
        return None

    def _ema_slope(self, sess: _SessionState) -> float | None:
        """Slope = (EMA_now - EMA_lookback) / lookback. Returns None until
        the history deque is full."""
        if len(sess.ema_history) < sess.ema_history.maxlen:
            return None
        return (sess.ema_history[-1] - sess.ema_history[0]) / self.config.slope_lookback

    # ───────────────────────── harness hook ─────────────────────

    def wants_force_flat(self, bar: Bar) -> bool:
        """Backtest flatten = hard_close_ct. Live flatten = 16:59 to clear
        before OVERNIGHT_DRIFT at 17:00 CT (fleet coordination). The
        backtest harness only ever runs through hard_close_ct anyway, so
        the 16:59 check is effectively live-only — kept here for live
        when this strategy is wired into the conductor."""
        _, minute = self._ct_parts(bar)
        if minute >= self._hard_close_min:
            return True
        if minute >= _to_minutes(self.config.fleet_coord_flatten_ct):
            return True
        return False

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
        sess = self._ensure_session(ct_date)

        # ── Capture settlement (close of the bar that finishes RTH at
        # 15:00 CT). Done before the time gate so we record it even on
        # sessions where on_bar later short-circuits.
        if ct_minute == self._settlement_capture_min:
            self._settlements[ct_date] = bar.c

        # ── Session time gate. Pre-09:00 we still cumulate VWAP from
        # 08:30 onward (consistent with the strategy's intraday VWAP),
        # but we don't act on signals.
        if ct_minute < _to_minutes(time(8, 30)) or ct_minute >= self._hard_close_min:
            return None

        # ── VWAP / EMA updates (always when in session).
        if bar.v > 0:
            tp = (bar.h + bar.l + bar.c) / 3.0
            sess.cum_tpv += tp * bar.v
            sess.cum_v += bar.v
        vwap = (sess.cum_tpv / sess.cum_v) if sess.cum_v > 0 else None
        ema_val = sess.ema.update(bar.c)
        if ema_val is not None:
            sess.ema_history.append(ema_val)

        # Acting window — entries only fire after 09:00.
        if ct_minute < self._session_open_min:
            return None

        # ── In-position or both-directions entered: nothing to evaluate.
        if current_position != 0:
            return None
        if sess.long_entered and sess.short_entered:
            return None

        slope = self._ema_slope(sess)
        if slope is None:
            return None   # EMA / slope not warm yet

        settlement = self._lookup_settlement(ct_date)
        levels = _collect_levels(self._levels, vwap, settlement)
        band_width = self.config.confluence_ticks * self.contract.tick_size
        zones = _find_zones(levels, band_width, self.config.min_levels_in_zone)

        # ── Confirmation pass: if the PRIOR bar set a pending touch and
        # this bar closes back outside the zone in the trend direction,
        # fire entry. Done before new-touch detection so a single bar
        # can both confirm an old setup and stage a new one (rare but
        # possible).
        if sess.pending_long_zone is not None and self.config.allow_longs:
            z = sess.pending_long_zone
            sess.pending_long_zone = None   # one-shot
            if not sess.long_entered and bar.c >= z["ceiling"]:
                return self._build_signal(
                    side="buy", zone=z, n_levels=len(z["names"]),
                    state=state, profile=profile,
                    current_position=current_position,
                    current_balance_unrealized=current_balance_unrealized,
                    bar_close=bar.c,
                )

        if sess.pending_short_zone is not None and self.config.allow_shorts:
            z = sess.pending_short_zone
            sess.pending_short_zone = None
            if not sess.short_entered and bar.c <= z["floor"]:
                return self._build_signal(
                    side="sell", zone=z, n_levels=len(z["names"]),
                    state=state, profile=profile,
                    current_position=current_position,
                    current_balance_unrealized=current_balance_unrealized,
                    bar_close=bar.c,
                )

        # ── New-touch detection: stage the closest zone for next-bar
        # confirmation if the bar straddles the zone edge from the trend
        # side. Two parallel pendings — long and short — because the
        # nearest zone may differ for the two directions (unlikely with
        # a single _nearest_zone result, but the staging is direction-
        # specific so we keep the slots separate for clarity).
        if zones:
            z = _nearest_zone(zones, bar.c)
            if z is not None:
                # Long candidate: uptrend + bar straddles ceiling from above
                if (self.config.allow_longs and not sess.long_entered
                        and slope > 0
                        and bar.h >= z["ceiling"] and bar.l <= z["ceiling"]):
                    sess.pending_long_zone = z
                # Short candidate: downtrend + bar straddles floor from below
                if (self.config.allow_shorts and not sess.short_entered
                        and slope < 0
                        and bar.l <= z["floor"] and bar.h >= z["floor"]):
                    sess.pending_short_zone = z

        return None

    # ───────────────────────── signal construction ──────────────

    def _build_signal(
        self, *, side: str, zone: dict, n_levels: int,
        state: DailyState, profile: EvalProfile,
        current_position: int, current_balance_unrealized: float,
        bar_close: float,
    ) -> Signal | None:
        cfg = self.config
        tick = self.contract.tick_size
        # Initial stop: stop_ticks below zone.floor for long; above
        # zone.ceiling for short. Computed in points, converted to the
        # tick-rounded offset the BracketSpec wants.
        if side == "buy":
            stop_distance = (bar_close - zone["floor"]) + cfg.stop_ticks * tick
        else:
            stop_distance = (zone["ceiling"] - bar_close) + cfg.stop_ticks * tick
        stop_ticks = max(1, int(round(stop_distance / tick)))
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

        # Mark the direction as entered (one-trade-per-direction-per-session).
        assert self._session is not None
        if side == "buy":
            self._session.long_entered = True
        else:
            self._session.short_entered = True

        direction = "long" if side == "buy" else "short"
        # Reason encodes n_levels + level names (sorted, joined with +)
        # so the breakdown can split by confluence type without re-deriving.
        # Example: confluence_long_n2_PDH+VWAP / confluence_short_n3_ONL+PDL+SETT
        names_part = "+".join(sorted(zone["names"]))
        return Signal(
            side=side,  # type: ignore[arg-type]
            size=size,
            bracket=BracketSpec(
                stop_loss_offset_ticks=stop_ticks,
                take_profit_offset_ticks=target_ticks,
            ),
            reason=f"confluence_{direction}_n{n_levels}_{names_part}",
        )
