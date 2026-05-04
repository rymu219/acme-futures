"""Live 2-minute bar + cum-delta builder for the v3 runtime.

Two input modes:
  (a) Quote-driven (price-only) tick rule. Each new last-trade price tick
      classifies as +1 (price up vs prior) or -1 (price down vs prior).
      Used when the broker only exposes quotes and not trades.
      Validated proxy correlation 0.90 at the BAR level when size weighting
      is preserved; with unit weight the correlation is weaker.
  (b) Trade-stream-driven. Each (price, size, side) tuple goes in directly.
      side='B' (buy aggressor) → +size, side='A' (sell aggressor) → -size.
      Used when the broker exposes a trades hub. Matches OOS methodology
      exactly.

Either way the builder emits closed 2-min bars with delta + cum_delta_session
that the v3 engine consumes. cum_delta resets at 08:30 CT (session open).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from typing import Literal

from acme.broker.base import Bar

CT = timezone(timedelta(hours=-6))   # America/Chicago, ignoring DST shifts
SESSION_OPEN_CT = dtime(8, 30)


@dataclass(frozen=True)
class BarWithDelta:
    bar: Bar
    delta: int                  # bar's signed flow
    cum_delta_session: int      # session-cumulative since 08:30 CT


def _bucket_floor_2m(t: datetime) -> datetime:
    minute = (t.minute // 2) * 2
    return t.replace(minute=minute, second=0, microsecond=0)


def _session_anchor_ct(t_any: datetime) -> datetime:
    """Most recent 08:30 CT for any tz-aware datetime."""
    t_ct = t_any.astimezone(CT)
    today_open = t_ct.replace(hour=SESSION_OPEN_CT.hour,
                              minute=SESSION_OPEN_CT.minute,
                              second=0, microsecond=0)
    if t_ct >= today_open:
        return today_open
    return today_open - timedelta(days=1)


class LiveBarDeltaBuilder:
    """Builds 2-minute bars with delta. Caller drives via add_trade_or_tick().

    NOT thread-safe. Caller serializes all input on a single async loop.
    """

    def __init__(
        self,
        *,
        on_bar: Callable[[BarWithDelta], None],
    ) -> None:
        self._on_bar = on_bar
        self._bucket_start: datetime | None = None
        self._open: float | None = None
        self._high: float = float("-inf")
        self._low: float = float("inf")
        self._close: float = 0.0
        self._volume: int = 0
        self._delta: int = 0
        self._n: int = 0
        self._last_price: float | None = None
        self._last_dir: int = 0    # carried for tick-rule price-equal ties
        # Session-state for cum_delta reset at 08:30 CT
        self._session_anchor: datetime | None = None
        self._cum_delta_session: int = 0

    def add_quote_tick(self, t: datetime, last_price: float) -> None:
        """Quote-mode input. We don't know aggressor or size, so unit-weight
        tick rule: +1 if price > prior, -1 if price < prior, carry-forward
        sign on equal price. Use only when broker.stream_trades isn't wired."""
        if self._last_price is None:
            sign = 0
        elif last_price > self._last_price:
            sign = 1
            self._last_dir = 1
        elif last_price < self._last_price:
            sign = -1
            self._last_dir = -1
        else:
            sign = self._last_dir   # tie: carry forward
        self._last_price = last_price
        self._ingest(t=t, price=last_price, size=1, signed_size=sign)

    def add_trade(
        self,
        t: datetime,
        price: float,
        size: int,
        *,
        side: Literal["B", "A"] | None = None,
    ) -> None:
        """Trade-mode input. If side is None, falls back to tick rule on
        price (still size-weighted)."""
        if side == "B":
            signed = size
        elif side == "A":
            signed = -size
        else:
            # tick rule + size weighting
            if self._last_price is None:
                signed = 0
            elif price > self._last_price:
                signed = size
                self._last_dir = 1
            elif price < self._last_price:
                signed = -size
                self._last_dir = -1
            else:
                signed = size * self._last_dir
        self._last_price = price
        self._ingest(t=t, price=price, size=size, signed_size=signed)

    # ---------- internal ----------

    def _maybe_reset_session(self, t: datetime) -> None:
        anchor = _session_anchor_ct(t)
        if self._session_anchor != anchor:
            self._session_anchor = anchor
            self._cum_delta_session = 0

    def _ingest(self, *, t: datetime, price: float, size: int,
                signed_size: int) -> None:
        bucket = _bucket_floor_2m(t)
        if self._bucket_start is None:
            self._bucket_start = bucket
            self._open_bar(price)
        elif bucket != self._bucket_start:
            # Close prior bucket, open new one
            self._close_bar()
            self._bucket_start = bucket
            self._open_bar(price)
        # Accumulate
        self._high = max(self._high, price)
        self._low = min(self._low, price)
        self._close = price
        self._volume += size
        self._delta += signed_size
        self._n += 1

    def _open_bar(self, price: float) -> None:
        self._open = price
        self._high = price
        self._low = price
        self._close = price
        self._volume = 0
        self._delta = 0
        self._n = 0

    def _close_bar(self) -> None:
        if self._n == 0 or self._open is None or self._bucket_start is None:
            return
        bar = Bar(
            t=self._bucket_start, o=self._open, h=self._high,
            l=self._low, c=self._close, v=self._volume,
        )
        # Reset session cum_delta if we crossed 08:30 CT
        self._maybe_reset_session(self._bucket_start)
        self._cum_delta_session += self._delta
        self._on_bar(BarWithDelta(
            bar=bar, delta=self._delta,
            cum_delta_session=self._cum_delta_session,
        ))

    def force_close_current(self) -> None:
        """Emit the in-progress bar (if any) immediately. Used at shutdown
        or when the caller knows no more ticks will arrive in this bucket."""
        if self._n > 0:
            self._close_bar()
            self._open = None
            self._n = 0
            self._bucket_start = None
