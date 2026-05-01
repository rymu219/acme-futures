"""Flat-first state machine.

Topstep prohibits hedging — no opposite positions in the same account. When a
signal fires opposite the current position, the conductor must:

  1. CLOSING — submit a flatten order, wait for fill (or for next bar in dry-run)
  2. COOLDOWN — wait `cooldown_seconds` (default 60) so we don't whipsaw on a
     spurious signal
  3. RE_EVAL — on the next bar after cooldown, the strategy fleet is re-polled.
     The trade fires only if a same-direction signal still exists. This is
     critical: market may have moved, the original signal may be stale.

States: IDLE → CLOSING → COOLDOWN → RE_EVAL → IDLE.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import structlog

log = structlog.get_logger(__name__)

FlatFirstState = Literal["IDLE", "CLOSING", "COOLDOWN", "RE_EVAL"]
Direction = Literal["buy", "sell"]


@dataclass
class FlatFirstStatus:
    state: FlatFirstState
    cooldown_until: datetime | None
    pending_direction: Direction | None
    reason: str


class FlatFirstFSM:
    """Encapsulates the close-then-cooldown-then-re-evaluate flow.

    Hold one instance per Conductor. Methods are pure-ish: they return the new
    state and emit (via callbacks) what the conductor should do next. The FSM
    does NOT call the broker directly — it tells the caller what to do.
    """

    def __init__(self, cooldown_seconds: int = 60) -> None:
        self.cooldown_seconds = cooldown_seconds
        self._state: FlatFirstState = "IDLE"
        self._cooldown_until: datetime | None = None
        self._pending_direction: Direction | None = None
        self._last_reason: str = ""

    @property
    def status(self) -> FlatFirstStatus:
        return FlatFirstStatus(
            state=self._state,
            cooldown_until=self._cooldown_until,
            pending_direction=self._pending_direction,
            reason=self._last_reason,
        )

    @property
    def is_blocking(self) -> bool:
        """True when the conductor must NOT submit any new orders."""
        return self._state in ("CLOSING", "COOLDOWN")

    def request_reversal(
        self,
        new_direction: Direction,
        now: datetime,
        reason: str = "",
    ) -> Literal["close_now"]:
        """Called by the conductor when an opposite-direction signal wins
        arbitration while we hold a position. Transitions IDLE → CLOSING.
        """
        if self._state != "IDLE":
            log.warning("flat_first_already_active", current=self._state)
        self._state = "CLOSING"
        self._pending_direction = new_direction
        self._last_reason = reason
        log.info("flat_first_close", direction=new_direction, reason=reason)
        return "close_now"

    def on_position_closed(self, now: datetime) -> None:
        """Called by the conductor after the close fill is confirmed.
        Transitions CLOSING → COOLDOWN, sets the cooldown timer.
        """
        if self._state != "CLOSING":
            log.warning("flat_first_unexpected_close", state=self._state)
            return
        self._state = "COOLDOWN"
        self._cooldown_until = now + timedelta(seconds=self.cooldown_seconds)
        log.info("flat_first_cooldown_start", until=self._cooldown_until.isoformat())

    def tick(self, now: datetime) -> Literal["re_eval", None]:
        """Called by the conductor on each bar. If we're in COOLDOWN and the
        timer has elapsed, transitions to RE_EVAL and signals the conductor
        to re-poll the strategies on this bar.
        """
        if self._state == "COOLDOWN" and self._cooldown_until and now >= self._cooldown_until:
            self._state = "RE_EVAL"
            log.info("flat_first_re_eval", pending_direction=self._pending_direction)
            return "re_eval"
        return None

    def on_re_eval_done(self, executed: bool) -> None:
        """Called by the conductor after re-eval. Whether or not a same-direction
        signal still existed, we return to IDLE.
        """
        if self._state != "RE_EVAL":
            log.warning("flat_first_unexpected_re_eval_done", state=self._state)
        self._state = "IDLE"
        self._cooldown_until = None
        self._pending_direction = None
        self._last_reason = ""
        log.info("flat_first_idle", post_re_eval_executed=executed)
