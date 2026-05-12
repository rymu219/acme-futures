"""Session-level computation for the BOUNDARY strategy.

For each trade date and contract, compute six levels:

  - PDH / PDL — Previous Day High / Low (RTH session: 08:30-15:00 CT)
  - ONH / ONL — Overnight High / Low (Globex: 17:00 prior-day - 08:30 current)
  - ORH / ORL — Opening Range High / Low (first 30 min of RTH: 08:30-09:00 CT)

CME US equity-index session times (per acme.calendar):
  - RTH:     08:30 CT - 15:00 CT
  - Globex:  17:00 CT (prior day) - 08:30 CT
  - Opening Range: 08:30 CT - 09:00 CT (first 30 min of RTH)

The module is pure: caller supplies a Bars iterator (any tz-aware
datetime). Useful both for retag (offline against Databento parquet)
and for live (incremental updates from the running runtime).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from acme.broker.base import Bar

CT = ZoneInfo("America/Chicago")

# Session boundary times (CT). Match acme.calendar conventions.
RTH_OPEN = time(8, 30)
RTH_CLOSE = time(15, 0)
OPENING_RANGE_END = time(9, 0)
GLOBEX_OPEN = time(17, 0)


@dataclass(frozen=True)
class DayLevels:
    """Per-(date, contract) session level snapshot."""
    trade_date: date
    contract: str
    pdh: float | None
    pdl: float | None
    onh: float | None
    onl: float | None
    orh: float | None
    orl: float | None


def trading_date_ct(ts: datetime) -> date:
    """Return the trade-date label for a UTC/aware datetime.

    The trade date "rolls" at 17:00 CT (Globex open). Bars at 17:30 CT
    Sunday belong to Monday's session.
    """
    ct = ts.astimezone(CT)
    if ct.time() >= GLOBEX_OPEN:
        return ct.date() + timedelta(days=1)
    return ct.date()


def _rth_window(d: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(d, RTH_OPEN, tzinfo=CT),
        datetime.combine(d, RTH_CLOSE, tzinfo=CT),
    )


def _opening_range_window(d: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(d, RTH_OPEN, tzinfo=CT),
        datetime.combine(d, OPENING_RANGE_END, tzinfo=CT),
    )


def _overnight_window(d: date) -> tuple[datetime, datetime]:
    """Globex 17:00 CT (prior day) up to 08:30 CT (current day)."""
    start = datetime.combine(d - timedelta(days=1), GLOBEX_OPEN, tzinfo=CT)
    end = datetime.combine(d, RTH_OPEN, tzinfo=CT)
    return start, end


def _high_low(bars: Iterable[Bar]) -> tuple[float | None, float | None]:
    highs, lows = [], []
    for b in bars:
        highs.append(b.h)
        lows.append(b.l)
    if not highs:
        return None, None
    return max(highs), min(lows)


def compute_day_levels(
    bars: list[Bar], trade_date: date, contract: str = "MES",
) -> DayLevels:
    """Compute six levels for `trade_date` using all supplied bars.

    Bars should cover at least the prior-day RTH (08:30-15:00 CT day-1)
    through the current day's first 30 min of RTH (08:30-09:00 CT).
    Missing slices return None for the affected level pairs.
    """
    # Bucket bars by (UTC) interval intersection — convert each bar.t to CT
    # once and compare via aware datetimes.
    bars_ct = sorted(bars, key=lambda b: b.t)

    prior_day = trade_date - timedelta(days=1)
    pdh_start, pdh_end = _rth_window(prior_day)
    on_start, on_end = _overnight_window(trade_date)
    or_start, or_end = _opening_range_window(trade_date)

    pdh, pdl = _high_low(
        b for b in bars_ct if pdh_start <= b.t.astimezone(CT) < pdh_end
    )
    onh, onl = _high_low(
        b for b in bars_ct if on_start <= b.t.astimezone(CT) < on_end
    )
    orh, orl = _high_low(
        b for b in bars_ct if or_start <= b.t.astimezone(CT) < or_end
    )

    return DayLevels(
        trade_date=trade_date, contract=contract,
        pdh=pdh, pdl=pdl, onh=onh, onl=onl, orh=orh, orl=orl,
    )


def nearest_level_distance(
    price: float, levels: DayLevels, tick_size: float,
) -> tuple[str | None, float | None]:
    """Find the closest non-null level to `price` and return (name, ticks).

    Returns (None, None) if no levels are defined for the day.
    Used by the retag script and by BOUNDARY's entry check.
    """
    candidates: dict[str, float | None] = {
        "pdh": levels.pdh, "pdl": levels.pdl,
        "onh": levels.onh, "onl": levels.onl,
        "orh": levels.orh, "orl": levels.orl,
    }
    valid = [(name, lvl) for name, lvl in candidates.items() if lvl is not None]
    if not valid:
        return None, None
    closest = min(valid, key=lambda kv: abs(price - kv[1]))
    name, lvl = closest
    ticks = (price - lvl) / tick_size
    return name, ticks
