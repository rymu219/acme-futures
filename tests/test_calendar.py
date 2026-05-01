from datetime import date, datetime, time

from acme.calendar import (
    CT,
    DEFAULT_CLOSE_BUFFER_MIN,
    EARLY_CLOSE_0800_2026,
    EARLY_CLOSE_1145_2026,
    FOMC_STATEMENT_DATES_2026,
    FULLY_CLOSED_2026,
    can_trade_now,
    in_econ_blackout,
    is_first_friday,
    schedule_for,
    topstep_trading_date,
)


def _ct(y, mo, d, h=12, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=CT)


# --- topstep_trading_date ---

def test_trading_date_during_day_session():
    # Wednesday 09:00 CT → Wednesday
    assert topstep_trading_date(_ct(2026, 4, 29, 9, 0)) == date(2026, 4, 29)


def test_trading_date_at_topstep_blackout_window():
    # Wednesday 16:00 CT (between 15:10 close and 17:00 resume) → still Wednesday
    assert topstep_trading_date(_ct(2026, 4, 29, 16, 0)) == date(2026, 4, 29)


def test_trading_date_after_17_00_ct_rolls_to_next_day():
    # Wednesday 17:30 CT → Thursday's session
    assert topstep_trading_date(_ct(2026, 4, 29, 17, 30)) == date(2026, 4, 30)


def test_trading_date_late_evening():
    # Wednesday 23:00 CT → Thursday's session
    assert topstep_trading_date(_ct(2026, 4, 29, 23, 0)) == date(2026, 4, 30)


def test_trading_date_requires_tz_aware():
    import pytest
    with pytest.raises(ValueError):
        topstep_trading_date(datetime(2026, 4, 29, 12, 0))


# --- schedule_for ---

def test_schedule_normal_weekday():
    s = schedule_for(date(2026, 4, 29))   # Wednesday
    assert not s.fully_closed
    assert s.enforced_close == time(15, 10)
    assert s.flatten_at == time(14, 55)


def test_schedule_weekend_is_closed():
    s = schedule_for(date(2026, 5, 2))    # Saturday
    assert s.fully_closed


def test_schedule_new_years_fully_closed():
    s = schedule_for(date(2026, 1, 1))
    assert s.fully_closed
    assert date(2026, 1, 1) in FULLY_CLOSED_2026


def test_schedule_christmas_fully_closed():
    assert schedule_for(date(2026, 12, 25)).fully_closed


def test_schedule_thanksgiving_early_close_1145():
    s = schedule_for(date(2026, 11, 26))
    assert not s.fully_closed
    assert s.enforced_close == time(11, 45)
    assert s.flatten_at == time(11, 30)


def test_schedule_day_after_thanksgiving_early_close_1145():
    assert schedule_for(date(2026, 11, 27)).flatten_at == time(11, 30)


def test_schedule_christmas_eve_early_close_1145():
    assert schedule_for(date(2026, 12, 24)).flatten_at == time(11, 30)


def test_schedule_good_friday_0800_close():
    s = schedule_for(date(2026, 4, 3))
    assert not s.fully_closed
    assert s.enforced_close == time(8, 0)
    assert s.flatten_at == time(7, 45)


def test_buffer_is_15_minutes():
    assert DEFAULT_CLOSE_BUFFER_MIN == 15


# --- econ events ---

def test_first_friday_detector():
    assert is_first_friday(date(2026, 5, 1))     # Friday May 1
    assert is_first_friday(date(2026, 6, 5))     # Friday June 5
    assert not is_first_friday(date(2026, 5, 8)) # Friday May 8 (2nd)
    assert not is_first_friday(date(2026, 4, 30))  # Thursday


def test_nfp_blackout_first_friday_725_to_800():
    # Friday May 1 2026 — first Friday of May
    assert in_econ_blackout(_ct(2026, 5, 1, 7, 25)) == (True, "nfp_blackout")
    assert in_econ_blackout(_ct(2026, 5, 1, 7, 45)) == (True, "nfp_blackout")
    assert in_econ_blackout(_ct(2026, 5, 1, 7, 59)) == (True, "nfp_blackout")
    assert in_econ_blackout(_ct(2026, 5, 1, 8, 0))[0] is False
    assert in_econ_blackout(_ct(2026, 5, 1, 7, 24))[0] is False


