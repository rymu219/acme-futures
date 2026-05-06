"""ProjectX (TopstepX) REST + SignalR broker adapter.

Endpoints:
  POST /api/Auth/loginKey            — auth, returns JWT
  POST /api/Auth/validate            — refresh JWT
  POST /api/Account/search           — list accounts (id, balance, canTrade)
  POST /api/Contract/search          — resolve symbol -> contract id
  POST /api/History/retrieveBars     — historical bars
  POST /api/Order/place              — submit order (with optional bracket)
  POST /api/Order/cancel             — cancel by orderId
  POST /api/Position/searchOpen      — open positions

SignalR hubs:
  rtc.topstepx.com/hubs/user         — fills, order status, balance
  rtc.topstepx.com/hubs/market       — quotes, trades, depth

JWTs expire in 24h. We refresh proactively on first 401.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from acme.broker.base import Bar, BracketSpec, BrokerAdapter, Position, Quote, Side, Trade

log = structlog.get_logger(__name__)

_SIDE_TO_INT: dict[Side, int] = {"buy": 0, "sell": 1}
_ORDER_TYPE_MARKET = 2


class ProjectXError(Exception):
    pass


class ProjectXAdapter(BrokerAdapter):
    def __init__(
        self,
        username: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        user_hub: str | None = None,
        market_hub: str | None = None,
        account_id: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.username = username or os.environ["PROJECTX_USERNAME"]
        self.api_key = api_key or os.environ["PROJECTX_API_KEY"]
        self.base_url = base_url or os.getenv("PROJECTX_BASE_URL", "https://api.topstepx.com")
        self.user_hub = user_hub or os.getenv(
            "PROJECTX_USER_HUB", "https://rtc.topstepx.com/hubs/user"
        )
        self.market_hub = market_hub or os.getenv(
            "PROJECTX_MARKET_HUB", "https://rtc.topstepx.com/hubs/market"
        )
        self._account_id: str | None = account_id or os.getenv("PROJECTX_ACCOUNT_ID") or None
        self._token: str | None = None
        self._client = client or httpx.AsyncClient(base_url=self.base_url, timeout=20.0)
        # SignalR fan-out muxes — created lazily on first stream_* call.
        # ProjectX rejects a second concurrent SignalR connection on the same
        # JWT, so we MUST share connections across all callers.
        self._market_mux: _MarketHubMux | None = None
        self._user_mux: _UserHubMux | None = None

    async def aclose(self) -> None:
        if self._market_mux is not None:
            self._market_mux.stop()
        if self._user_mux is not None:
            self._user_mux.stop()
        await self._client.aclose()

    async def __aenter__(self) -> ProjectXAdapter:
        await self.authenticate()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()

    # ---------- auth ----------

    async def authenticate(self) -> None:
        r = await self._client.post(
            "/api/Auth/loginKey",
            json={"userName": self.username, "apiKey": self.api_key},
        )
        r.raise_for_status()
        body = r.json()
        token = body.get("token") or body.get("Token")
        if not token:
            raise ProjectXError(f"loginKey returned no token: {body}")
        self._token = token

    async def _ensure_auth(self) -> None:
        if not self._token:
            await self.authenticate()

    @retry(
        retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError)),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        reraise=True,
    )
    async def _post(self, path: str, json: dict | None = None) -> dict:
        await self._ensure_auth()
        r = await self._client.post(
            path,
            json=json,
            headers={"Authorization": f"Bearer {self._token}"},
        )
        if r.status_code == 401:
            self._token = None
            await self.authenticate()
            r = await self._client.post(
                path,
                json=json,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        if r.status_code == 429:
            retry_after = float(r.headers.get("Retry-After", "1"))
            await asyncio.sleep(retry_after)
            r.raise_for_status()
        r.raise_for_status()
        return r.json()

    # ---------- account ----------

    async def get_account(self) -> dict:
        body = await self._post("/api/Account/search", {"onlyActiveAccounts": True})
        accounts = body.get("accounts") or body.get("Accounts") or []
        if not accounts:
            raise ProjectXError(f"no accounts in /Account/search response: {body}")
        if self._account_id:
            for a in accounts:
                if str(a.get("id") or a.get("Id")) == str(self._account_id):
                    return a
        first = accounts[0]
        self._account_id = str(first.get("id") or first.get("Id"))
        return first

    @property
    def account_id(self) -> str:
        if not self._account_id:
            raise ProjectXError("account_id not set; call get_account() first")
        return self._account_id

    # ---------- contracts ----------

    async def resolve_contract(self, symbol: str) -> str:
        override = os.getenv("ACME_MES_OVERRIDE", "").strip() if symbol == "MES" else ""
        if override:
            return override
        body = await self._post(
            "/api/Contract/search",
            {"live": False, "searchText": symbol},
        )
        contracts = body.get("contracts") or body.get("Contracts") or []
        active = [c for c in contracts if c.get("activeContract") or c.get("ActiveContract")]
        if not active:
            raise ProjectXError(f"no activeContract for symbol={symbol}: {body}")
        return str(active[0].get("id") or active[0].get("Id"))

    # ---------- bars ----------

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
        body = await self._post(
            "/api/History/retrieveBars",
            {
                "contractId": contract_id,
                "live": False,
                "startTime": start.isoformat(),
                "endTime": end.isoformat(),
                "unit": unit,
                "unitNumber": unit_number,
                "limit": limit,
                "includePartialBar": False,
            },
        )
        rows = body.get("bars") or body.get("Bars") or []
        return [
            Bar(
                t=datetime.fromisoformat((r.get("t") or r.get("Time")).replace("Z", "+00:00")),
                o=float(r.get("o") or r.get("Open")),
                h=float(r.get("h") or r.get("High")),
                l=float(r.get("l") or r.get("Low")),
                c=float(r.get("c") or r.get("Close")),
                v=int(r.get("v") or r.get("Volume") or 0),
            )
            for r in rows
        ]

    # ---------- orders ----------

    async def submit_market_order(
        self,
        contract_id: str,
        side: Side,
        size: int,
        *,
        custom_tag: str | None = None,
        bracket: BracketSpec | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "accountId": self.account_id,
            "contractId": contract_id,
            "type": _ORDER_TYPE_MARKET,
            "side": _SIDE_TO_INT[side],
            "size": int(size),
        }
        if custom_tag:
            payload["customTag"] = custom_tag
        if bracket:
            if bracket.stop_loss_offset_ticks is not None:
                payload["stopLossBracket"] = {"offsetTicks": bracket.stop_loss_offset_ticks}
            if bracket.take_profit_offset_ticks is not None:
                payload["takeProfitBracket"] = {"offsetTicks": bracket.take_profit_offset_ticks}
        body = await self._post("/api/Order/place", payload)
        order_id = body.get("orderId") or body.get("OrderId") or body.get("id")
        if order_id is None:
            raise ProjectXError(f"/Order/place returned no orderId: {body}")
        return str(order_id)

    async def cancel_order(self, order_id: str) -> None:
        await self._post(
            "/api/Order/cancel",
            {"accountId": self.account_id, "orderId": order_id},
        )

    # ---------- positions ----------

    async def get_positions(self) -> list[Position]:
        body = await self._post(
            "/api/Position/searchOpen",
            {"accountId": self.account_id},
        )
        rows = body.get("positions") or body.get("Positions") or []
        out: list[Position] = []
        for r in rows:
            size = int(r.get("size") or r.get("Size") or 0)
            if size == 0:
                continue
            out.append(
                Position(
                    contract_id=str(r.get("contractId") or r.get("ContractId")),
                    size=size,
                    avg_price=float(r.get("averagePrice") or r.get("AveragePrice") or 0.0),
                )
            )
        return out

    async def flatten_all(self) -> None:
        positions = await self.get_positions()
        for p in positions:
            opposite: Side = "sell" if p.size > 0 else "buy"
            try:
                await self.submit_market_order(
                    p.contract_id,
                    opposite,
                    abs(p.size),
                    custom_tag="flatten_eod",
                )
            except Exception as e:
                log.error("flatten_failed", contract_id=p.contract_id, error=str(e))

    # ---------- streaming (SignalR) ----------
    # ProjectX rejects a second concurrent SignalR connection on the same JWT
    # (we hit this in PR #6). Streaming is funneled through two muxes that
    # each open ONE connection and fan out to N consumer queues. Each call
    # to stream_quotes / stream_trades / stream_user_events returns its own
    # async iterator; under the hood they share connections.

    async def stream_quotes(self, contract_id: str) -> AsyncIterator[Quote]:  # type: ignore[override]
        await self._ensure_auth()
        if self._market_mux is None:
            self._market_mux = _MarketHubMux(self.market_hub, lambda: self._token)
        async for q in self._market_mux.subscribe_quotes(contract_id):
            yield q

    async def stream_trades(self, contract_id: str) -> AsyncIterator[Trade]:  # type: ignore[override]
        await self._ensure_auth()
        if self._market_mux is None:
            self._market_mux = _MarketHubMux(self.market_hub, lambda: self._token)
        async for tr in self._market_mux.subscribe_trades(contract_id):
            yield tr

    async def stream_user_events(self) -> AsyncIterator[dict]:  # type: ignore[override]
        await self._ensure_auth()
        if self._user_mux is None:
            self._user_mux = _UserHubMux(self.user_hub, lambda: self._token, self.account_id)
        async for evt in self._user_mux.subscribe():
            yield evt


# ---------- SignalR fan-out muxes ----------
# Each mux owns ONE connection per hub. Consumers register a queue and read
# from it; the mux's signalrcore callbacks fan out incoming events to all
# registered queues. Callbacks run in signalrcore's thread, so we use
# loop.call_soon_threadsafe to safely hand work back to asyncio.


def _extract_dict_from_args(args: list[Any]) -> dict | None:
    """ProjectX hubs may deliver [dict], [str_id, dict], [str_id, json_str]."""
    for a in args or []:
        if isinstance(a, dict):
            return a
        if isinstance(a, str) and a.startswith("{"):
            import json
            try:
                return json.loads(a)
            except Exception:
                continue
    return None


def _extract_contract_id_from_args(args: list[Any]) -> str | None:
    """Pulls a non-JSON string id out of the args. Returns None if absent."""
    for a in args or []:
        if isinstance(a, str) and not a.startswith("{"):
            return a
    return None


def _coerce_trade_side(payload: dict) -> str | None:
    raw = (
        payload.get("side")
        or payload.get("Side")
        or payload.get("aggressor")
        or payload.get("Aggressor")
        or payload.get("type")
        or payload.get("Type")
    )
    if raw is None:
        return None
    if isinstance(raw, str):
        upper = raw.upper()
        if upper in ("B", "BUY"):
            return "B"
        if upper in ("A", "S", "SELL", "ASK"):
            return "A"
        return None
    if isinstance(raw, int | float):
        if int(raw) == 0:
            return "B"
        if int(raw) == 1:
            return "A"
    return None


class _MarketHubMux:
    """One SignalR connection to the market hub, fanned out across N
    consumers per (contract_id, kind) tuple. Lazily started on first
    subscribe; lives until the adapter is closed.
    """

    def __init__(self, market_hub_url: str, token_provider: Any) -> None:
        self._market_hub_url = market_hub_url
        self._token_provider = token_provider  # callable -> str | None
        self._connection: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started: bool = False
        self._start_lock = asyncio.Lock()
        # consumer queues, keyed by contract_id
        self._quote_consumers: dict[str, list[asyncio.Queue[Quote]]] = {}
        self._trade_consumers: dict[str, list[asyncio.Queue[Trade]]] = {}
        # subscriptions already sent to the hub
        self._subscribed_quotes: set[str] = set()
        self._subscribed_trades: set[str] = set()
        # rate-limit log spam on bad parses
        self._err_count: int = 0

    async def _ensure_started(self) -> None:
        async with self._start_lock:
            if self._started:
                return
            from signalrcore.hub_connection_builder import HubConnectionBuilder  # type: ignore
            self._loop = asyncio.get_running_loop()
            token = self._token_provider() or ""
            self._connection = (
                HubConnectionBuilder()
                .with_url(
                    f"{self._market_hub_url}?access_token={token}",
                    options={"access_token_factory": lambda: self._token_provider() or ""},
                )
                .with_automatic_reconnect(
                    {"type": "raw", "keep_alive_interval": 10, "reconnect_interval": 5}
                )
                .build()
            )
            self._connection.on("GatewayQuote", self._on_quote)
            self._connection.on("GatewayTrade", self._on_trade)
            self._connection.start()
            await asyncio.sleep(1.0)  # signalrcore handshake
            self._started = True
            log.info("market_hub_mux_started")

    def _on_quote(self, args: list[Any]) -> None:
        try:
            payload = _extract_dict_from_args(args)
            if payload is None:
                if self._err_count < 3:
                    log.error("market_mux_quote_no_dict", args_repr=repr(args)[:200])
                    self._err_count += 1
                return
            cid = _extract_contract_id_from_args(args)
            bid = payload.get("bestBid") or payload.get("bid")
            ask = payload.get("bestAsk") or payload.get("ask")
            last = payload.get("lastPrice") or payload.get("last")
            ts = payload.get("timestamp") or payload.get("t")
            t = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else datetime.now()
            q = Quote(
                t=t,
                bid=float(bid) if bid is not None else None,
                ask=float(ask) if ask is not None else None,
                last=float(last) if last is not None else None,
            )
            self._fanout_quote(cid, q)
        except Exception as e:
            if self._err_count < 3:
                log.error("market_mux_quote_failed", error=str(e), args_repr=repr(args)[:200])
                self._err_count += 1

    def _fanout_quote(self, cid: str | None, q: Quote) -> None:
        if self._loop is None:
            return
        # If contract_id is unknown (some shapes), fan out to ALL subscribed lists.
        if cid and cid in self._quote_consumers:
            consumers = list(self._quote_consumers[cid])
        elif cid is None:
            consumers = []
            for qs in self._quote_consumers.values():
                consumers.extend(qs)
        else:
            return
        for queue in consumers:
            self._loop.call_soon_threadsafe(queue.put_nowait, q)

    def _on_trade(self, args: list[Any]) -> None:
        try:
            payload = _extract_dict_from_args(args)
            if payload is None:
                if self._err_count < 3:
                    log.error("market_mux_trade_no_dict", args_repr=repr(args)[:200])
                    self._err_count += 1
                return
            cid = _extract_contract_id_from_args(args)
            price = payload.get("price") or payload.get("Price") or payload.get("lastPrice")
            size = (
                payload.get("size") or payload.get("Size")
                or payload.get("volume") or payload.get("Volume")
            )
            ts = payload.get("timestamp") or payload.get("Timestamp") or payload.get("t")
            if price is None or size is None:
                return
            t = (
                datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if isinstance(ts, str) else datetime.now()
            )
            tr = Trade(
                t=t,
                contract_id=cid or "",
                price=float(price),
                size=int(size),
                side=_coerce_trade_side(payload),  # type: ignore[arg-type]
            )
            self._fanout_trade(cid, tr)
        except Exception as e:
            if self._err_count < 3:
                log.error("market_mux_trade_failed", error=str(e), args_repr=repr(args)[:200])
                self._err_count += 1

    def _fanout_trade(self, cid: str | None, tr: Trade) -> None:
        if self._loop is None:
            return
        if cid and cid in self._trade_consumers:
            consumers = list(self._trade_consumers[cid])
        elif cid is None:
            consumers = []
            for ts in self._trade_consumers.values():
                consumers.extend(ts)
        else:
            return
        for queue in consumers:
            self._loop.call_soon_threadsafe(queue.put_nowait, tr)

    async def subscribe_quotes(self, contract_id: str) -> AsyncIterator[Quote]:
        await self._ensure_started()
        consumers = self._quote_consumers.setdefault(contract_id, [])
        if contract_id not in self._subscribed_quotes:
            with contextlib.suppress(Exception):
                self._connection.send("SubscribeContractQuotes", [contract_id])
            self._subscribed_quotes.add(contract_id)
        my_queue: asyncio.Queue[Quote] = asyncio.Queue()
        consumers.append(my_queue)
        log.info("market_mux_quote_subscriber_added", contract_id=contract_id,
                 total_consumers=len(consumers))
        try:
            while True:
                yield await my_queue.get()
        finally:
            with contextlib.suppress(ValueError):
                consumers.remove(my_queue)
            if not consumers and contract_id in self._subscribed_quotes:
                with contextlib.suppress(Exception):
                    self._connection.send("UnsubscribeContractQuotes", [contract_id])
                self._subscribed_quotes.discard(contract_id)

    async def subscribe_trades(self, contract_id: str) -> AsyncIterator[Trade]:
        await self._ensure_started()
        consumers = self._trade_consumers.setdefault(contract_id, [])
        if contract_id not in self._subscribed_trades:
            with contextlib.suppress(Exception):
                self._connection.send("SubscribeContractTrades", [contract_id])
            self._subscribed_trades.add(contract_id)
        my_queue: asyncio.Queue[Trade] = asyncio.Queue()
        consumers.append(my_queue)
        log.info("market_mux_trade_subscriber_added", contract_id=contract_id,
                 total_consumers=len(consumers))
        try:
            while True:
                yield await my_queue.get()
        finally:
            with contextlib.suppress(ValueError):
                consumers.remove(my_queue)
            if not consumers and contract_id in self._subscribed_trades:
                with contextlib.suppress(Exception):
                    self._connection.send("UnsubscribeContractTrades", [contract_id])
                self._subscribed_trades.discard(contract_id)

    def stop(self) -> None:
        if self._connection is not None and self._started:
            with contextlib.suppress(Exception):
                self._connection.stop()
            self._started = False


class _UserHubMux:
    """One SignalR connection to the user hub, fanned out across N consumers.
    The user hub is per-account (we use one account), so all consumers see
    the same stream of order/fill/position/account events.
    """

    def __init__(self, user_hub_url: str, token_provider: Any, account_id: str) -> None:
        self._user_hub_url = user_hub_url
        self._token_provider = token_provider
        self._account_id = account_id
        self._connection: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started: bool = False
        self._start_lock = asyncio.Lock()
        self._consumers: list[asyncio.Queue[dict]] = []
        self._err_count: int = 0

    def _emit(self, kind: str):
        def handler(args: list[Any]) -> None:
            payload: Any = _extract_dict_from_args(args)
            if payload is None:
                payload = args
            evt = {"kind": kind, "payload": payload}
            if self._loop is None:
                return
            for queue in list(self._consumers):
                self._loop.call_soon_threadsafe(queue.put_nowait, evt)
        return handler

    async def _ensure_started(self) -> None:
        async with self._start_lock:
            if self._started:
                return
            from signalrcore.hub_connection_builder import HubConnectionBuilder  # type: ignore
            self._loop = asyncio.get_running_loop()
            token = self._token_provider() or ""
            self._connection = (
                HubConnectionBuilder()
                .with_url(
                    f"{self._user_hub_url}?access_token={token}",
                    options={"access_token_factory": lambda: self._token_provider() or ""},
                )
                .with_automatic_reconnect(
                    {"type": "raw", "keep_alive_interval": 10, "reconnect_interval": 5}
                )
                .build()
            )
            self._connection.on("GatewayUserOrder", self._emit("order"))
            self._connection.on("GatewayUserTrade", self._emit("fill"))
            self._connection.on("GatewayUserPosition", self._emit("position"))
            self._connection.on("GatewayUserAccount", self._emit("account"))
            self._connection.start()
            await asyncio.sleep(1.0)  # signalrcore handshake
            with contextlib.suppress(Exception):
                self._connection.send("SubscribeAccounts", [])
                self._connection.send("SubscribeOrders", [self._account_id])
                self._connection.send("SubscribeTrades", [self._account_id])
                self._connection.send("SubscribePositions", [self._account_id])
            self._started = True
            log.info("user_hub_mux_started", account_id=self._account_id)

    async def subscribe(self) -> AsyncIterator[dict]:
        await self._ensure_started()
        my_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._consumers.append(my_queue)
        log.info("user_mux_subscriber_added", total_consumers=len(self._consumers))
        try:
            while True:
                yield await my_queue.get()
        finally:
            with contextlib.suppress(ValueError):
                self._consumers.remove(my_queue)

    def stop(self) -> None:
        if self._connection is not None and self._started:
            with contextlib.suppress(Exception):
                self._connection.stop()
            self._started = False
