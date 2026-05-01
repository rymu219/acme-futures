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

from acme.broker.base import Bar, BracketSpec, BrokerAdapter, Position, Quote, Side

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

    async def aclose(self) -> None:
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
    # SignalR streaming is implemented lazily — the runner.py loop will call these
    # only when broker is real (smoke + production). Tests use the paper adapter.

    async def stream_quotes(self, contract_id: str) -> AsyncIterator[Quote]:  # type: ignore[override]
        from signalrcore.hub_connection_builder import HubConnectionBuilder  # type: ignore

        await self._ensure_auth()
        queue: asyncio.Queue[Quote] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        err_count = {"n": 0}

        def _extract_dict(args: list[Any]) -> dict | None:
            # ProjectX market hub typically delivers [contract_id_str, quote_dict].
            # Some shapes seen: [dict], [str, dict], [str, json_str].
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

        def _on_quote(args: list[Any]) -> None:
            try:
                payload = _extract_dict(args)
                if payload is None:
                    if err_count["n"] < 3:
                        log.error("on_quote_no_dict", args_repr=repr(args)[:200])
                        err_count["n"] += 1
                    return
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
                loop.call_soon_threadsafe(queue.put_nowait, q)
            except Exception as e:
                if err_count["n"] < 3:
                    log.error("on_quote_failed", error=str(e), args_repr=repr(args)[:200])
                    err_count["n"] += 1

        token = self._token
        connection = (
            HubConnectionBuilder()
            .with_url(
                f"{self.market_hub}?access_token={token}",
                options={"access_token_factory": lambda: token},
            )
            .with_automatic_reconnect({"type": "raw", "keep_alive_interval": 10, "reconnect_interval": 5})
            .build()
        )
        connection.on("GatewayQuote", _on_quote)
        connection.start()
        await asyncio.sleep(1.0)  # give the websocket handshake a moment
        try:
            connection.send("SubscribeContractQuotes", [contract_id])
            while True:
                yield await queue.get()
        finally:
            with contextlib.suppress(Exception):
                connection.send("UnsubscribeContractQuotes", [contract_id])
            connection.stop()

    async def stream_user_events(self) -> AsyncIterator[dict]:  # type: ignore[override]
        from signalrcore.hub_connection_builder import HubConnectionBuilder  # type: ignore

        await self._ensure_auth()
        queue: asyncio.Queue[dict] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _emit(kind: str):
            def handler(args: list[Any]) -> None:
                payload: Any = None
                for a in args or []:
                    if isinstance(a, dict):
                        payload = a
                        break
                    if isinstance(a, str) and a.startswith("{"):
                        import json
                        try:
                            payload = json.loads(a)
                            break
                        except Exception:
                            continue
                if payload is None:
                    payload = args
                evt = {"kind": kind, "payload": payload}
                loop.call_soon_threadsafe(queue.put_nowait, evt)
            return handler

        token = self._token
        connection = (
            HubConnectionBuilder()
            .with_url(
                f"{self.user_hub}?access_token={token}",
                options={"access_token_factory": lambda: token},
            )
            .with_automatic_reconnect({"type": "raw", "keep_alive_interval": 10, "reconnect_interval": 5})
            .build()
        )
        connection.on("GatewayUserOrder", _emit("order"))
        connection.on("GatewayUserTrade", _emit("fill"))
        connection.on("GatewayUserPosition", _emit("position"))
        connection.on("GatewayUserAccount", _emit("account"))
        connection.start()
        await asyncio.sleep(1.0)  # give the websocket handshake a moment
        try:
            connection.send("SubscribeAccounts", [])
            connection.send("SubscribeOrders", [self.account_id])
            connection.send("SubscribeTrades", [self.account_id])
            connection.send("SubscribePositions", [self.account_id])
            while True:
                yield await queue.get()
        finally:
            connection.stop()
