"""Strategy registry — in-memory mirror of the Supabase `strategies` table.

The registry is the source of truth for which strategies exist, their lifecycle
state, tier, params, and current confidence score. The Conductor reads it on
every bar to decide who to fan signals to.

State transitions are auditable — every transition writes a `state_transition`
event to broker_events.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import structlog

from acme.db import Db
from acme.strategies.base import LifecycleState, Strategy, StrategyMetadata

log = structlog.get_logger(__name__)


VALID_TRANSITIONS: dict[LifecycleState, set[LifecycleState]] = {
    "BACKTEST": {"REPLAY", "RETIRED"},
    "REPLAY":   {"SHADOW", "BACKTEST", "RETIRED"},
    "SHADOW":   {"PILOT", "BENCH", "RETIRED"},
    "PILOT":    {"LIVE", "BENCH", "SHADOW", "RETIRED"},
    "LIVE":     {"BENCH", "PILOT", "RETIRED"},
    "BENCH":    {"PILOT", "LIVE", "RETIRED"},
    "RETIRED":  set(),
}


@dataclass
class RegisteredStrategy:
    name: str
    version: str
    state: LifecycleState
    tier: int
    params: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0
    notes: str = ""
    bench_cycles_30d: int = 0
    instance: Strategy | None = None         # populated by attach_instance()
    metadata: StrategyMetadata | None = None  # mirror of instance.metadata

    @property
    def is_executable(self) -> bool:
        """True for states where the conductor should actually place orders."""
        return self.state in ("PILOT", "LIVE")

    @property
    def is_active(self) -> bool:
        """True for states where the strategy receives bars (signals get logged)."""
        return self.state not in ("RETIRED",)


class IllegalTransitionError(Exception):
    pass


class StrategyRegistry:
    """In-memory mirror of the Supabase `strategies` table, with attached
    runtime instances. Single-process; no concurrency guards (the Conductor
    is the only writer in production).
    """

    def __init__(self, db: Db | None = None) -> None:
        self.db = db
        self._strats: dict[str, RegisteredStrategy] = {}

    # ---------- loading & persistence ----------

    def load_from_db(self) -> None:
        if self.db is None:
            raise RuntimeError("registry has no Db; cannot load")
        res = self.db.client.table("strategies").select("*").execute()
        for row in res.data or []:
            params = row.get("params") or {}
            if isinstance(params, str):
                params = json.loads(params)
            self._strats[row["name"]] = RegisteredStrategy(
                name=row["name"],
                version=row["version"],
                state=row["state"],
                tier=int(row.get("tier") or 2),
                params=params,
                score=float(row.get("score") or 0),
                notes=row.get("notes") or "",
                bench_cycles_30d=int(row.get("bench_cycles_30d") or 0),
            )
        log.info("registry_loaded", count=len(self._strats))

    def attach_instance(self, name: str, instance: Strategy) -> None:
        """Bind a runtime Strategy object to a registered row, copying metadata."""
        if name not in self._strats:
            raise KeyError(f"unknown strategy {name}; load_from_db or upsert first")
        self._strats[name].instance = instance
        self._strats[name].metadata = getattr(instance, "metadata", None)

    def upsert(
        self,
        name: str,
        version: str,
        state: LifecycleState,
        *,
        tier: int = 2,
        params: dict | None = None,
        notes: str = "",
    ) -> RegisteredStrategy:
        rec = RegisteredStrategy(
            name=name, version=version, state=state, tier=tier,
            params=params or {}, notes=notes,
        )
        self._strats[name] = rec
        if self.db is not None:
            self.db.client.table("strategies").upsert({
                "name": name, "version": version, "state": state,
                "tier": tier, "params": rec.params, "notes": notes,
            }, on_conflict="name").execute()
        return rec

    # ---------- transitions ----------

    def transition(
        self,
        name: str,
        to_state: LifecycleState,
        reason: str,
        *,
        triggered_by: str = "manual",
    ) -> None:
        rec = self._strats.get(name)
        if rec is None:
            raise KeyError(f"unknown strategy {name}")
        if to_state not in VALID_TRANSITIONS.get(rec.state, set()):
            raise IllegalTransitionError(
                f"cannot transition {name} from {rec.state} to {to_state}"
            )
        prev = rec.state
        rec.state = to_state
        if self.db is not None:
            self.db.client.table("strategies").update({
                "state": to_state,
                "last_promoted_at": "now()",
            }).eq("name", name).execute()
            self.db.log_event(
                "state_transition",
                strategy=name,
                raw={
                    "from": prev,
                    "to": to_state,
                    "reason": reason,
                    "triggered_by": triggered_by,
                },
            )
        log.info("registry_transition", name=name, from_=prev, to=to_state, reason=reason)

    # ---------- score ----------

    def set_score(self, name: str, score: float) -> None:
        rec = self._strats.get(name)
        if rec is None:
            raise KeyError(f"unknown strategy {name}")
        rec.score = score
        if self.db is not None:
            self.db.client.table("strategies").update(
                {"score": score}
            ).eq("name", name).execute()

    # ---------- queries ----------

    def get(self, name: str) -> RegisteredStrategy:
        return self._strats[name]

    def list_all(self) -> list[RegisteredStrategy]:
        return list(self._strats.values())

    def list_active(self) -> list[RegisteredStrategy]:
        return [s for s in self._strats.values() if s.is_active and s.instance is not None]

    def list_executable(self) -> list[RegisteredStrategy]:
        return [s for s in self._strats.values() if s.is_executable and s.instance is not None]

    def __len__(self) -> int:
        return len(self._strats)

    def __contains__(self, name: str) -> bool:
        return name in self._strats
