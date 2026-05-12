"""PULSE entry-gate stack — Python port of the Pine indicator's "Pro
Filters" and "Risk Management" groups.

Each gate is its own small class so callers (IGNITION, diagnostics,
audits) can use any subset and test each in isolation. The full Pine
gate list is:

  Ported here:
    - VolRegimeClassifier   — ATR / SMA(ATR, 20) → low / normal / high
    - ZoneClassifier        — rolling 60-bar range; flags upper/lower 20%
    - PullbackRiskFilter    — |slope| < threshold OR RVOL < threshold
    - ExhaustionFilter      — N-bar price extension > N * ATR
    - LockoutManager        — consecutive-loss circuit breaker
    - HTFAlignmentEngine    — HTF EMA spread slope + RVOL; caller feeds HTF bars

  Deferred (separate concerns):
    - Market structure (swing-high/low classification) — not blocking IGNITION
    - Key levels (PDH/PDL/session H-L/round numbers) — same data as BOUNDARY
      Phase 5; consolidate there
    - ES correlation — needs a second data feed plumbed in

The IGNITION strategy (Phase 2c) composes pulse_features + the gates it
cares about. Each gate returns a structured result with the boolean
outcome plus the raw values so failures can be logged with reasons.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from acme.broker.base import Bar
from acme.indicators import ATR, EMA, SMA


# ════════════════════════════════════════════════════════════════════
# Vol regime
# ════════════════════════════════════════════════════════════════════


VolRegimeLabel = Literal["low", "normal", "high"]


@dataclass(frozen=True)
class VolRegimeState:
    atr: float
    atr_avg: float
    ratio: float
    label: VolRegimeLabel
    # PULSE quirk: high-vol expands projected move 1.25x; low-vol raises
    # the prob-gate by +0.07. Exposed here so the strategy can apply
    # whichever adjustment it cares about.
    proj_move_multiplier: float
    prob_gate_adjustment: float


class VolRegimeClassifier:
    """ATR ratio classifier from the Pine indicator's Vol Regime block.

    high if ATR / SMA(ATR, atr_avg_period) > high_ratio
            AND ATR >= min_high_atr (absolute floor — prevents 'high'
            classification when expansion is huge in ratio terms but
            tiny in absolute points; the 2026-05-12 overnight session
            showed REGIME whipsawing because thin-market ATR of ~0.5pt
            crossed the 1.5× ratio threshold but stop distance of
            1.5×0.5pt = 3 ticks was too tight to survive any reversion)
    low  if ATR / SMA(ATR, atr_avg_period) < low_ratio
    normal otherwise
    """

    def __init__(
        self, *, atr_period: int = 4, atr_avg_period: int = 20,
        high_ratio: float = 1.5, low_ratio: float = 0.8,
        high_proj_mult: float = 1.25, low_gate_adjust: float = 0.07,
        min_high_atr: float = 0.0,
    ) -> None:
        self._atr = ATR(atr_period)
        self._atr_sma = SMA(atr_avg_period)
        self._high_ratio = high_ratio
        self._low_ratio = low_ratio
        self._high_proj_mult = high_proj_mult
        self._low_gate_adjust = low_gate_adjust
        self._min_high_atr = min_high_atr

    @property
    def is_warm(self) -> bool:
        return self._atr.is_warm and self._atr_sma.is_warm

    def update(self, bar: Bar) -> VolRegimeState | None:
        atr = self._atr.update(bar)
        if atr is None:
            return None
        atr_avg = self._atr_sma.update(atr)
        if atr_avg is None:
            return None
        if atr_avg <= 0:
            return None
        ratio = atr / atr_avg
        label: VolRegimeLabel
        if ratio > self._high_ratio and atr >= self._min_high_atr:
            label = "high"
        elif ratio < self._low_ratio:
            label = "low"
        else:
            label = "normal"
        return VolRegimeState(
            atr=atr, atr_avg=atr_avg, ratio=ratio, label=label,
            proj_move_multiplier=self._high_proj_mult if label == "high" else 1.0,
            prob_gate_adjustment=self._low_gate_adjust if label == "low" else 0.0,
        )


# ════════════════════════════════════════════════════════════════════
# Zone (upper / mid / lower of recent range)
# ════════════════════════════════════════════════════════════════════


ZoneLabel = Literal["upper", "mid", "lower"]


@dataclass(frozen=True)
class ZoneState:
    range_high: float
    range_low: float
    range_pos: float  # 0..1
    label: ZoneLabel
    in_upper: bool
    in_lower: bool
    zone_ok: bool    # entries allowed only in mid (not upper, not lower)


class ZoneClassifier:
    """Rolling-window highest-high / lowest-low → range position.

    Matches Pine's `sessionHigh = ta.highest(high, 60)` and
    `sessionLow = ta.lowest(low, 60)`. The Pine `zoneOK` rule:
    range_pos must be between upper_band and (1 - upper_band) — neither
    too high nor too low — for an entry to qualify.
    """

    def __init__(self, *, window_bars: int = 60, upper_band: float = 0.20) -> None:
        if not 0 < upper_band < 0.5:
            raise ValueError("upper_band must be in (0, 0.5)")
        self._window = window_bars
        self._upper = upper_band
        self._highs: deque[float] = deque(maxlen=window_bars)
        self._lows: deque[float] = deque(maxlen=window_bars)

    @property
    def is_warm(self) -> bool:
        return len(self._highs) >= self._window

    def update(self, bar: Bar) -> ZoneState | None:
        self._highs.append(bar.h)
        self._lows.append(bar.l)
        if not self.is_warm:
            return None
        range_high = max(self._highs)
        range_low = min(self._lows)
        size = max(range_high - range_low, 1e-9)
        pos = (bar.c - range_low) / size
        in_upper = pos > (1.0 - self._upper)
        in_lower = pos < self._upper
        label: ZoneLabel = "upper" if in_upper else ("lower" if in_lower else "mid")
        return ZoneState(
            range_high=range_high, range_low=range_low, range_pos=pos,
            label=label, in_upper=in_upper, in_lower=in_lower,
            zone_ok=not (in_upper or in_lower),
        )


# ════════════════════════════════════════════════════════════════════
# Pullback risk & exhaustion (stateless given features)
# ════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class PullbackRisk:
    slope_too_flat: bool
    rvol_too_low: bool
    flagged: bool

    @classmethod
    def evaluate(
        cls, *, slope: float, rvol: float,
        slope_band: float = 0.02, rvol_band: float = 0.90,
    ) -> "PullbackRisk":
        s = abs(slope) < slope_band
        v = rvol < rvol_band
        return cls(slope_too_flat=s, rvol_too_low=v, flagged=s or v)


@dataclass(frozen=True)
class Exhaustion:
    """N-bar price extension beyond `atr_mult * ATR`. PULSE uses 4 bars
    by default. Returned for both directions; the strategy uses
    `exhausted_up` to block long entries near a top (and `exhausted_down`
    for shorts).
    """
    delta_up: float
    delta_down: float
    exhausted_up: bool
    exhausted_down: bool

    @classmethod
    def evaluate(
        cls, *, current_close: float, lookback_close: float, atr: float,
        atr_mult: float = 1.25,
    ) -> "Exhaustion":
        du = current_close - lookback_close
        dd = lookback_close - current_close
        thr = atr_mult * atr
        return cls(
            delta_up=du, delta_down=dd,
            exhausted_up=du > thr,
            exhausted_down=dd > thr,
        )


class ExhaustionTracker:
    """Rolling close-history wrapper around `Exhaustion.evaluate` so the
    caller doesn't have to manage the close[lookback] deque manually."""

    def __init__(self, *, lookback_bars: int = 4, atr_mult: float = 1.25) -> None:
        self._lookback = lookback_bars
        self._atr_mult = atr_mult
        self._closes: deque[float] = deque(maxlen=lookback_bars + 1)

    @property
    def is_warm(self) -> bool:
        return len(self._closes) > self._lookback

    def update(self, close: float, atr: float) -> Exhaustion | None:
        self._closes.append(close)
        if not self.is_warm:
            return None
        return Exhaustion.evaluate(
            current_close=close, lookback_close=self._closes[0], atr=atr,
            atr_mult=self._atr_mult,
        )


