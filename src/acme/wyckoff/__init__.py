"""Wyckoff phase classifier — per-bar state machine that emits phase
snapshots and confirmed events.

Mirrors the architecture of `acme.regime`:
  - `classifier.WyckoffClassifier` — stateful engine, fed bars in order
  - `state.Phase` / `state.EventKind` — typed enums
  - `events.WyckoffEvent` — confirmed event dataclass

Persistence is the caller's job. The engine never imports Supabase.
The conductor (or a backtest harness) is responsible for writing
snapshots and events to the `wyckoff_state` / `wyckoff_events` tables.

Single-threaded v1: implements the Accumulation sequence
(SC → AR → ST → Spring → SOS → MARKUP). Distribution sequence
(BC → AR → ST → UT → SOW → MARKDOWN) is a follow-up phase — UT
detection is stubbed but not driven.
"""
from acme.wyckoff.classifier import (  # noqa: F401
    WyckoffClassifier,
    WyckoffConfig,
    WyckoffSnapshot,
)
from acme.wyckoff.events import WyckoffEvent  # noqa: F401
from acme.wyckoff.state import EventKind, Phase  # noqa: F401
