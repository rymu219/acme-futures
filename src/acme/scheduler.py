"""APScheduler wiring: EOD flatten and daily-state rollover.

Topstep rule: traders must close all positions before 15:10 CT, Mon-Fri.
We flatten at 14:55 CT by default (15-minute buffer before the deadline).
Daily rollover fires Sun-Fri at 17:01 CT (1 min after Topstep's 17:00 resume).
APScheduler with `timezone="America/Chicago"` respects DST automatically.
"""

from __future__ import annotations

from typing import Protocol

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

log = structlog.get_logger(__name__)


class _Flattenable(Protocol):
    async def flatten_all(self) -> None: ...


class _Loggable(Protocol):
    def log_event(self, kind: str, **kwargs) -> None: ...


async def flatten_eod(broker: _Flattenable, db: _Loggable | None) -> None:
    if db is not None:
        db.log_event("flatten_triggered")
    await broker.flatten_all()
    log.info("eod_flatten_complete")


async def rollover_day(broker: _Flattenable, db: _Loggable | None) -> None:
    log.info("daily_rollover_fired")


def build_scheduler(
    broker: _Flattenable,
    db: _Loggable | None = None,
    *,
    tz: str = "America/Chicago",
    flatten_hh: int = 14,
    flatten_mm: int = 55,
) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone=tz)
    sched.add_job(
        flatten_eod,
        trigger=CronTrigger(
            day_of_week="mon-fri", hour=flatten_hh, minute=flatten_mm, timezone=tz
        ),
        args=[broker, db],
        misfire_grace_time=60,
        coalesce=True,
        id="eod_flatten",
    )
    sched.add_job(
        rollover_day,
        trigger=CronTrigger(
            day_of_week="sun,mon,tue,wed,thu,fri", hour=17, minute=1, timezone=tz
        ),
        args=[broker, db],
        misfire_grace_time=60,
        coalesce=True,
        id="daily_rollover",
    )
    return sched
