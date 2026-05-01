"""Smoke #2: 1-lot MES buy then close, with Supabase event logging.

WARNING: places real orders against your Topstep Combine sim account. The Combine
records all activity. Be deliberate. 1 contract MES only.

Run:  uv run python -m acme.smoke.round_trip
"""

from __future__ import annotations

import asyncio

from acme.broker.projectx import ProjectXAdapter
from acme.db import Db


async def main() -> None:
    db = Db()
    async with ProjectXAdapter() as broker:
        account = await broker.get_account()
        account_id = str(account.get("id") or account.get("Id"))
        db.log_event("auth_success", account_id=account_id, raw={"balance": account.get("balance")})

        contract_id = await broker.resolve_contract("MES")
        print(f"account_id={account_id} contract_id={contract_id}")

        print("submitting buy 1...")
        order_id = await broker.submit_market_order(contract_id, "buy", 1, custom_tag="smoke_buy")
        print(f"  buy order_id={order_id}")
        db.log_event(
            "order_submitted", contract_id=contract_id, symbol="MES",
            account_id=account_id, side="buy", size=1,
            raw={"order_id": order_id, "tag": "smoke_buy"},
        )
        await asyncio.sleep(3)

        positions_before_flatten = await broker.get_positions()
        held = sum(p.size for p in positions_before_flatten if p.contract_id == contract_id)
        print(f"  position after buy: {held}")

        print("flattening...")
        db.log_event("flatten_triggered", contract_id=contract_id, symbol="MES",
                     account_id=account_id, raw={"reason": "smoke_round_trip"})
        await broker.flatten_all()
        await asyncio.sleep(2)

        positions = await broker.get_positions()
        net = sum(p.size for p in positions if p.contract_id == contract_id)
        print(f"net position on {contract_id}: {net}")
        db.log_event("round_trip_complete", contract_id=contract_id, symbol="MES",
                     account_id=account_id, raw={"net_position": net, "held_before_flatten": held})
        assert net == 0, f"expected flat, got net={net}"
        print("OK: round trip flat")


if __name__ == "__main__":
    asyncio.run(main())
