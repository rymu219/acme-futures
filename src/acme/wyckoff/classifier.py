"""WyckoffClassifier — stateful per-bar phase + event detector.

Implements the Accumulation sequence (SC → AR → ST → Spring → SOS).
Distribution mirror (BC → AR → ST → UT) is deferred to a follow-up
phase; UT detection is not driven in v1.

LOOK-AHEAD-FREE INVARIANT (load-bearing): the classifier never uses
information from a future bar to make a decision about the current
bar. Spring detection, which the user's spec defines as "breaks below
ST low on light volume, recovers above ST low within 1-3 bars," is
implemented by:

  1. Detecting a Spring CANDIDATE on bar T (light-volume break below
     ST low). The classifier records the candidate in internal state
     but DOES NOT emit a confirmed event or change phase yet.
  2. On each subsequent bar T+1, T+2, T+3, checking whether bar.c has
     recovered above ST low. If yes — emit the Spring event with
     bar_ts = the *recovery* bar's timestamp. If no recovery within
     3 bars — drop the candidate silently.

A Spring event therefore corresponds to a recovery bar, not a
candidate bar. The Phase 2 Spring Strategy (when built) will read
this event from `wyckoff_events` and enter on the *next* bar after
the recovery bar — same execution lag pattern that other strategies
use to avoid intra-bar lookahead.

CONFIG defaults match the orient doc:

    SC→AR window      = 30 bars      (after SC, AR must arrive in 30 bars)
    AR→ST window      = 30 bars
    ST→Spring window  = 30 bars
    Spring→SOS window =  5 bars
    Multi-bar low LB  = 20 bars      (price near multi-bar low = bar.l within
                                       near_tolerance_ticks of min(low_lookback))
    ATR period        = 14
    Near tolerance    =  2 ticks
    Arming expiry     = revert to last confirmed phase (DON'T reset to UNKNOWN)
    Threading         = single-threaded (no parallel accumulation/distribution)
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from acme.broker.base import Bar
from acme.contracts import MES, FuturesContract
from acme.indicators import ATR, SMA
from acme.wyckoff.events import WyckoffEvent
from acme.wyckoff.state import EventKind, Phase

# ───────────────────────── config ──────────────────────────────────────


@dataclass(frozen=True)
class WyckoffConfig:
    # Indicator windows
    atr_period:              int   = 14
    rvol_sma_period:         int   = 20
    multi_bar_low_lookback:  int   = 20
    near_tolerance_ticks:    int   = 2

    # ── SC (Selling Climax) ────────────────────────────────────────
    sc_volume_multiple:      float = 2.0        # bar.v >= 2.0 × vol_sma
    sc_spread_atr_multiple:  float = 1.5        # spread >= 1.5 × ATR
    sc_close_position_min:   float = 0.70       # close in upper 30% of bar

    # ── AR (Automatic Rally) ──────────────────────────────────────
    ar_window_bars:          int   = 30
    ar_min_advance_atr:      float = 0.5        # bar.c > sc.c + 0.5 × ATR
    # AR volume must be moderate (< SC volume). Coded as fraction of SC vol.
    ar_volume_max_frac_sc:   float = 1.0

    # ── ST (Secondary Test) ──────────────────────────────────────
    st_window_bars:          int   = 30
    st_volume_max_frac_sc:   float = 0.60
    # "Returns to SC area": bar.l within `near_tolerance_ticks` of SC low,
    # and bar.l >= SC low (holds above).

    # ── Spring ────────────────────────────────────────────────────
    spring_window_bars:      int   = 30
    spring_volume_max_frac_st: float = 0.80
    # Recovery: bar.c > ST low within `spring_recovery_max_bars` bars
    # AFTER the candidate bar.
    spring_recovery_max_bars: int  = 3

    # ── SOS (Sign of Strength) ────────────────────────────────────
    sos_window_bars:         int   = 5
    sos_spread_atr_multiple: float = 1.0        # wide-spread up bar
    # Volume expanding vs the prior bar.
    sos_volume_expanding:    bool  = True


# ───────────────────────── snapshot ────────────────────────────────────


@dataclass(frozen=True)
class WyckoffSnapshot:
    """Per-bar output of the classifier. Persists to `wyckoff_state`
    (one row per bar). The `new_events` list carries any events
    confirmed *on this bar* — typically empty, occasionally one,
    very rarely more than one."""
    ts:                 datetime
    timeframe:          str
    contract:           str
    phase:              Phase
    last_event_kind:    EventKind | None
    last_event_ts:      datetime | None
    armed_for:          EventKind | None       # next-expected event
    spread_atr_ratio:   float | None
    rvol:               float | None
    new_events:         list[WyckoffEvent] = field(default_factory=list)
    notes:              str = ""

    def to_db_row(self) -> dict:
        """Shape matches the `wyckoff_state` migration schema."""
        return {
            "ts":               self.ts.isoformat(),
            "timeframe":        self.timeframe,
            "contract":         self.contract,
            "phase":            self.phase.value,
            "last_event":       (self.last_event_kind.value
                                 if self.last_event_kind is not None else None),
            "last_event_ts":    (self.last_event_ts.isoformat()
                                 if self.last_event_ts is not None else None),
            "armed_for":        (self.armed_for.value
                                 if self.armed_for is not None else None),
            "spread_atr":       self.spread_atr_ratio,
            "rvol":             self.rvol,
            "raw_state":        {"notes": self.notes},
        }


# ───────────────────────── pending-event records ───────────────────────


@dataclass
class _AnchorEvent:
    """Internal record of a confirmed event that's anchoring the next
    arm — e.g., SC anchors the AR detector, ST anchors Spring."""
    kind:    EventKind
    bar_ts:  datetime
    bar_l:   float
    bar_h:   float
    bar_c:   float
    bar_v:   int
    armed_at_index: int     # classifier's monotonic bar counter at confirmation


@dataclass
class _SpringCandidate:
    """Internal record of a Spring CANDIDATE awaiting recovery confirmation.

    Created when a bar breaks below the ST low on light volume.
    Resolved within `spring_recovery_max_bars` bars when a subsequent
    bar's close climbs back above ST low (→ confirm event) or expires
    silently (→ drop). The candidate bar's timestamp is recorded for
    reference but the EVENT timestamp will be the *recovery* bar.
    """
    candidate_bar_ts: datetime
    candidate_index:  int
    st_low:           float
    st_volume:        int
    st_bar_ts:        datetime
    candidate_o:      float
    candidate_h:      float
    candidate_l:      float
    candidate_c:      float
    candidate_v:      int
    candidate_atr:    float | None


# ───────────────────────── classifier ──────────────────────────────────


class WyckoffClassifier:
    """Stateful Wyckoff phase + event detector. Fed bars in chronological
    order via `on_bar(bar)`. Emits a `WyckoffSnapshot` every bar.

    State persistence (across runner restarts) is the caller's job: load
    recent `wyckoff_events` rows from Supabase and replay them via
    `replay_event()` before feeding live bars.
    """

    def __init__(
        self,
        config: WyckoffConfig | None = None,
        *,
        timeframe_minutes: int = 2,
        contract: FuturesContract = MES,
    ) -> None:
        self.config = config or WyckoffConfig()
        self.contract = contract
        self.timeframe_minutes = timeframe_minutes
        self.timeframe_label = f"{timeframe_minutes}m"

        # Indicators — all from acme.indicators (no new code needed)
        self._atr = ATR(self.config.atr_period)
        self._vol_sma = SMA(self.config.rvol_sma_period)
        self._low_window: deque[float] = deque(
            maxlen=self.config.multi_bar_low_lookback
        )

        # State machine
        self._phase: Phase = Phase.UNKNOWN
        self._last_event_kind: EventKind | None = None
        self._last_event_ts: datetime | None = None
        self._armed_for: EventKind | None = None

        # Anchor events — the confirmed predecessor that's arming the
        # current detector. Only one accumulation thread tracked at a time.
        self._sc: _AnchorEvent | None = None
        self._ar: _AnchorEvent | None = None
        self._st: _AnchorEvent | None = None
        self._spring: _AnchorEvent | None = None  # confirmed Spring (post-recovery)

        # Pending Spring candidate (arm-on-candidate / confirm-on-recovery).
        # NOT persisted — purely transient state in the classifier.
        self._spring_candidate: _SpringCandidate | None = None

        # Bar counter (monotonic, used for window-expiry checks)
        self._bar_idx: int = 0

        # Prior bar's volume — needed for SOS "expanding volume" check.
        self._prev_volume: int | None = None

    # ──────────────────────────── helpers ─────────────────────────

    @property
    def near_tolerance_pts(self) -> float:
        return self.config.near_tolerance_ticks * self.contract.tick_size

    def _close_position(self, bar: Bar) -> float | None:
        """Where bar.c sits within [bar.l, bar.h], 0..1. None on degenerate range."""
        rng = bar.h - bar.l
        if rng <= 0:
            return None
        return (bar.c - bar.l) / rng

    def _spread_atr_ratio(self, bar: Bar, atr_val: float | None) -> float | None:
        if atr_val is None or atr_val <= 0:
            return None
        return (bar.h - bar.l) / atr_val

    def _rvol(self, bar: Bar, vol_sma: float | None) -> float | None:
        if vol_sma is None or vol_sma <= 0:
            return None
        return float(bar.v) / vol_sma

    def _make_event(
        self, bar: Bar, kind: EventKind,
        *,
        atr_val: float | None,
        rvol_val: float | None,
        volume_vs_anchor: float | None,
        ref: _AnchorEvent | None,
        phase: Phase,
        notes: str,
    ) -> WyckoffEvent:
        spread_pts = bar.h - bar.l
        return WyckoffEvent(
            bar_ts=bar.t, kind=kind, phase_at_event=phase,
            contract=self.contract.symbol,
            bar_o=bar.o, bar_h=bar.h, bar_l=bar.l, bar_c=bar.c, bar_v=int(bar.v),
            spread_pts=spread_pts, atr_pts=atr_val,
            spread_atr_ratio=(spread_pts / atr_val) if (atr_val and atr_val > 0) else None,
            rvol=rvol_val,
            volume_vs_anchor=volume_vs_anchor,
            ref_event_kind=ref.kind if ref else None,
            ref_event_ts=ref.bar_ts if ref else None,
            notes=notes,
        )

    def _make_anchor(self, bar: Bar, kind: EventKind) -> _AnchorEvent:
        return _AnchorEvent(
            kind=kind, bar_ts=bar.t,
            bar_l=bar.l, bar_h=bar.h, bar_c=bar.c, bar_v=int(bar.v),
            armed_at_index=self._bar_idx,
        )

    # ──────────────────────────── replay ──────────────────────────

    def replay_event(self, kind: EventKind, ts: datetime,
                     bar_l: float, bar_h: float, bar_c: float, bar_v: int) -> None:
        """Restore state from a persisted event. Called on conductor
        startup with the recent `wyckoff_events` rows in chronological
        order BEFORE the first live bar is fed.

        This is intentionally minimal — it sets phase + anchor refs but
        does NOT re-compute indicators. The indicators warm naturally
        once live bars start flowing.
        """
        anchor = _AnchorEvent(
            kind=kind, bar_ts=ts,
            bar_l=bar_l, bar_h=bar_h, bar_c=bar_c, bar_v=bar_v,
            armed_at_index=0,
        )
        self._last_event_kind = kind
        self._last_event_ts = ts
        if kind == EventKind.SC:
            self._phase = Phase.ACCUMULATION
            self._sc = anchor
            self._armed_for = EventKind.AR
        elif kind == EventKind.AR:
            self._phase = Phase.ACCUMULATION
            self._ar = anchor
            self._armed_for = EventKind.ST
        elif kind == EventKind.ST:
            self._phase = Phase.ACCUMULATION
            self._st = anchor
            self._armed_for = EventKind.SPRING
        elif kind == EventKind.SPRING:
            self._phase = Phase.ACCUMULATION
            self._spring = anchor
            self._armed_for = EventKind.SOS
        elif kind == EventKind.SOS:
            self._phase = Phase.MARKUP
            self._armed_for = None
            # Sequence complete; clear anchors so the next SC starts fresh.
            self._sc = self._ar = self._st = self._spring = None
        # UT replay reserved for the distribution-sequence follow-up phase.

    # ──────────────────────────── arming expiry ───────────────────

    def _expire_stale_arms(self) -> None:
        """Drop arming context when its window has elapsed. Phase stays
        at whatever the last confirmed transition put it at — never
        resets to UNKNOWN mid-sequence.
        """
        cfg = self.config
        if (self._armed_for == EventKind.AR and self._sc is not None
                and self._bar_idx - self._sc.armed_at_index > cfg.ar_window_bars):
            self._sc = None
            self._armed_for = None
        elif (self._armed_for == EventKind.ST and self._ar is not None
                and self._bar_idx - self._ar.armed_at_index > cfg.st_window_bars):
            self._ar = None
            self._armed_for = None
        elif (self._armed_for == EventKind.SPRING and self._st is not None
                and self._bar_idx - self._st.armed_at_index > cfg.spring_window_bars):
            self._st = None
            self._armed_for = None
        elif (self._armed_for == EventKind.SOS and self._spring is not None
                and self._bar_idx - self._spring.armed_at_index > cfg.sos_window_bars):
            self._spring = None
            self._armed_for = None

    # ──────────────────────────── event detectors ─────────────────

    def _detect_sc(
        self, bar: Bar, atr_val: float | None,
        rvol_val: float | None, close_pos: float | None,
    ) -> bool:
        """SC: volume ≥ 2× vol_sma, spread ≥ 1.5×ATR, close in upper 30%,
        bar.l near multi-bar low. Detected freshly (no prior SC armed)."""
        if rvol_val is None or close_pos is None:
            return False
        if atr_val is None or atr_val <= 0:
            return False
        cfg = self.config
        if rvol_val < cfg.sc_volume_multiple:
            return False
        spread = bar.h - bar.l
        if spread < cfg.sc_spread_atr_multiple * atr_val:
            return False
        if close_pos < cfg.sc_close_position_min:
            return False
        if len(self._low_window) < self._low_window.maxlen:
            return False
        # bar.l must be within near tolerance of the lookback min low.
        # Use the window MINUS the current bar's low so we test "near
        # prior multi-bar low" (otherwise the current bar's low is the
        # min by construction and always passes).
        prior_lows = [x for i, x in enumerate(self._low_window)
                      if i < len(self._low_window) - 1]
        if not prior_lows:
            return False
        prior_min = min(prior_lows)
        # Bar qualifies if either it's within near-tolerance of the prior
        # multi-bar low OR it pushed below that low (capitulation can
        # extend the prior min downward by more than `near_tolerance_pts`).
        within_tolerance = abs(bar.l - prior_min) <= self.near_tolerance_pts
        made_new_low = bar.l <= prior_min
        return within_tolerance or made_new_low

    def _detect_ar(self, bar: Bar, atr_val: float | None) -> bool:
        """AR: after SC, bar advances by >= 0.5×ATR vs SC close, with
        moderate volume (< SC vol). Stop at first qualifying bar."""
        if self._sc is None or atr_val is None or atr_val <= 0:
            return False
        cfg = self.config
        if bar.c <= self._sc.bar_c + cfg.ar_min_advance_atr * atr_val:
            return False
        return bar.v < self._sc.bar_v * cfg.ar_volume_max_frac_sc

    def _detect_st(self, bar: Bar) -> bool:
        """ST: returns to SC area on volume < 60% of SC vol, holds above
        SC low. Requires SC + AR have already fired."""
        if self._sc is None or self._ar is None:
            return False
        cfg = self.config
        if bar.v >= self._sc.bar_v * cfg.st_volume_max_frac_sc:
            return False
        # "Returns to SC area": bar.l within 4× near-tolerance of SC low
        # (~8 ticks / ~2pt on MES — broader than the per-bar near-tolerance
        # but tighter than 1×ATR; loose enough to count as a "return to
        # the level").
        if abs(bar.l - self._sc.bar_l) > self.near_tolerance_pts * 4:
            return False
        # "Holds above SC low": bar.l >= SC low.
        return bar.l >= self._sc.bar_l

    def _detect_spring_candidate(self, bar: Bar) -> bool:
        """Spring CANDIDATE: bar.l < ST low AND vol < 80% of ST vol.
        ARM ONLY — do not emit a confirmed Spring event yet."""
        if self._st is None:
            return False
        cfg = self.config
        if bar.l >= self._st.bar_l:
            return False
        return bar.v < self._st.bar_v * cfg.spring_volume_max_frac_st

    def _maybe_confirm_spring(
        self, bar: Bar, atr_val: float | None, rvol_val: float | None,
    ) -> WyckoffEvent | None:
        """If a Spring candidate is pending, check whether THIS bar
        constitutes a valid recovery. Returns the confirmed Spring event
        (timestamp = this bar) or None.

        Recovery rule: bar.c > ST low (recovered above the level that
        was briefly broken). Must happen within
        `spring_recovery_max_bars` bars of the candidate.

        If the recovery window expires without confirmation, drops the
        candidate silently and returns None."""
        cand = self._spring_candidate
        if cand is None or self._st is None:
            return None

        bars_since = self._bar_idx - cand.candidate_index
        # Window expired — drop the candidate and walk away (no event written).
        if bars_since > self.config.spring_recovery_max_bars:
            self._spring_candidate = None
            return None

        # Within window — does THIS bar recover?
        if bar.c <= cand.st_low:
            return None

        # Recovery confirmed. Emit event with this bar's metadata.
        volume_vs_anchor = (cand.candidate_v / cand.st_volume
                            if cand.st_volume > 0 else None)
        ref_anchor = _AnchorEvent(
            kind=EventKind.ST, bar_ts=cand.st_bar_ts,
            bar_l=cand.st_low, bar_h=0.0, bar_c=0.0, bar_v=cand.st_volume,
            armed_at_index=0,
        )
        notes = (
            f"recovery_bars_after_candidate={bars_since} "
            f"candidate_bar_ts={cand.candidate_bar_ts.isoformat()} "
            f"candidate_low={cand.candidate_l} st_low={cand.st_low} "
            f"candidate_vol={cand.candidate_v} "
            f"candidate_vol_vs_st={volume_vs_anchor:.3f}"
            if volume_vs_anchor is not None else
            f"recovery_bars_after_candidate={bars_since}"
        )
        event = self._make_event(
            bar, EventKind.SPRING, atr_val=atr_val, rvol_val=rvol_val,
            volume_vs_anchor=volume_vs_anchor, ref=ref_anchor,
            phase=Phase.ACCUMULATION, notes=notes,
        )
        # Treat the *current bar* (recovery) as the Spring anchor for
        # the SOS arming that follows.
        self._spring = self._make_anchor(bar, EventKind.SPRING)
        self._spring_candidate = None
        return event

    def _detect_sos(self, bar: Bar, atr_val: float | None) -> bool:
        """SOS: after a confirmed Spring, wide-spread up-bar on expanding
        volume. Spread ≥ 1.0×ATR, bar.c > bar.o, vol > prior bar's vol."""
        if self._spring is None or atr_val is None or atr_val <= 0:
            return False
        cfg = self.config
        if (bar.h - bar.l) < cfg.sos_spread_atr_multiple * atr_val:
            return False
        if bar.c <= bar.o:
            return False
        return not (cfg.sos_volume_expanding and (
            self._prev_volume is None or bar.v <= self._prev_volume
        ))

    # ──────────────────────────── on_bar ──────────────────────────

    def on_bar(self, bar: Bar) -> WyckoffSnapshot:
        """Drive one bar through the classifier. Always returns a
        snapshot; emits zero or more confirmed events in `new_events`.
        """
        # ─── 0. Indicators ────────────────────────────────────────
        atr_val = self._atr.update(bar)
        vol_sma = self._vol_sma.update(float(bar.v))
        self._low_window.append(bar.l)
        rvol_val = self._rvol(bar, vol_sma)
        spread_atr = self._spread_atr_ratio(bar, atr_val)
        close_pos = self._close_position(bar)
        self._bar_idx += 1

        new_events: list[WyckoffEvent] = []

        # ─── 1. Expire stale arms ────────────────────────────────
        self._expire_stale_arms()

        # ─── 2. Spring recovery check (BEFORE looking for new arms) ─
        # If we have a pending Spring candidate, this bar might confirm
        # it. Do this first because a confirmed Spring re-arms SOS.
        confirmed_spring = self._maybe_confirm_spring(bar, atr_val, rvol_val)
        if confirmed_spring is not None:
            new_events.append(confirmed_spring)
            self._last_event_kind = EventKind.SPRING
            self._last_event_ts = bar.t
            self._armed_for = EventKind.SOS

        # ─── 3. Detect new events based on what's armed ──────────
        # SOS — only checked when armed (Spring already confirmed).
        if self._armed_for == EventKind.SOS and self._detect_sos(bar, atr_val):
            volume_vs_anchor = (bar.v / self._spring.bar_v
                                if (self._spring and self._spring.bar_v > 0)
                                else None)
            evt = self._make_event(
                bar, EventKind.SOS, atr_val=atr_val, rvol_val=rvol_val,
                volume_vs_anchor=volume_vs_anchor, ref=self._spring,
                phase=Phase.ACCUMULATION,
                notes=f"spread_atr={spread_atr:.2f} prev_v={self._prev_volume}",
            )
            new_events.append(evt)
            self._last_event_kind = EventKind.SOS
            self._last_event_ts = bar.t
            self._phase = Phase.MARKUP
            self._armed_for = None
            # Sequence complete — clear anchors so next SC starts fresh.
            self._sc = self._ar = self._st = self._spring = None

        # Spring CANDIDATE (arming only — confirmation happens at a
        # later bar in step 2 of a future on_bar call).
        elif (self._armed_for == EventKind.SPRING
                and self._spring_candidate is None
                and self._detect_spring_candidate(bar)):
            self._spring_candidate = _SpringCandidate(
                candidate_bar_ts=bar.t,
                candidate_index=self._bar_idx,
                st_low=self._st.bar_l, st_volume=self._st.bar_v,
                st_bar_ts=self._st.bar_ts,
                candidate_o=bar.o, candidate_h=bar.h,
                candidate_l=bar.l, candidate_c=bar.c, candidate_v=int(bar.v),
                candidate_atr=atr_val,
            )
            # NO event emitted; phase unchanged.

        # ST
        elif self._armed_for == EventKind.ST and self._detect_st(bar):
            volume_vs_anchor = (bar.v / self._sc.bar_v
                                if (self._sc and self._sc.bar_v > 0)
                                else None)
            evt = self._make_event(
                bar, EventKind.ST, atr_val=atr_val, rvol_val=rvol_val,
                volume_vs_anchor=volume_vs_anchor, ref=self._sc,
                phase=Phase.ACCUMULATION,
                notes=f"sc_low={self._sc.bar_l} dist_pts={abs(bar.l - self._sc.bar_l):.2f}",
            )
            new_events.append(evt)
            self._st = self._make_anchor(bar, EventKind.ST)
            self._last_event_kind = EventKind.ST
            self._last_event_ts = bar.t
            self._armed_for = EventKind.SPRING

        # AR
        elif self._armed_for == EventKind.AR and self._detect_ar(bar, atr_val):
            volume_vs_anchor = (bar.v / self._sc.bar_v
                                if (self._sc and self._sc.bar_v > 0)
                                else None)
            evt = self._make_event(
                bar, EventKind.AR, atr_val=atr_val, rvol_val=rvol_val,
                volume_vs_anchor=volume_vs_anchor, ref=self._sc,
                phase=Phase.ACCUMULATION,
                notes=f"advance_pts={bar.c - self._sc.bar_c:.2f}",
            )
            new_events.append(evt)
            self._ar = self._make_anchor(bar, EventKind.AR)
            self._last_event_kind = EventKind.AR
            self._last_event_ts = bar.t
            self._armed_for = EventKind.ST

        # SC — only when nothing is armed (we always treat a fresh SC as
        # starting a new accumulation sequence, replacing any half-done one).
        if self._armed_for is None and self._detect_sc(bar, atr_val, rvol_val, close_pos):
            evt = self._make_event(
                bar, EventKind.SC, atr_val=atr_val, rvol_val=rvol_val,
                volume_vs_anchor=None, ref=None,
                phase=Phase.ACCUMULATION,
                notes=f"rvol={rvol_val:.2f} spread_atr={spread_atr:.2f} "
                      f"close_pos={close_pos:.2f}",
            )
            new_events.append(evt)
            self._sc = self._make_anchor(bar, EventKind.SC)
            self._last_event_kind = EventKind.SC
            self._last_event_ts = bar.t
            self._phase = Phase.ACCUMULATION
            self._armed_for = EventKind.AR
            # Clear any prior accumulation thread state.
            self._ar = self._st = self._spring = None
            self._spring_candidate = None

        # ─── 4. Update prior-volume tracker for next bar's SOS check ─
        self._prev_volume = int(bar.v)

        # ─── 5. Emit snapshot ────────────────────────────────────
        return WyckoffSnapshot(
            ts=bar.t, timeframe=self.timeframe_label,
            contract=self.contract.symbol,
            phase=self._phase,
            last_event_kind=self._last_event_kind,
            last_event_ts=self._last_event_ts,
            armed_for=self._armed_for,
            spread_atr_ratio=spread_atr, rvol=rvol_val,
            new_events=new_events,
            notes=(f"bar_idx={self._bar_idx} "
                   f"spring_candidate={'yes' if self._spring_candidate else 'no'}"),
        )
