"""Topstep-aware calendar helpers.

Encodes:
  - Topstep's trading-day boundary (17:00 CT prev day → 15:10 CT current day)
  - 2026 US holidays as Topstep observes them (full-close vs early-close)
  - Per-day "effective close" enforcement time and our chosen flatten time
  - Predictable economic-event blackouts (NFP monthly, FOMC scheduled meetings)
  - "Should we be trading right now?" gate

Sources:
  - https://help.topstep.com/en/articles/8284206-when-and-what-products-can-i-trade
  - https://help.topstep.com/en/articles/13350348-topstep-holiday-trading-hours
  - https://help.topstep.com/en/articles/8284211-what-are-economic-releases

Hardcoded for 2026. Update the constants below for subsequent years, or migrate to
the `holidays` package if maintaining annually becomes a burden.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")

# Topstep's normal day enforces flat by 15:10 CT and resumes at 17:00 CT.
NORMAL_CLOSE_HHMM: tuple[int, int] = (15, 10)
NORMAL_RESUME_HHMM: tuple[int, int] = (17, 0)

# Our chosen buffer before Topstep's enforced close (minutes).
DEFAULT_CLOSE_BUFFER_MIN = 15

# 2026 US holidays per Topstep's published schedule.
# https://help.topstep.com/en/articles/13350348-topstep-holiday-trading-hours
FULLY_CLOSED_2026: frozenset[date] = frozenset({
    date(2026, 1, 1),    # New Year's Day
    date(2026, 12, 25),  # Christmas Day
})

# Holidays where Topstep enforces a 11:45 CT close.
EARLY_CLOSE_1145_2026: frozenset[date] = frozenset({
    date(2026, 1, 19),   # MLK Day
    date(2026, 2, 16),   # President's Day
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day observed (Jul 4 is Saturday)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 11, 27),  # Day after Thanksgiving
    date(2026, 12, 24),  # Christmas Eve
})

# Good Friday closes at 08:00 CT.
EARLY_CLOSE_0800_2026: frozenset[date] = frozenset({
    date(2026, 4, 3),
})

# Scheduled FOMC statement-release dates for 2026 (statement at 13:00 CT).
# Source: Federal Reserve calendar (verify annually).
FOMC_STATEMENT_DATES_2026: frozenset[date] = frozenset({
    date(2026, 1, 28),
    date(2026, 3, 18),
    date(2026, 4, 29),
    date(2026, 6, 17),
    date(2026, 7, 29),
    date(2026, 9, 16),
    date(2026, 10, 28),
    date(2026, 12, 9),
})


@dataclass(frozen=True)
class DaySchedule:
    """What the trading day looks like for a given date."""
    fully_closed: bool
    enforced_close: time | None      # Topstep's hard close enforcement (CT)
    flatten_at: time | None          # Our scheduled flatten time (CT, with buffer)


def topstep_trading_date(now_ct: datetime) -> date:
    """Topstep's trading day runs 17:00 CT prev day → 15:10 CT current day.

    Returns the trading-date label for `now_ct`. After 17:00 CT, it's tomorrow's session.
    Between 15:10 CT and 17:00 CT, no trading is allowed; we still return the calendar
    date for state-tracking purposes (the gap belongs to the day that just ended).
    """
    if now_ct.tzinfo is None:
        raise ValueError("now_ct must be timezone-aware (CT)")
    if now_ct.hour < 17:
        return now_ct.date()
    return now_ct.date() + timedelta(days=1)


def schedule_for(d: date, *, buffer_min: int = DEFAULT_CLOSE_BUFFER_MIN) -> DaySchedule:
    """Day schedule for a given calendar date in CT.

    Weekends → fully closed.
    Holidays → per the constants above.
    Otherwise → normal 15:10 CT close, flatten at (15:10 - buffer_min).
    """
    if d.weekday() >= 5 or d in FULLY_CLOSED_2026:
        return DaySchedule(fully_closed=True, enforced_close=None, flatten_at=None)
    if d in EARLY_CLOSE_0800_2026:
        return DaySchedule(
            fully_closed=False,
            enforced_close=time(8, 0),
            flatten_at=_minus_minutes(time(8, 0), buffer_min),
        )
    if d in EARLY_CLOSE_1145_2026:
        return DaySchedule(
            fully_closed=False,
            enforced_close=time(11, 45),
            flatten_at=_minus_minutes(time(11, 45), buffer_min),
        )
    return DaySchedule(
        fully_closed=False,
        enforced_close=time(*NORMAL_CLOSE_HHMM),
        flatten_at=_minus_minutes(time(*NORMAL_CLOSE_HHMM), buffer_min),
    )


def _minus_minutes(t: time, minutes: int) -> time:
    base = datetime(2000, 1, 1, t.hour, t.minute)
    base -= timedelta(minutes=minutes)
    return time(base.hour, base.minute)


def is_first_friday(d: date) -> bool:
    return d.weekday() == 4 and d.day <= 7


def in_econ_blackout(now_ct: datetime) -> tuple[bool, str]:
    """Returns (in_blackout, reason).

    Blackouts:
      - NFP (1st Friday of month, 07:25–08:00 CT)
      - FOMC statement (scheduled dates, 12:55–13:30 CT)
    """
    if now_ct.tzinfo is None:
        raise ValueError("now_ct must be timezone-aware (CT)")
    d = now_ct.date()
    t = now_ct.time()
    if is_first_friday(d) and time(7, 25) <= t < time(8, 0):
        return True, "nfp_blackout"
    if d in FOMC_STATEMENT_DATES_2026 and time(12, 55) <= t < time(13, 30):
        return True, "fomc_blackout"
    return False, ""


def can_trade_now(now_ct: datetime) -> tuple[bool, str]:
    """Top-level pre-trade gate combining session, holiday, and econ-event checks.

    Topstep's "trading day" runs 17:00 CT prev day → 15:10 CT current day. So at
    20:00 CT Wednesday we're inside *Thursday's* session — the gate must check
    Thursday's holiday/flatten rules, not Wednesday's.

    Returns (allowed, reason). Reason is empty if allowed.
    """
    if now_ct.tzinfo is None:
        raise ValueError("now_ct must be timezone-aware (CT)")
    t = now_ct.time()
    # Daily Topstep blackout window (15:10–17:00 CT) — applies to wall-clock time.
    if time(*NORMAL_CLOSE_HHMM) <= t < time(*NORMAL_RESUME_HHMM):
        return False, "topstep_daily_blackout_15_10_to_17_00"
    # Today's *trading* date (after 17:00 CT this rolls to tomorrow).
    trading_date = topstep_trading_date(now_ct)
    sched = schedule_for(trading_date)
    if sched.fully_closed:
        return False, "market_closed_today"
    # "Past today's flatten" only applies during the day-session phase, i.e. when
    # the calendar date matches the trading date. In the evening phase
    # (17:00 CT onward), we belong to tomorrow's session and tomorrow's flatten
    # is still hours away.
    if (
        now_ct.date() == trading_date
        and sched.flatten_at is not None
        and t >= sched.flatten_at
    ):
        return False, f"past_today_flatten_{sched.flatten_at.strftime('%H:%M')}"
    blackout, reason = in_econ_blackout(now_ct)
    if blackout:
        return False, reason
    return True, ""
