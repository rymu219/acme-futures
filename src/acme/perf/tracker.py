"""Per-strategy rolling performance tracker.

The tracker consumes closed-trade outcomes (from `dry_run_close` events in
shadow/dry-run, or real fills in live) and maintains a rolling window of
per-trade P&L. From that window it computes the metrics the leaderboard
displays and the scoring formula consumes:

  - n_trades
  - net_pnl (sum of trade P&Ls)
  - win_rate (fraction of trades with pnl > 0)
  - profit_factor (gross wins / abs gross losses)
  - sharpe (annualized using a 252-day year, intraday adjusted)
  - max_drawdown (peak-to-trough on cumulative equity)
  - avg_win, avg_loss
  - signal latency p50 / p95 (placeholder until we instrument latency in B3.5)

The window is "last N trades OR last K trading days, whichever covers more
trades." Default: 30 trades or 5 trading days. Trades stay in the window
until both bounds have passed.

This is a pure-data class. The conductor or a separate consumer is
responsible for feeding `record_close()` calls.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta


@dataclass
class TradeRecord:
    closed_at: datetime
    net_pnl: float
    side: str           # 'buy' or 'sell'
    outcome: str        # 'target' | 'stop' | other
    entry_price: float
    exit_price: float


@dataclass
class PerfMetrics:
    """Snapshot of a strategy's rolling performance at a point in time."""
    strategy: str
    window_label: str
    n_trades: int
    net_pnl: float
    win_rate: float            # 0.0 - 1.0
    profit_factor: float | None
    sharpe: float              # annualized, on per-trade returns (rough proxy)
    max_drawdown: float        # absolute dollar drawdown on cumulative equity
    avg_win: float
    avg_loss: float            # negative number
    best: float
    worst: float

    def as_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "window_label": self.window_label,
            "n_trades": self.n_trades,
            "net_pnl": round(self.net_pnl, 2),
            "win_rate": round(self.win_rate, 4),
            "profit_factor": (round(self.profit_factor, 4)
                              if self.profit_factor is not None else None),
            "sharpe": round(self.sharpe, 4),
            "max_drawdown": round(self.max_drawdown, 2),
            "avg_win": round(self.avg_win, 2),
            "avg_loss": round(self.avg_loss, 2),
            "best": round(self.best, 2),
            "worst": round(self.worst, 2),
        }


# Roughly: the U.S. equity-index session has ~6.5 RTH hours; CME globex is ~23h.
# For a per-trade sharpe annualization, we want trades-per-year. With the seed
# fleet on MES at ~0-3 trades per day per strategy, ~252 trading days/year, an
# average strategy might do 250-750 trades/year. We'll use a conservative
# annualization factor; the absolute number matters less than the rank ordering
# across strategies.
_DEFAULT_ANNUALIZATION = math.sqrt(252)


@dataclass
class PerfTracker:
    strategy: str
    max_trades: int = 200                   # hard cap on memory
    window_n_trades: int = 30
    window_n_days: int = 5
    annualization_factor: float = _DEFAULT_ANNUALIZATION
    _trades: deque[TradeRecord] = field(default_factory=lambda: deque(maxlen=200))

    def record_close(
        self,
        net_pnl: float,
        side: str,
        outcome: str,
        entry_price: float,
        exit_price: float,
        closed_at: datetime | None = None,
    ) -> None:
        self._trades.append(TradeRecord(
            closed_at=closed_at or datetime.now(UTC),
            net_pnl=net_pnl,
            side=side,
            outcome=outcome,
            entry_price=entry_price,
            exit_price=exit_price,
        ))

    def metrics(self, *, window_label: str | None = None,
                now: datetime | None = None) -> PerfMetrics:
        """Compute metrics over the rolling window.

        Window definition: include trades closed within the last `window_n_days`
        OR the last `window_n_trades`, whichever yields more trades. This keeps
        the metric stable for both high-frequency strategies (n-trades-bounded)
        and low-frequency ones (days-bounded).
        """
        trades = self._window(now=now)
        return self._metrics_for(
            trades,
            window_label or f"{self.window_n_trades}t_or_{self.window_n_days}d",
        )

    def metrics_all_time(self) -> PerfMetrics:
        """Cumulative metrics over every trade ever recorded."""
        return self._metrics_for(list(self._trades), "all")

    def _window(self, *, now: datetime | None = None) -> list[TradeRecord]:
        if not self._trades:
            return []
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(days=self.window_n_days)
        by_time = [t for t in self._trades if t.closed_at >= cutoff]
        by_count = list(self._trades)[-self.window_n_trades:]
        return by_time if len(by_time) >= len(by_count) else by_count

    def _metrics_for(self, trades: list[TradeRecord], label: str) -> PerfMetrics:
        if not trades:
            return PerfMetrics(
                strategy=self.strategy, window_label=label,
                n_trades=0, net_pnl=0.0, win_rate=0.0, profit_factor=None,
                sharpe=0.0, max_drawdown=0.0,
                avg_win=0.0, avg_loss=0.0, best=0.0, worst=0.0,
            )
        pnls = [t.net_pnl for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_wins = sum(wins)
        gross_losses = -sum(losses)   # positive
        profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else None
        win_rate = len(wins) / len(pnls)

        # Sharpe-ish: mean / stddev of trade returns × annualization factor.
        # Treats each trade as a Bernoulli-ish return event. Imperfect but useful
        # for ranking across strategies.
        mean = sum(pnls) / len(pnls)
        if len(pnls) > 1:
            var = sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1)
            sd = math.sqrt(var)
        else:
            sd = 0.0
        sharpe = (mean / sd) * self.annualization_factor if sd > 0 else 0.0

        # Drawdown on cumulative equity curve
        max_drawdown = _max_drawdown(pnls)

        return PerfMetrics(
            strategy=self.strategy, window_label=label,
            n_trades=len(pnls),
            net_pnl=sum(pnls),
            win_rate=win_rate,
            profit_factor=profit_factor,
            sharpe=sharpe,
            max_drawdown=max_drawdown,
            avg_win=(sum(wins) / len(wins)) if wins else 0.0,
            avg_loss=(sum(losses) / len(losses)) if losses else 0.0,
            best=max(pnls),
            worst=min(pnls),
        )


