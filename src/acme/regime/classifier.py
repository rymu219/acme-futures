"""Regime classification — turns indicator readings into a regime label.

Priority-ordered rules (per the engineering brief):
  1. CHAOTIC      news_blackout OR atr_ratio > 1.8
  2. COMPRESSING  bb_width_pct < 0.20 AND atr_ratio < 0.85
  3. TRENDING     adx > 25 AND atr_ratio > 0.9 AND hurst > 0.50
  4. RANGING      adx < 22 AND hurst < 0.52 AND atr_ratio < 1.3 AND not compressing
  5. AMBIGUOUS    none of the above
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from acme.broker.base import Bar
from acme.indicators import ADX, ATR, SMA
from acme.regime.indicators import BBWidthPercentile, Hurst

Regime = Literal["trending", "ranging", "compressing", "chaotic", "ambiguous"]
Direction = Literal["long_bias", "short_bias", "neutral"]
ADXDirection = Literal["rising", "falling", "flat"]


@dataclass(frozen=True)
class RegimeSnapshot:
    ts: datetime
    timeframe: str               # '5m'
    regime: Regime
    direction: Direction
    confidence: float            # 0..1
    adx: float | None
    adx_direction: ADXDirection
    atr_current: float | None
    atr_ratio: float | None
    bb_width: float | None
    bb_width_pct: float | None
    hurst: float | None
    volume_ratio: float | None
    momentum_score: float | None
    reasoning: list[str] = field(default_factory=list)

    def to_db_row(self) -> dict:
        return {
            "ts": self.ts.isoformat(),
            "timeframe": self.timeframe,
            "adx": self.adx,
            "adx_direction": self.adx_direction,
            "atr_current": self.atr_current,
            "atr_ratio": self.atr_ratio,
            "bb_width": self.bb_width,
            "bb_width_pct": self.bb_width_pct,
            "hurst": self.hurst,
            "volume_ratio": self.volume_ratio,
            "momentum_score": self.momentum_score,
            "regime": self.regime,
            "regime_direction": self.direction,
            "confidence": self.confidence,
            "raw_signals": {"reasoning": self.reasoning},
        }


def _scale(x: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def classify_regime(
    *,
    ts: datetime,
    timeframe: str,
    adx: float | None,
    adx_direction: ADXDirection,
    atr_current: float | None,
    atr_ratio: float | None,
    bb_width: float | None,
    bb_width_pct: float | None,
    hurst: float | None,
    volume_ratio: float | None,
    momentum_score: float | None,
    news_blackout: bool,
) -> RegimeSnapshot:
    reasoning: list[str] = []

    # If any of the core indicators are missing (warmup), we can't classify cleanly.
    core_warm = all(v is not None for v in (adx, atr_ratio, hurst, bb_width_pct))
    if not core_warm:
        reasoning.append("indicators_warming_up")
        return _snapshot(ts, timeframe, "ambiguous", "neutral", 0.2,
                         adx, adx_direction, atr_current, atr_ratio,
                         bb_width, bb_width_pct, hurst, volume_ratio,
                         momentum_score, reasoning)

    # 1. CHAOTIC
    if news_blackout:
        reasoning.append("news_blackout")
        return _snapshot(ts, timeframe, "chaotic", "neutral", 1.0,
                         adx, adx_direction, atr_current, atr_ratio,
                         bb_width, bb_width_pct, hurst, volume_ratio,
                         momentum_score, reasoning)
    if atr_ratio is not None and atr_ratio > 1.8:
        reasoning.append(f"atr_ratio={atr_ratio:.2f} > 1.8")
        return _snapshot(ts, timeframe, "chaotic", "neutral", 1.0,
                         adx, adx_direction, atr_current, atr_ratio,
                         bb_width, bb_width_pct, hurst, volume_ratio,
                         momentum_score, reasoning)

    # 2. COMPRESSING
    if (bb_width_pct is not None and bb_width_pct < 0.20
            and atr_ratio is not None and atr_ratio < 0.85):
        # confidence inversely scaled on bb_width_pct (0.0 → 1.0 → confidence 1.0)
        conf = 1.0 - bb_width_pct  # in [0.8, 1.0]
        reasoning.append(f"bb_width_pct={bb_width_pct:.2f} < 0.20 AND atr_ratio={atr_ratio:.2f} < 0.85")
        return _snapshot(ts, timeframe, "compressing", "neutral", conf,
                         adx, adx_direction, atr_current, atr_ratio,
                         bb_width, bb_width_pct, hurst, volume_ratio,
                         momentum_score, reasoning)

    # 3. TRENDING
    if (adx is not None and adx > 25
            and atr_ratio is not None and atr_ratio > 0.9
            and hurst is not None and hurst > 0.50):
        # base confidence scales adx 25..40 → 0.5..1.0
        base = 0.5 + 0.5 * _scale(adx, 25.0, 40.0)
        # hurst weighting: 0.50→1.0 maps to 1.0→1.2
        hurst_weight = 1.0 + 2.0 * max(0.0, hurst - 0.50)
        conf = min(1.0, base * hurst_weight)
        # adjustments
        if adx_direction == "rising":
            conf = min(1.0, conf + 0.10)
            reasoning.append("adx_rising:+0.10")
        if volume_ratio is not None:
            if volume_ratio > 1.3:
                conf = min(1.0, conf + 0.05)
                reasoning.append(f"volume_ratio={volume_ratio:.2f} > 1.3:+0.05")
            elif volume_ratio < 0.7:
                conf = max(0.0, conf - 0.10)
                reasoning.append(f"volume_ratio={volume_ratio:.2f} < 0.7:-0.10")

        if momentum_score is not None and momentum_score > 0.55:
            direction: Direction = "long_bias"
        elif momentum_score is not None and momentum_score < 0.45:
            direction = "short_bias"
        else:
            direction = "neutral"
        reasoning.append(f"adx={adx:.1f} > 25, atr_ratio={atr_ratio:.2f} > 0.9, hurst={hurst:.2f} > 0.50")
        return _snapshot(ts, timeframe, "trending", direction, conf,
                         adx, adx_direction, atr_current, atr_ratio,
                         bb_width, bb_width_pct, hurst, volume_ratio,
                         momentum_score, reasoning)

    # 4. RANGING
    if (adx is not None and adx < 22
            and hurst is not None and hurst < 0.52
            and atr_ratio is not None and atr_ratio < 1.3):
        # confidence: low ADX is good; penalize 15-22 borderline
        # 22→0.5, 15→0.9
        conf = 0.9 if adx < 15 else 0.9 - 0.4 * _scale(adx, 15.0, 22.0)
        if adx_direction == "falling":
            conf = min(1.0, conf + 0.10)
            reasoning.append("adx_falling:+0.10")
        reasoning.append(f"adx={adx:.1f} < 22, hurst={hurst:.2f} < 0.52, atr_ratio={atr_ratio:.2f} < 1.3")
        return _snapshot(ts, timeframe, "ranging", "neutral", conf,
                         adx, adx_direction, atr_current, atr_ratio,
                         bb_width, bb_width_pct, hurst, volume_ratio,
                         momentum_score, reasoning)

    # 5. AMBIGUOUS
    reasoning.append("no rule cleanly matched")
    return _snapshot(ts, timeframe, "ambiguous", "neutral", 0.3,
                     adx, adx_direction, atr_current, atr_ratio,
                     bb_width, bb_width_pct, hurst, volume_ratio,
                     momentum_score, reasoning)


def _snapshot(ts, timeframe, regime, direction, confidence,
              adx, adx_direction, atr_current, atr_ratio,
              bb_width, bb_width_pct, hurst, volume_ratio, momentum_score,
              reasoning) -> RegimeSnapshot:
    return RegimeSnapshot(
        ts=ts, timeframe=timeframe, regime=regime,
        direction=direction, confidence=round(confidence, 4),
        adx=adx, adx_direction=adx_direction,
        atr_current=atr_current, atr_ratio=atr_ratio,
        bb_width=bb_width, bb_width_pct=bb_width_pct,
        hurst=hurst, volume_ratio=volume_ratio,
        momentum_score=momentum_score,
        reasoning=reasoning,
    )


# ---------- streaming engine ----------

class RegimeEngine:
    """Stateful per-bar regime classifier. Feed bars in chronological order.

    Used both online (live conductor) and offline (backfill). The engine doesn't
    know about Supabase — it just emits snapshots. Persistence is the caller's job.
    """

    def __init__(
        self,
        timeframe_minutes: int = 5,
        atr_period: int = 14,
        atr_ratio_lookback: int = 20,
        adx_period: int = 14,
        bb_period: int = 20,
        bb_lookback: int = 50,
        hurst_window: int = 100,
        volume_period: int = 20,
        momentum_period: int = 14,
    ) -> None:
        self.timeframe_minutes = timeframe_minutes
        self.timeframe_label = f"{timeframe_minutes}m"
        self._atr = ATR(atr_period)
        self._atr_history: deque[float] = deque(maxlen=atr_ratio_lookback)
        self._adx = ADX(adx_period)
        self._adx_prev: float | None = None
        self._bbwp = BBWidthPercentile(period=bb_period, lookback=bb_lookback)
        self._hurst = Hurst(window=hurst_window)
        self._vol_sma = SMA(volume_period)
        self._momentum_period = momentum_period
        self._highs: deque[float] = deque(maxlen=momentum_period)
        self._lows: deque[float] = deque(maxlen=momentum_period)

    def on_bar(self, bar: Bar, *, news_blackout: bool = False) -> RegimeSnapshot:
        # ATR + history
        atr_val = self._atr.update(bar)
        if atr_val is not None:
            self._atr_history.append(atr_val)
        atr_ratio: float | None = None
        if len(self._atr_history) == self._atr_history.maxlen and atr_val is not None:
            mean_atr = sum(self._atr_history) / len(self._atr_history)
            if mean_atr > 0:
                atr_ratio = atr_val / mean_atr

        # ADX + direction
        adx_val = self._adx.update(bar)
        adx_dir: ADXDirection = "flat"
        if adx_val is not None and self._adx_prev is not None:
            delta = adx_val - self._adx_prev
            if delta > 0.5:
                adx_dir = "rising"
            elif delta < -0.5:
                adx_dir = "falling"
        if adx_val is not None:
            self._adx_prev = adx_val

        # BB width + percentile
        bbwp_out = self._bbwp.update(bar.c)
        bb_width = bbwp_out.width if bbwp_out else None
        bb_width_pct = bbwp_out.percentile if bbwp_out else None

        # Hurst
        hurst_val = self._hurst.update(bar.c)

        # Volume ratio
        vol_avg = self._vol_sma.update(float(bar.v))
        volume_ratio: float | None = None
        if vol_avg and vol_avg > 0:
            volume_ratio = bar.v / vol_avg

        # Momentum score: close position within last-N high/low range
        self._highs.append(bar.h)
        self._lows.append(bar.l)
        momentum_score: float | None = None
        if len(self._highs) == self._momentum_period:
            hi = max(self._highs)
            lo = min(self._lows)
            if hi > lo:
                momentum_score = (bar.c - lo) / (hi - lo)

        return classify_regime(
            ts=bar.t, timeframe=self.timeframe_label,
            adx=adx_val, adx_direction=adx_dir,
            atr_current=atr_val, atr_ratio=atr_ratio,
            bb_width=bb_width, bb_width_pct=bb_width_pct,
            hurst=hurst_val, volume_ratio=volume_ratio,
            momentum_score=momentum_score,
            news_blackout=news_blackout,
        )
