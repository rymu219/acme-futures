"""Phase + event-kind enums for the Wyckoff classifier."""
from __future__ import annotations

from enum import StrEnum


class Phase(StrEnum):
    """Market phase per the Wyckoff framework. The classifier holds
    exactly one phase at a time (v1 single-threaded design).

    The `UNKNOWN` phase is the boot state and the fallback when an
    in-progress sequence expires before completing. The classifier
    never resets to UNKNOWN mid-sequence — only when no last
    confirmed transition is on record.
    """
    UNKNOWN      = "unknown"
    ACCUMULATION = "accumulation"
    MARKUP       = "markup"
    DISTRIBUTION = "distribution"
    MARKDOWN     = "markdown"


class EventKind(StrEnum):
    """Wyckoff event labels. Stored in `wyckoff_events.event_kind`."""
    SC      = "sc"        # Selling Climax (accumulation start)
    AR      = "ar"        # Automatic Rally
    ST      = "st"        # Secondary Test
    SPRING  = "spring"    # Spring (false breakdown + recovery)
    SOS     = "sos"       # Sign of Strength (markup confirmation)
    UT      = "ut"        # Upthrust (distribution mirror of Spring)
