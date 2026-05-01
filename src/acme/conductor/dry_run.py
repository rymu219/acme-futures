"""Phantom-position tracking for dry-run mode.

In dry-run, the conductor opens a phantom position for each signal it would
have executed and tracks bracket fills against subsequent bars. When a stop
or target is touched, a `dry_run_close` event is logged with realized P&L.

Conservative assumption: if both stop and target are within a single bar's
H/L range, assume the stop fires first (standard backtesting convention).

Extracted unchanged from `runner.py` (Phase A) into `conductor/`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import structlog

from acme.broker.base import Bar
from acme.db import Db

log = structlog.get_logger(__name__)


@dataclass
class DryRunPosition:
    """A phantom position opened by a dry-run signal. Closed when a subsequent
    bar's high/low touches the bracket levels.
    """
    side: Literal["buy", "sell"]
    size: int
    entry_price: float
    stop_price: float
    target_price: float
    entry_bar_t: datetime
    reason: str
    strategy: str = "unknown"


@dataclass
class DryRunClose:
    """Per-close payload for downstream consumers (e.g. PerfTracker)."""
    strategy: str
    side: Literal["buy", "sell"]
    size: int
    entry_price: float
    exit_price: float
    net_pnl: float
    outcome: str
    closed_at: datetime


def check_dry_run_exits(
    open_positions: list[DryRunPosition],
    bar: Bar,
    contract_id: str,
    point_value: float,
    round_turn_fee: float,
    db: Db | None,
) -> tuple[int, list[DryRunClose]]:
    """For each open dry-run position, check whether `bar` hit its stop or target.
    Logs a dry_run_close event and returns the net change in phantom net position
    plus a list of DryRunClose records for downstream consumers (e.g. PerfTracker).

    On bars where both stop and target are within OHLC, assume the stop fires first.
    """
    delta = 0
    closes: list[DryRunClose] = []
    for pos in list(open_positions):
        if pos.side == "buy":
            stop_hit = bar.l <= pos.stop_price
            target_hit = bar.h >= pos.target_price
            close_price = pos.stop_price if stop_hit else (pos.target_price if target_hit else None)
            outcome = "stop" if stop_hit else ("target" if target_hit else None)
            direction = 1
            close_size_change = -pos.size
        else:
            stop_hit = bar.h >= pos.stop_price
            target_hit = bar.l <= pos.target_price
            close_price = pos.stop_price if stop_hit else (pos.target_price if target_hit else None)
            outcome = "stop" if stop_hit else ("target" if target_hit else None)
            direction = -1
            close_size_change = pos.size
        if close_price is None:
            continue
        price_pnl = direction * (close_price - pos.entry_price) * point_value * pos.size
        net_pnl = price_pnl - round_turn_fee * pos.size
        if db:
            db.log_event(
                "dry_run_close",
                contract_id=contract_id,
                symbol="MES",
                side="sell" if pos.side == "buy" else "buy",
                size=pos.size,
                price=close_price,
                strategy=pos.strategy,
                raw={
                    "outcome": outcome,
                    "entry_price": pos.entry_price,
                    "exit_price": close_price,
                    "price_pnl": round(price_pnl, 2),
                    "fees": round(round_turn_fee * pos.size, 2),
                    "net_pnl": round(net_pnl, 2),
                    "entry_reason": pos.reason,
                    "bar_t": bar.t.isoformat(),
                },
            )
        log.info(
            "dry_run_close",
            outcome=outcome,
            net_pnl=round(net_pnl, 2),
            entry=pos.entry_price,
            exit=close_price,
            strategy=pos.strategy,
        )
        closes.append(DryRunClose(
            strategy=pos.strategy,
            side=pos.side,
            size=pos.size,
            entry_price=pos.entry_price,
            exit_price=close_price,
            net_pnl=net_pnl,
            outcome=outcome,
            closed_at=bar.t,
        ))
        open_positions.remove(pos)
        delta += close_size_change
    return delta, closes
