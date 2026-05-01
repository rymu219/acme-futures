"""Deterministic in-memory adapter for unit tests. No network."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

from acme.broker.base import Bar, BracketSpec, Position, Quote, Side


class PaperAdapter:
    def __init__(
        self,
        *,
        contract_id: str = "CON.F.US.MES.M26",
        starting_price: float = 5000.0,
        tick_size: float = 0.25,
    ) -> None:
        self._contract_id = contract_id
        self._price = starting_price
        self._tick_size = tick_size
        self._order_seq = itertools.count(1)
        self._positions: dict[str, int] = {}
        self._avg_price: dict[str, float] = {}
        self._orders: list[dict] = []
        self._user_events: asyncio.Queue[dict] = asyncio.Queue()

    async def authenticate(self) -> None:
        return None

    async def get_account(self) -> dict:
        return {"id": "PAPER-1", "balance": 50_000.0, "canTrade": True}

    async def resolve_contract(self, symbol: str) -> str:
        return self._contract_id

    async def get_bars(
        self,
        contract_id: str,
        *,
        unit: int,
        unit_number: int,
        start: datetime,
        end: datetime,
        limit: int = 20_000,
    ) -> list[Bar]:
        # Deterministic 1-minute bars rising by 1 tick each bar.
        bars: list[Bar] = []
        n = min(limit, max(0, int((end - start).total_seconds() // 60)))
        for i in range(n):
            t = start + timedelta(minutes=i)
            base = self._price + i * self._tick_size
            bars.append(Bar(t=t, o=base, h=base + self._tick_size, l=base, c=base + self._tick_size, v=10))
        return bars

    async def stream_quotes(self, contract_id: str) -> AsyncIterator[Quote]:
        for i in range(5):
            yield Quote(
                t=datetime.now(),
                bid=self._price + i * self._tick_size,
                ask=self._price + (i + 1) * self._tick_size,
                last=self._price + i * self._tick_size,
            )

    async def stream_user_events(self) -> AsyncIterator[dict]:
        while True:
            yield await self._user_events.get()

    async def submit_market_order(
        self,
        contract_id: str,
        side: Side,
        size: int,
        *,
        custom_tag: str | None = None,
        bracket: BracketSpec | None = None,
    ) -> str:
        order_id = f"PAPER-{next(self._order_seq)}"
        signed = size if side == "buy" else -size
        prev = self._positions.get(contract_id, 0)
        new = prev + signed
        self._positions[contract_id] = new
        self._avg_price[contract_id] = self._price
        self._orders.append(
            {"id": order_id, "contract_id": contract_id, "side": side, "size": size, "tag": custom_tag}
        )
        return order_id

    async def cancel_order(self, order_id: str) -> None:
        return None

    async def get_positions(self) -> list[Position]:
        return [
            Position(contract_id=cid, size=sz, avg_price=self._avg_price.get(cid, 0.0))
            for cid, sz in self._positions.items()
            if sz != 0
        ]

    async def flatten_all(self) -> None:
        for cid, sz in list(self._positions.items()):
            if sz == 0:
                continue
            opposite: Side = "sell" if sz > 0 else "buy"
            await self.submit_market_order(cid, opposite, abs(sz), custom_tag="flatten_eod")

    # Test helpers
    @property
    def submitted_orders(self) -> list[dict]:
        return list(self._orders)

    def reset(self) -> None:
        self._positions.clear()
        self._avg_price.clear()
        self._orders.clear()
