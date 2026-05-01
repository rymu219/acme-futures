"""Smoke #1: auth + resolve MES + last 5 bars + 5 live quotes. Manual.

Run:  uv run python -m acme.smoke.hello_world
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from acme.broker.projectx import ProjectXAdapter


async def main() -> None:
    async with ProjectXAdapter() as broker:
        account = await broker.get_account()
        print(f"account_id={account.get('id')} balance={account.get('balance')}")

        contract_id = await broker.resolve_contract("MES")
        print(f"contract_id={contract_id}")

        end = datetime.now(UTC)
        start = end - timedelta(minutes=10)
        bars = await broker.get_bars(contract_id, unit=2, unit_number=1, start=start, end=end, limit=5)
        print("last bars:")
        for b in bars[-5:]:
            print(f"  {b.t.isoformat()}  O={b.o} H={b.h} L={b.l} C={b.c} V={b.v}")

        print("subscribing to quotes for 5 ticks...")
        n = 0
        async for q in broker.stream_quotes(contract_id):
            print(f"  {q.t.isoformat()}  bid={q.bid} ask={q.ask} last={q.last}")
            n += 1
            if n >= 5:
                break


if __name__ == "__main__":
    asyncio.run(main())