def test_nfp_blackout_does_not_fire_on_non_first_friday():
    assert in_econ_blackout(_ct(2026, 5, 8, 7, 30))[0] is False


def test_fomc_blackout_fires_only_on_scheduled_dates():
    # April 29 2026 is on the FOMC list
    assert date(2026, 4, 29) in FOMC_STATEMENT_DATES_2026
    assert in_econ_blackout(_ct(2026, 4, 29, 12, 55)) == (True, "fomc_blackout")
    assert in_econ_blackout(_ct(2026, 4, 29, 13, 15)) == (True, "fomc_blackout")
    assert in_econ_blackout(_ct(2026, 4, 29, 13, 30))[0] is False
    # April 28 is not an FOMC day
    assert in_econ_blackout(_ct(2026, 4, 28, 13, 0))[0] is False


# --- can_trade_now ---

def test_can_trade_during_normal_morning():
    allowed, _ = can_trade_now(_ct(2026, 4, 29, 9, 0))
    assert allowed


def test_cannot_trade_on_fully_closed_holiday():
    allowed, reason = can_trade_now(_ct(2026, 1, 1, 10, 0))
    assert not allowed and reason == "market_closed_today"


def test_cannot_trade_during_topstep_daily_blackout():
    # Between 15:10 and 17:00 CT
    allowed, reason = can_trade_now(_ct(2026, 4, 29, 16, 0))
    assert not allowed and "blackout" in reason


def test_cannot_trade_past_todays_flatten_time():
    # 14:55 is the flatten time for normal days
    allowed, reason = can_trade_now(_ct(2026, 4, 29, 14, 56))
    assert not allowed and "past_today_flatten" in reason


def test_cannot_trade_past_holiday_flatten_time():
    # Thanksgiving — flatten is 11:30. At 11:35 should block.
    allowed, reason = can_trade_now(_ct(2026, 11, 26, 11, 35))
    assert not allowed and "past_today_flatten" in reason


def test_cannot_trade_during_nfp():
    allowed, reason = can_trade_now(_ct(2026, 5, 1, 7, 30))
    assert not allowed and reason == "nfp_blackout"


def test_cannot_trade_during_fomc():
    allowed, reason = can_trade_now(_ct(2026, 4, 29, 13, 0))
    assert not allowed and reason == "fomc_blackout"


def test_can_trade_in_evening_session_after_17_00():
    """At 20:00 CT Wednesday, we're in Thursday's session. Should be allowed."""
    allowed, reason = can_trade_now(_ct(2026, 4, 29, 20, 0))
    assert allowed, f"unexpected block: {reason}"


def test_can_trade_at_18_30_ct_overnight():
    """6:30 PM CT Wednesday — well past today's 14:55 flatten,
    but in tomorrow's session. Should be allowed.
    """
    allowed, reason = can_trade_now(_ct(2026, 4, 29, 18, 30))
    assert allowed, f"unexpected block: {reason}"


def test_can_trade_at_03_00_ct_overnight():
    """3:00 AM CT Thursday — middle of overnight session, well before Thursday's flatten."""
    allowed, _ = can_trade_now(_ct(2026, 4, 30, 3, 0))
    assert allowed


def test_evening_session_respects_next_day_holiday_close():
    """At 20:00 CT on Christmas Eve (already in Christmas Day's session),
    Christmas Day is fully closed → blocked.
    """
    allowed, reason = can_trade_now(_ct(2026, 12, 24, 20, 0))
    assert not allowed and reason == "market_closed_today"


def test_evening_session_respects_next_day_holiday_flatten():
    """At 20:00 CT on Wed Nov 25 (Thanksgiving Eve, in Thanksgiving's session),
    we're WAY before Thanksgiving's 11:30 flatten time, so allowed.
    """
    allowed, _ = can_trade_now(_ct(2026, 11, 25, 20, 0))
    assert allowed


def test_holiday_constants_have_no_overlap():
    """No date should be in both fully-closed and any early-close set."""
    early = EARLY_CLOSE_0800_2026 | EARLY_CLOSE_1145_2026
    assert not (FULLY_CLOSED_2026 & early)
    assert not (EARLY_CLOSE_0800_2026 & EARLY_CLOSE_1145_2026)
