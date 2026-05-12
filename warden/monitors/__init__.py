"""Warden monitor modules. Each monitor is a class implementing a
`run(sb)` method that returns a list of new operator_events to emit
(plus optionally a list of event ids to resolve)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MonitorResult:
    """Output of one monitor pass.

    Monitors are stateless across runs — they re-derive state from
    Supabase each tick. To avoid duplicate alerts, each monitor reads
    recent operator_events to check whether a matching alert is
    already open, and uses `events_to_emit` only for fresh anomalies.
    """
    events_to_emit: list[dict[str, Any]] = field(default_factory=list)
    events_to_resolve: list[int] = field(default_factory=list)