# ════════════════════════════════════════════════════════════════════
# Lockout state machine
# ════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class LockoutState:
    consec_losses: int
    locked_until: datetime | None  # UTC
    is_locked: bool


class LockoutManager:
    """Consec-loss circuit breaker matching Pine's lockout system.

    Caller emits trade outcomes via `record_loss(now)` and `record_win(now)`.
    `is_locked(now)` returns whether new entries should be blocked.

    Defaults from the Pine indicator: 2 consecutive losses → 30 minute
    lockout. Tunable per-strategy.
    """

    def __init__(
        self, *, max_consec_losses: int = 2, lockout_minutes: int = 30,
        enabled: bool = True,
    ) -> None:
        if max_consec_losses < 1:
            raise ValueError("max_consec_losses must be >= 1")
        if lockout_minutes < 0:
            raise ValueError("lockout_minutes must be >= 0")
        self._max_consec = max_consec_losses
        self._lockout = timedelta(minutes=lockout_minutes)
        self._enabled = enabled
        self._consec = 0
        self._locked_until: datetime | None = None

    @property
    def state(self) -> LockoutState:
        return LockoutState(
            consec_losses=self._consec,
            locked_until=self._locked_until,
            is_locked=self._locked_until is not None,
        )

    def record_loss(self, now: datetime) -> LockoutState:
        self._consec += 1
        if self._enabled and self._consec >= self._max_consec:
            self._locked_until = now + self._lockout
        return self.state

    def record_win(self, now: datetime) -> LockoutState:
        self._consec = 0
        self._locked_until = None
        return self.state

    def reset(self) -> LockoutState:
        self._consec = 0
        self._locked_until = None
        return self.state

    def is_locked(self, now: datetime) -> bool:
        if not self._enabled or self._locked_until is None:
            return False
        if now >= self._locked_until:
            # Lockout expired — clear and resume eligibility
            self._locked_until = None
            self._consec = 0
            return False
        return True