def _max_drawdown(pnls: list[float]) -> float:
    """Maximum peak-to-trough drawdown on the cumulative equity curve.
    Returns a non-negative dollar amount.
    """
    if not pnls:
        return 0.0
    peak = 0.0
    cum = 0.0
    max_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = peak - cum
        max_dd = max(max_dd, dd)
    return max_dd


class PerfRegistry:
    """Holds a `PerfTracker` per strategy. Owns the read-side aggregation that
    leaderboard and arbitrator consume.
    """

    def __init__(self) -> None:
        self._trackers: dict[str, PerfTracker] = {}

    def get(self, strategy: str) -> PerfTracker:
        if strategy not in self._trackers:
            self._trackers[strategy] = PerfTracker(strategy=strategy)
        return self._trackers[strategy]

    def record_close(self, strategy: str, **kw) -> None:
        self.get(strategy).record_close(**kw)

    def all_metrics(self) -> list[PerfMetrics]:
        return [t.metrics() for t in self._trackers.values()]

    def metrics_for(self, strategy: str) -> PerfMetrics | None:
        if strategy not in self._trackers:
            return None
        return self._trackers[strategy].metrics()

    def __len__(self) -> int:
        return len(self._trackers)

    def __contains__(self, strategy: str) -> bool:
        return strategy in self._trackers

    def trading_dates_seen(self, strategy: str) -> set[date]:
        if strategy not in self._trackers:
            return set()
        return {t.closed_at.date() for t in self._trackers[strategy]._trades}

    def backfill_from_db(self, db, *, kind: str = "dry_run_close") -> int:
        """Replay every historical close event from Supabase into the per-strategy
        trackers. Called once at runner startup so a restart preserves the metrics
        accumulated by prior runs. Returns the number of events replayed.

        Bounded by `PerfTracker.max_trades` (200) per strategy — older events past
        that are silently dropped by the deque. For B3 this is fine; if we ever
        want the full history we'd page in by date range.
        """
        if db is None:
            return 0
        try:
            res = (
                db.client.table("broker_events")
                .select("strategy,occurred_at,raw")
                .eq("kind", kind)
                .order("id", desc=False)
                .execute()
            )
        except Exception:
            return 0
        n = 0
        for row in res.data or []:
            strategy = row.get("strategy") or "unknown"
            raw = row.get("raw")
            if not raw or not isinstance(raw, dict):
                continue
            if "net_pnl" not in raw or "entry_price" not in raw:
                continue
            try:
                net_pnl = float(raw["net_pnl"])
                entry_price = float(raw["entry_price"])
                exit_price = float(raw.get("exit_price") or entry_price)
                outcome = raw.get("outcome") or "unknown"
                closed_at = datetime.fromisoformat(
                    row["occurred_at"].replace("Z", "+00:00")
                )
            except (ValueError, TypeError, KeyError):
                continue
            # The dry_run_close `side` column records the CLOSING-order side.
            # For PerfTracker the side is purely informational (Sharpe/PF/DD/win
            # rate are side-agnostic) so we don't need to reverse-engineer it.
            side = "buy" if exit_price >= entry_price else "sell"
            self.record_close(
                strategy=strategy, net_pnl=net_pnl, side=side,
                outcome=outcome, entry_price=entry_price,
                exit_price=exit_price, closed_at=closed_at,
            )
            n += 1
        return n
