from collections.abc import AsyncIterator
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel

Side = Literal["buy", "sell"]


class Bar(BaseModel):
    t: datetime
    o: float
    h: float
    l: float  # noqa: E741 — standard OHLC field name
    c: float
    v: int


class Quote(BaseModel):
    t: datetime
    bid: float | None = None
    ask: float | None = None
    last: float | None = None


# Aggressor side: "B" = buy aggressor (lifted offer), "A" = sell aggressor (hit bid).
# None = broker did not classify; consumer should fall back to tick rule.
TradeSide = Literal["B", "A"]


class Trade(BaseModel):
    t: datetime
    contract_id: str
    price: float
    size: int
    side: TradeSide | None = None


class Fill(BaseModel):
    order_id: str
    contract_id: str
    side: Side
    size: int
    price: float
    t: datetime


class Position(BaseModel):
    contract_id: str
    size: int
    avg_price: float


class BracketSpec(BaseModel):
    stop_loss_offset_ticks: int | None = None
    take_profit_offset_ticks: int | None = None


class BrokerAdapter(Protocol):
    async def authenticate(self) -> None: ...
    async def get_account(self) -> dict: ...
    async def resolve_contract(self, symbol: str) -> str: ...
    async def get_bars(
        self,
        contract_id: str,
        *,
        unit: int,
        unit_number: int,
        start: datetime,
        end: datetime,
        limit: int = 20_000,
    ) -> list[Bar]: ...
    def stream_quotes(self, contract_id: str) -> AsyncIterator[Quote]: ...
    def stream_trades(self, contract_id: str) -> AsyncIterator[Trade]: ...
    def stream_user_events(self) -> AsyncIterator[dict]: ...
    async def submit_market_order(
        self,
        contract_id: str,
        side: Side,
        size: int,
        *,
        custom_tag: str | None = None,
        bracket: BracketSpec | None = None,
    ) -> str: ...
    async def cancel_order(self, order_id: str) -> None: ...
    async def get_positions(self) -> list[Position]: ...
    async def flatten_all(self) -> None: ...
