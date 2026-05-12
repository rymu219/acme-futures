"""PULSE 4-Bar PRO core feature engine — Python port of the Pine math.

Faithful port of the *core scoring engine* from "PULSE 4-Bar PRO PACK v1.1
[MES]" Pine indicator. This module handles ONLY:

  - EMA-fast / EMA-slow separation and its 4-bar weighted slope
  - RVOL (current vol / SMA of vol)
  - Combined edge score → logistic probability
  - Projected-move estimate

It does NOT handle the indicator's entry gates (HTF alignment, market
structure, key levels, vol regime, ES correlation, session windows,
zones, lockout). Those are stacked on top in Phase 2b — see
[`docs/part_2_plan.md`](../../../docs/part_2_plan.md) — because every
gate is a separate concern that benefits from being individually
testable.

Reference: see the Pine source the user pasted; the inputs default
to its `Core Settings` and `Model Weights` defaults.

The math (single-bar update from the Pine indicator):

    emaF      = EMA(close, emaFast)
    emaS      = EMA(close, emaSlow)
    emaSep    = emaF - emaS
    slope     = change(emaSep)                        # this bar's slope
    rvol      = volume / SMA(volume, volMa)

    w0, w1, w2, w3 = 1, decay**1, decay**2, decay**3  # default decay=0.6
    wsum = w0 + w1 + w2 + w3
    mom_w = sum(sign(slope[k]) * w_k for k in 0..3) / wsum   # direction
    mag_w = sum(slope[k]      * w_k for k in 0..3) / wsum    # magnitude

    tanh_mag = tanh(clip(mag_w, -5, +5))
    rawScore = w_mom*mom_w + w_mag*tanh_mag + w_rvol*(rvol - 1)
    score    = clip(rawScore, -scoreClip, +scoreClip)        # default ±3

    pLong  = 1 / (1 + exp(-2*score))                          # logistic
    pShort = 1 - pLong
    edge   = |pLong - 0.5| * 2                                # 0..1

    projPts = max(ATR(atrLen) * (rvol / rvolBase), minRangePts)
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from acme.broker.base import Bar
from acme.indicators import ATR, EMA, SMA


@dataclass(frozen=True)
class PulseFeatureConfig:
    """Defaults match the PULSE Pine indicator's `Core Settings` and
    `Model Weights` groups."""
    # Core
    atr_len: int = 4
    ema_fast: int = 9
    ema_slow: int = 14
    vol_ma: int = 20
    # Model
    decay: float = 0.60
    w_mom: float = 0.60
    w_mag: float = 0.20
    w_rvol: float = 0.20
    rvol_base: float = 1.50
    score_clip: float = 3.0
    min_range_pts: float = 0.50

    def __post_init__(self) -> None:
        if not 0.1 <= self.decay <= 0.99:
            raise ValueError(f"decay must be in [0.1, 0.99], got {self.decay}")
        if self.score_clip <= 0:
            raise ValueError("score_clip must be > 0")
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast must be < ema_slow")


@dataclass(frozen=True)
class PulseFeatures:
    """One bar's worth of PULSE features. All fields are populated only
    when the underlying indicators are warm; before then `update()`
    returns None.
    """
    ema_fast: float
    ema_slow: float
    ema_sep: float
    slope: float
    atr: float
    rvol: float
    mom_w: float          # 4-bar decayed sign of slope (-1..+1)
    mag_w: float          # 4-bar decayed slope magnitude
    tanh_mag: float       # tanh-normalised magnitude (-1..+1)
    raw_score: float      # weighted combination before clipping
    score: float          # clipped score (-score_clip..+score_clip)
    p_long: float         # 0..1
    p_short: float        # 0..1
    edge: float           # |p_long - 0.5| * 2, 0..1
    proj_pts: float       # projected move (always >= min_range_pts)


class PulseFeatureEngine:
    """Stateful 4-bar PULSE feature engine.

    Usage:
        eng = PulseFeatureEngine()
        for bar in bars:
            feat = eng.update(bar)
            if feat is None:
                continue           # warming up
            ...

    `update()` returns None until the slowest indicator (vol_ma SMA on
    volume) has seen at least `vol_ma` bars AND the 4-bar slope history
    has filled (4 bars after the EMAs are warm).
    """

    def __init__(self, config: PulseFeatureConfig | None = None) -> None:
        self.config = config or PulseFeatureConfig()
        cfg = self.config
        self._ema_fast = EMA(cfg.ema_fast)
        self._ema_slow = EMA(cfg.ema_slow)
        self._vol_sma = SMA(cfg.vol_ma)
        self._atr = ATR(cfg.atr_len)
        # 4-bar slope history (newest at index 0)
        self._slopes: deque[float] = deque(maxlen=4)
        self._prev_sep: float | None = None
        # Precomputed weights (Pine: w0=1, w1=decay, w2=decay^2, w3=decay^3)
        self._w = [1.0, cfg.decay, cfg.decay ** 2, cfg.decay ** 3]
        self._wsum = sum(self._w)

    # ---- properties --------------------------------------------------

    @property
    def is_warm(self) -> bool:
        """True when every underlying indicator has warmed AND there
        are 4 slope samples in history."""
        return (
            self._ema_fast.is_warm
            and self._ema_slow.is_warm
            and self._vol_sma.is_warm
            and self._atr.is_warm
            and len(self._slopes) == 4
        )

    def required_history_bars(self) -> int:
        """Conservative warm-up estimate: the slowest indicator plus the
        4 bars of slope history. Volume MA tends to be the slowest."""
        return max(
            self.config.ema_slow,
            self.config.vol_ma,
            self.config.atr_len,
        ) + 4

    # ---- update ------------------------------------------------------

    def update(self, bar: Bar) -> PulseFeatures | None:
        cfg = self.config
        ef = self._ema_fast.update(bar.c)
        es = self._ema_slow.update(bar.c)
        vma = self._vol_sma.update(float(bar.v))
        atr = self._atr.update(bar)

        if ef is None or es is None or vma is None or atr is None:
            return None

        sep = ef - es
        if self._prev_sep is None:
            self._prev_sep = sep
            return None
        slope = sep - self._prev_sep
        self._prev_sep = sep
        self._slopes.appendleft(slope)

        if len(self._slopes) < 4:
            return None

        # 4-bar weighted slope: direction (sign) and magnitude (raw)
        s0, s1, s2, s3 = self._slopes[0], self._slopes[1], self._slopes[2], self._slopes[3]
        mom_w = (
            _sign(s0) * self._w[0] + _sign(s1) * self._w[1]
            + _sign(s2) * self._w[2] + _sign(s3) * self._w[3]
        ) / self._wsum
        mag_w = (
            s0 * self._w[0] + s1 * self._w[1] + s2 * self._w[2] + s3 * self._w[3]
        ) / self._wsum

        # tanh-normalised magnitude (Pine uses (exp(2x)-1)/(exp(2x)+1))
        clipped = max(min(mag_w, 5.0), -5.0)
        tanh_mag = math.tanh(clipped)

        # RVOL with the same safety floor as the Pine indicator
        rvol = bar.v / vma if (vma > 0 and bar.v > 0) else 1.0
        rvol = max(rvol, 0.01)

        raw_score = (
            cfg.w_mom * mom_w
            + cfg.w_mag * tanh_mag
            + cfg.w_rvol * (rvol - 1.0)
        )
        score = max(min(raw_score, cfg.score_clip), -cfg.score_clip)

        # Logistic probability (Pine: 1/(1+exp(-2*score)))
        p_long = 1.0 / (1.0 + math.exp(-2.0 * score))
        p_short = 1.0 - p_long
        edge = abs(p_long - 0.5) * 2.0

        # Projected move
        proj_pts = max(atr * (rvol / cfg.rvol_base), cfg.min_range_pts)

        return PulseFeatures(
            ema_fast=ef, ema_slow=es, ema_sep=sep, slope=slope,
            atr=atr, rvol=rvol, mom_w=mom_w, mag_w=mag_w,
            tanh_mag=tanh_mag, raw_score=raw_score, score=score,
            p_long=p_long, p_short=p_short, edge=edge,
            proj_pts=proj_pts,
        )


def _sign(x: float) -> float:
    """math.copysign-based sign; returns 0 for x == 0 (Pine math.sign)."""
    if x > 0:
        return 1.0
    if x < 0:
        return -1.0
    return 0.0
