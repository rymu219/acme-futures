"""DEPRECATED — v3 multi-variant runner has been archived.

The 16-variant v3 runtime was retired 2026-05-11 after the audit
(docs/v3_audit.md). The replacement is `acme.fleet_runner` running the
4 Part-2 strategies (IGNITION / SESSION / REGIME / BOUNDARY) through
the classic conductor.

This module is now a thin compatibility shim that re-exports the
archived definitions from `archive/v3/runner.py`. Anything still
importing from `acme.runner` keeps working — the audit script and the
v3 backtest harness both rely on the `VARIANTS` list to recover per-
variant metadata. A `DeprecationWarning` is emitted on import so any
new callers notice.

The archived runner is preserved primarily for:
  - audit reproducibility (`scripts/v3_audit/section_00_triage.py`)
  - backtest harness (`src/acme/backtest/v3_run.py`,
    `src/acme/backtest/v3_replay.py`)

It is NOT instantiated by any live process. The LaunchAgent
`com.acme-futures.v3-runner.plist` has been unloaded; see CLAUDE.md
for the live supervision chain (now: `com.acme-futures.fleet-runner`).
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

warnings.warn(
    "acme.runner is archived; use acme.fleet_runner instead. "
    "See docs/v3_audit.md for context.",
    DeprecationWarning,
    stacklevel=2,
)

# Re-export everything from the archived module by injecting its dir
# onto sys.path and importing as a top-level `runner` module.
_ARCHIVE = Path(__file__).resolve().parents[2] / "archive" / "v3"
sys.path.insert(0, str(_ARCHIVE))

from runner import (  # type: ignore[import-not-found]  # noqa: E402,F401
    SESSION_END_CT,
    SESSION_OPEN_CT,
    VARIANTS,
    V3Runtime,
    _parse_hhmm,
    _resolve_classifier,
    _VariantSpec,
)
