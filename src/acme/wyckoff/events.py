"""WyckoffEvent dataclass + DB row serialization.

Events are the persistent currency of the Wyckoff classifier. One row
per confirmed event lands in `wyckoff_events`. State is reconstructed
on conductor restart by replaying these rows in order.

Events DO NOT correspond one-to-one with bars. The Spring event in
particular is written ONLY at the recovery-confirmation bar, never at
the initial candidate bar — see `WyckoffClassifier._maybe_confirm_spring`
for the arm-on-candidate / confirm-on-recovery mechanic.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from acme.wyckoff.state import EventKind, Phase


@dataclass(frozen=True)
class WyckoffEvent:
    """One confirmed Wyckoff event. Persists to `wyckoff_events`."""
    bar_ts:            datetime
    kind:              EventKind
    phase_at_event:    Phase
    contract:          str

    # OHLCV at the event bar (for the SOS / SC / etc. confirmation bar —
    # for Spring this is the *recovery* bar, not the candidate bar).
    bar_o: float
    bar_h: float
    bar_l: float
    bar_c: float
    bar_v: int

    # Volatility + spread context
    spread_pts:        float
    atr_pts:           float | None
    spread_atr_ratio:  float | None

    # Volume context
    rvol:              float | None     # vs 20-bar SMA
    volume_vs_anchor:  float | None     # e.g., ST vol / SC vol, Spring vol / ST vol

    # Cross-reference: the prior event in the sequence this event resolves.
    # e.g., ST.ref_event_kind = SC, Spring.ref_event_kind = ST.
    ref_event_kind:    EventKind | None
    ref_event_ts:      datetime | None

    notes:             str

    def to_db_row(self) -> dict:
        """Shape matches the `wyckoff_events` migration schema."""
        return {
            "bar_ts":           self.bar_ts.isoformat(),
            "contract":         self.contract,
            "event_kind":       self.kind.value,
            "phase_at_event":   self.phase_at_event.value,
            "bar_o":            self.bar_o,
            "bar_h":            self.bar_h,
            "bar_l":            self.bar_l,
            "bar_c":            self.bar_c,
            "bar_v":            self.bar_v,
            "spread_pts":       self.spread_pts,
            "atr_pts":          self.atr_pts,
            "spread_atr_ratio": self.spread_atr_ratio,
            "rvol":             self.rvol,
            "volume_vs_anchor": self.volume_vs_anchor,
            "ref_event_kind":   (self.ref_event_kind.value
                                 if self.ref_event_kind is not None else None),
            "ref_event_ts":     (self.ref_event_ts.isoformat()
                                 if self.ref_event_ts is not None else None),
            "notes":            self.notes,
        }