# ════════════════════════════════════════════════════════════════════
# HTF alignment
# ════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class HTFState:
    htf_ema_fast: float
    htf_ema_slow: float
    htf_spread: float
    htf_slope: float
    htf_rvol: float
    htf_bullish: bool
    htf_bearish: bool


class HTFAlignmentEngine:
    """Higher-timeframe alignment from the PULSE Pro Filters block.

    The Pine version uses `request.security` to pull HTF bars from
    TradingView. Here we treat the HTF stream as caller-supplied:
    `update_htf(bar)` is called when a new HTF bar closes
    (e.g. when a 5-min bar finalises). The latest HTFState applies
    to all LTF bars until the next HTF bar arrives.

    `aligned(p_long, p_short)` returns whether the LTF probability
    direction agrees with the HTF regime.
    """

    def __init__(
        self, *, ema_fast: int = 9, ema_slow: int = 14, vol_ma: int = 20,
        rvol_floor: float = 0.8,
    ) -> None:
        self._ema_fast = EMA(ema_fast)
        self._ema_slow = EMA(ema_slow)
        self._vol_sma = SMA(vol_ma)
        self._rvol_floor = rvol_floor
        self._prev_spread: float | None = None
        self._latest: HTFState | None = None

    @property
    def is_warm(self) -> bool:
        return self._latest is not None

    @property
    def latest(self) -> HTFState | None:
        return self._latest

    def update_htf(self, bar: Bar) -> HTFState | None:
        ef = self._ema_fast.update(bar.c)
        es = self._ema_slow.update(bar.c)
        vma = self._vol_sma.update(float(bar.v))
        if ef is None or es is None or vma is None:
            return None
        spread = ef - es
        slope = 0.0 if self._prev_spread is None else (spread - self._prev_spread)
        self._prev_spread = spread
        rvol = (float(bar.v) / vma) if (vma > 0 and bar.v > 0) else 1.0
        bullish = slope > 0 and rvol > self._rvol_floor
        bearish = slope < 0 and rvol > self._rvol_floor
        state = HTFState(
            htf_ema_fast=ef, htf_ema_slow=es, htf_spread=spread,
            htf_slope=slope, htf_rvol=rvol,
            htf_bullish=bullish, htf_bearish=bearish,
        )
        self._latest = state
        return state

    def aligned(self, *, p_long: float, p_short: float) -> bool:
        """True if no HTF data yet (permissive) or if LTF probability
        direction agrees with HTF regime."""
        if self._latest is None:
            return True
        if p_long > p_short and self._latest.htf_bullish:
            return True
        if p_short > p_long and self._latest.htf_bearish:
            return True
        return False
