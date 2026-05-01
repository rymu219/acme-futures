"""Futures contract registry and front-month resolution.

Phase A scope: MES only. The front-month resolver is calendar-based — CME equity-index
futures roll on a fixed quarterly cycle, so we don't need a chain API to know which
month is active. The ACME_MES_OVERRIDE env var pins a specific contract during edge
cases (holiday roll, manual override).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class FuturesContract:
    symbol: str
    exchange: str
    tick_size: float
    point_value: float
    tick_value: float
    multiplier: int
    quote_currency: str = "USD"


MES = FuturesContract(
    symbol="MES",
    exchange="CME",
    tick_size=0.25,
    point_value=5.0,
    tick_value=1.25,
    multiplier=5,
)

REGISTRY: dict[str, FuturesContract] = {"MES": MES}


# CME equity-index quarterly cycle: H=Mar, M=Jun, U=Sep, Z=Dec
_QUARTERLY: list[tuple[int, str]] = [(3, "H"), (6, "M"), (9, "U"), (12, "Z")]


def _third_friday(year: int, month: int) -> date:
    d = date(year, month, 15)
    while d.weekday() != 4:
        d += timedelta(days=1)
    return d


def front_month_code(today: date | None = None, *, roll_days_before_expiry: int = 8) -> str:
    """Return the active equity-index futures month-code+year for MES.

    Roll heuristic: switch to the next quarterly contract `roll_days_before_expiry`
    calendar days before its 3rd-Friday expiry. Returns e.g. 'M26' for June 2026.
    """
    today = today or date.today()
    candidates: list[tuple[int, int, str]] = []
    for year_offset in (0, 1):
        y = today.year + year_offset
        for month, code in _QUARTERLY:
            candidates.append((y, month, code))
    candidates.sort()
    for y, month, code in candidates:
        expiry = _third_friday(y, month)
        roll = expiry - timedelta(days=roll_days_before_expiry)
        if today < roll:
            return f"{code}{str(y)[-2:]}"
    raise RuntimeError(f"front_month_code: no candidate matched today={today}")


def resolve_mes_contract_id() -> str:
    """ProjectX-style id 'CON.F.US.MES.<MonthCode>', honoring ACME_MES_OVERRIDE."""
    override = os.getenv("ACME_MES_OVERRIDE", "").strip()
    if override:
        return override
    return f"CON.F.US.MES.{front_month_code()}"
