"""GO/NO-GO Box — 4-gate hard-binary entry filter.

Python port of the user-pasted Pine indicator "GO/NO-GO Box
(EMA9/14 · Sep · Volume · Time)". The four gates:

  1. **Separation** — |EMA_fast - EMA_slow| >= sep_thr
     ("are the EMAs separated enough to indicate a trend, not a chop?")

  2. **Volume ratio** — volume / SMA(volume, vr_len) >= vr_thr,
     optionally with the additional requirement that VR is rising
     vs. the prior bar
     ("is there real participation?")

  3. **Slope alignment** — both EMA_fast and EMA_slow have moved in the
     same direction over `slope_lookback` bars, by at least slope_thr
     ("are both EMAs trending the same way?")

  4. **Time window** — handled by the strategy layer (see
     `acme.calendar`); this module does not own session logic so the
     caller can plug in audit-driven windows that differ from the
     Pine indicator's defaults.

Composite signal:
    LONG   ← sep_ok AND vr_ok AND up_slope
    SHORT  ← sep_ok AND vr_ok AND down_slope
    WAIT   ← anything else

Defaults match the Pine indicator's inputs.

Note on the Pine indicator's afternoon time window (14:00-15:15 ET =
13:00-14:15 CT): this overlaps the worst hour in v3 audit §2
(13:00 CT had fleet PF 0.30 — the single worst hour in 7 days). The
strategy layer that consumes this module should use audit-driven
windows (03-05 CT and 08-09 CT, possibly 17 CT), not the Pine's
defaults.
"""
from __future__ import annotations

from dataclasses import dataclass

from acme.broker.base import Bar
from acme.indicators import EMA, SMA


@dataclass(frozen=True)
class GoNoGoConfig:
    """Defaults match the Pine indicator's inputs."""
    ema_fast: int = 9
    ema_slow: int = 14
    sep_thr: float = 0.35
    vr_len: int = 20
    vr_thr: float = 0.85
    require_vr_rising: bool = True
    slope_lookback: int = 1
    slope_thr: float = 0.0

    def __post_init__(self) -> None:
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast must be < ema_slow")
        if self.slope_lookback < 1:
            raise ValueError("slope_lookback must be >= 1")
        if self.sep_thr < 0:
            raise ValueError("sep_thr must be >= 0")
        if self.vr_thr <= 0:
            raise ValueError("vr_thr must be > 0")


@dataclass(frozen=True)
class GoNoGoState:
    """One bar's GO/NO-GO state. All four gates are reported individually
    so callers (UI, strategy, logs) can show *why* a bar is WAIT.
    """
    ema_fast: float
    ema_slow: float
    abs_sep: float
    vr: float
    vr_rising: bool
    slope_fast: float
    slope_slow: float
    # gates
    sep_ok: bool
    vr_ok: bool
    up_aligned: bool
    down_aligned: bool
    # composite (without time gate — strategy layer adds that)
    signal_long: bool
    signal_short: bool

    @property
    def signal(self) -> int:
        """+1 LONG, -1 SHORT, 0 WAIT (gates only — strategy adds time)."""
        if self.signal_long:
            return 1
        if self.signal_short:
            return -1
        return 0


class GoNoGoEngine:
    """Stateful GO/NO-GO feature engine.

    Usage:
        eng = GoNoGoEngine()
        for bar in bars:
            state = eng.update(bar)
            if state is None:
                continue          # warming up
            if state.signal == 1 and in_window(bar.t):
                ...               # enter long
    """

    def __init__(self, config: GoNoGoConfig | None = None) -> None:
        self.config = config or GoNoGoConfig()
        self._ema_fast = EMA(self.config.ema_fast)
        self._ema_slow = EMA(self.config.ema_slow)
        self._vol_sma = SMA(self.config.vr_len)
        # History buffers for slope lookback and VR-rising check.
        # We need slope_lookback + 1 EMA snapshots and 2 VR snapshots.
        self._ema_fast_hist: list[float] = []
        self._ema_slow_hist: list[float] = []
        self._vr_prev: float | None = None

    @property
    def is_warm(self) -> bool:
        return (
            self._ema_fast.is_warm
            and self._ema_slow.is_warm
            and self._vol_sma.is_warm
            and len(self._ema_fast_hist) > self.config.slope_lookback
            and len(self._ema_slow_hist) > self.config.slope_lookback
        )

    def required_history_bars(self) -> int:
        return max(self.config.ema_slow, self.config.vr_len) + self.config.slope_lookback + 1

    def update(self, bar: Bar) -> GoNoGoState | None:
        cfg = self.config
        ef = self._ema_fast.update(bar.c)
        es = self._ema_slow.update(bar.c)
        vma = self._vol_sma.update(float(bar.v))
        if ef is None or es is None or vma is None:
            return None

        # Append to history — bounded length to avoid unbounded growth
        self._ema_fast_hist.append(ef)
        self._ema_slow_hist.append(es)
        max_keep = cfg.slope_lookback + 4
        if len(self._ema_fast_hist) > max_keep:
            self._ema_fast_hist = self._ema_fast_hist[-max_keep:]
            self._ema_slow_hist = self._ema_slow_hist[-max_keep:]
        if len(self._ema_fast_hist) <= cfg.slope_lookback:
            return None  # not enough history for slope

        abs_sep = abs(ef - es)
        slope_fast = ef - self._ema_fast_hist[-1 - cfg.slope_lookback]
        slope_slow = es - self._ema_slow_hist[-1 - cfg.slope_lookback]

        vr = (float(bar.v) / vma) if (vma > 0 and bar.v > 0) else 0.0
        vr_rising = (self._vr_prev is None) or (vr > self._vr_prev)
        # Update prev for next call
        prev_vr = self._vr_prev
        self._vr_prev = vr

        sep_ok = abs_sep >= cfg.sep_thr
        vr_ok = (vr >= cfg.vr_thr) and (vr_rising if cfg.require_vr_rising else True)
        up_aligned = (slope_fast > cfg.slope_thr) and (slope_slow > cfg.slope_thr)
        down_aligned = (slope_fast < -cfg.slope_thr) and (slope_slow < -cfg.slope_thr)

        signal_long = sep_ok and vr_ok and up_aligned
        signal_short = sep_ok and vr_ok and down_aligned

        return GoNoGoState(
            ema_fast=ef, ema_slow=es, abs_sep=abs_sep,
            vr=vr, vr_rising=vr_rising,
            slope_fast=slope_fast, slope_slow=slope_slow,
            sep_ok=sep_ok, vr_ok=vr_ok,
            up_aligned=up_aligned, down_aligned=down_aligned,
            signal_long=signal_long, signal_short=signal_short,
        )
