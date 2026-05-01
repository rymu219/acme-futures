from datetime import datetime

import pytest
from apscheduler.triggers.cron import CronTrigger

from acme.broker.paper import PaperAdapter
from acme.scheduler import build_scheduler, flatten_eod


@pytest.mark.asyncio
async def test_flatten_eod_calls_broker():
    broker = PaperAdapter()
    await broker.submit_market_order("CON.F.US.MES.M26", "buy", 1)
    assert (await broker.get_positions())[0].size == 1

    class _DbStub:
        def __init__(self):
            self.events = []
        def log_event(self, kind, **kw):
            self.events.append((kind, kw))

    db = _DbStub()
    await flatten_eod(broker, db)
    positions = await broker.get_positions()
    assert all(p.size == 0 for p in positions)
    assert ("flatten_triggered", {}) in db.events


def test_scheduler_builds_with_eod_job_default_14_55():
    """Default flatten time is 14:55 CT — 15-min buffer before Topstep's 15:10 deadline."""
    broker = PaperAdapter()
    sched = build_scheduler(broker, db=None, tz="America/Chicago")
    job = sched.get_job("eod_flatten")
    assert job is not None
    trigger = job.trigger
    assert isinstance(trigger, CronTrigger)
    fields = {f.name: str(f) for f in trigger.fields}
    assert fields["hour"] == "14"
    assert fields["minute"] == "55"
    assert "mon" in fields["day_of_week"] and "fri" in fields["day_of_week"]


def test_scheduler_eod_trigger_next_fire_is_at_14_55_ct():
    broker = PaperAdapter()
    sched = build_scheduler(broker, db=None, tz="America/Chicago")
    trigger = sched.get_job("eod_flatten").trigger
    # On a Wednesday morning CT, next fire is the same day at 14:55 CT
    from zoneinfo import ZoneInfo
    ct = ZoneInfo("America/Chicago")
    morning = datetime(2026, 4, 29, 9, 0, tzinfo=ct)
    next_fire = trigger.get_next_fire_time(None, morning)
    assert next_fire.hour == 14 and next_fire.minute == 55
    assert next_fire.tzinfo is not None


def test_scheduler_flattens_well_before_topstep_3_10_pm_deadline():
    """Sanity guard: scheduler must fire at least 5 minutes before Topstep's 15:10 CT cutoff."""
    broker = PaperAdapter()
    sched = build_scheduler(broker, db=None)
    trigger = sched.get_job("eod_flatten").trigger
    fields = {f.name: str(f) for f in trigger.fields}
    hh = int(fields["hour"])
    mm = int(fields["minute"])
    minutes_before_deadline = (15 * 60 + 10) - (hh * 60 + mm)
    assert minutes_before_deadline >= 5, (
        f"Flatten fires at {hh:02d}:{mm:02d} CT — "
        f"only {minutes_before_deadline} min before Topstep's 15:10 deadline"
    )
