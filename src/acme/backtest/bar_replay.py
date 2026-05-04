"""Bar-replay backtest engine.

Drives a single Strategy through historical bars and simulates bracket fills
with conservative slippage. Per-trade outcomes feed a PerfTracker; the final
metrics + equity curve are returned as a BacktestReport.

Slippage model (conservative; can be overridden):
  - Entry: 1 tick adverse (you "paid" 1 tick worse than bar close)
  - Stop hit: 2 ticks adverse (you slipped through the stop)
  - Target hit: 1 tick adverse (you didn't get full extension)

For multi-timeframe strategies (e.g. 5-min), the engine aggregates 1-min bars
into the strategy's declared timeframe via MultiTimeframeAggregator.

The engine optionally enforces each strategy's `metadata.time_buckets` as a
HARD filter on entry signals (default ON). The live runner doesn't enforce
this — strategies are pure signal-generators and the live conductor uses
buckets as a soft signal for arbitration. The backtest enforces because
the most informative finding from the first run was that strategies were
trading 24h despite metadata declaring RTH-only windows.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Literal

import structlog

from acme.broker.base import Bar
from acme.calendar import CT, topstep_trading_date
from acme.conductor.bar_aggregator import MultiTimeframeAggregator
from acme.context import MarketContext
from acme.perf.tracker import PerfMetrics, PerfTracker
from acme.risk import DailyState, EvalProfile
from acme.strategies.base import Strategy
from acme.telemetry import BarEventLogger

log = structlog.get_logger(__name__)


@dataclass
class _OpenPosition:
    side: Literal["buy", "sell"]
    size: int
    entry_price: float
    stop_price: float
    target_price: float
    entry_bar_t: datetime
    reason: str
    bar_event_id: int | None = None


@dataclass
class TradeOutcome:
    strategy: str
    side: Literal["buy", "sell"]
    size: int
    entry_t: datetime
    exit_t: datetime
    entry_price: float
    exit_price: float
    outcome: Literal["target", "stop", "eod_flatten"]
    gross_pnl: float
    fees: float
    net_pnl: float
    bars_held: int


@dataclass
class BacktestReport:
    strategy: str
    profile: str
    bars_processed: int
    metrics: PerfMetrics
    trades: list[TradeOutcome] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)

    def summary_dict(self) -> dict:
        m = self.metrics
        return {
            "strategy": self.strategy,
            "profile": self.profile,
            "bars_processed": self.bars_processed,
            "n_trades": m.n_trades,
            "net_pnl": round(m.net_pnl, 2),
            "win_rate": round(m.win_rate, 4),
            "profit_factor": (round(m.profit_factor, 4)
                              if m.profit_factor is not None else None),
            "sharpe": round(m.sharpe, 4),
            "max_drawdown": round(m.max_drawdown, 2),
            "best": round(m.best, 2),
            "worst": round(m.worst, 2),
        }


# Stage 0 gates from the Phase B plan
STAGE_0_MIN_SHARPE = 1.5
STAGE_0_MIN_PROFIT_FACTOR = 1.4
STAGE_0_MAX_DRAWDOWN_PCT = 0.08   # 8% of starting balance
STAGE_0_MIN_TRADES = 100


def evaluate_stage_0(report: BacktestReport, starting_balance: float) -> dict:
    """Returns a dict with each gate's pass/fail + a single overall verdict."""
    m = report.metrics
    max_dd_pct = m.max_drawdown / starting_balance if starting_balance > 0 else 1.0
    gates = {
        "sharpe": (m.sharpe >= STAGE_0_MIN_SHARPE,
                   f"{m.sharpe:.2f} ≥ {STAGE_0_MIN_SHARPE}"),
        "profit_factor": (
            m.profit_factor is not None and m.profit_factor >= STAGE_0_MIN_PROFIT_FACTOR,
            f"{m.profit_factor or 0:.2f} ≥ {STAGE_0_MIN_PROFIT_FACTOR}",
        ),
        "max_drawdown": (max_dd_pct <= STAGE_0_MAX_DRAWDOWN_PCT,
                         f"{max_dd_pct*100:.2f}% ≤ {STAGE_0_MAX_DRAWDOWN_PCT*100:.0f}%"),
        "sample_size": (m.n_trades >= STAGE_0_MIN_TRADES,
                        f"{m.n_trades} ≥ {STAGE_0_MIN_TRADES}"),
    }
    return {
        "gates": {k: {"pass": v[0], "actual": v[1]} for k, v in gates.items()},
        "verdict": "PASS" if all(v[0] for v in gates.values()) else "FAIL",
    }


def _bar_in_buckets(bar_t: datetime, buckets: list[str]) -> bool:
    """True if `bar_t` (any tz) falls inside any of the strategy's preferred
    CT time buckets. Buckets format: 'HH:MM-HH:MM' (24h CT). Empty list → no filter.
    """
    if not buckets:
        return True
    cur = bar_t.astimezone(CT).time()
    for bucket in buckets:
        try:
            start_s, end_s = bucket.split("-")
            sh, sm = (int(x) for x in start_s.split(":"))
            eh, em = (int(x) for x in end_s.split(":"))
        except (ValueError, IndexError):
            continue
        if time(sh, sm) <= cur <= time(eh, em):
            return True
    return False


def _exit_check(pos: _OpenPosition, bar: Bar, slip_stop_ticks: int,
                slip_target_ticks: int, tick_size: float) -> tuple[float, str] | None:
    """Returns (exit_price, outcome) if the bar's high/low touched a bracket level,
    else None. Conservative: stop fires first if both are within OHLC.
    """
    if pos.side == "buy":
        if bar.l <= pos.stop_price:
            return pos.stop_price - slip_stop_ticks * tick_size, "stop"
        if bar.h >= pos.target_price:
            return pos.target_price - slip_target_ticks * tick_size, "target"
    else:   # sell
        if bar.h >= pos.stop_price:
            return pos.stop_price + slip_stop_ticks * tick_size, "stop"
        if bar.l <= pos.target_price:
            return pos.target_price + slip_target_ticks * tick_size, "target"
    return None


def run_backtest(
    strategy: Strategy,
    bars: Iterator[Bar],
    *,
    profile: EvalProfile,
    starting_balance: float = 50_000.0,
    slip_entry_ticks: int = 1,
    slip_stop_ticks: int = 2,
    slip_target_ticks: int = 1,
    enforce_time_buckets: bool = True,
    telemetry: BarEventLogger | None = None,
) -> BacktestReport:
    """Run `strategy` against `bars`, simulating bracket fills with slippage.

    Returns a BacktestReport with per-trade outcomes, rolling metrics, and an
    equity curve sampled at every closed trade.
    """
    contract = strategy.contract
    point_value = contract.point_value
    tick = contract.tick_size
    round_turn_fee = profile.round_turn_fees.get(contract.symbol, 0.0)
    tf = strategy.timeframe_minutes

    aggregator = MultiTimeframeAggregator(timeframes=[tf]) if tf > 1 else None
    perf = PerfTracker(strategy=strategy.name)
    open_positions: list[_OpenPosition] = []
    trades: list[TradeOutcome] = []
    equity_curve: list[tuple[datetime, float]] = []
    cum_pnl = 0.0
    bars_processed = 0
    state: DailyState | None = None
    last_trading_date = None
    bar_indices_for_pos: dict[int, int] = {}   # id(pos) → bar index when opened
    context = MarketContext()

    for one_min_bar in bars:
        bars_processed += 1

        # Roll DailyState at the topstep trading-day boundary.
        # Critical: today's starting_balance must reflect cumulative P&L from
        # previous days, NOT the original Combine balance. Otherwise the
        # daily-loss-limit check in risk.py (which compares
        # `current_balance_unrealized - state.starting_balance` to
        # -daily_loss_limit) treats all-time cumulative P&L as today's loss,
        # blocking every trade once cum_pnl < -$1k.
        td = topstep_trading_date(one_min_bar.t.astimezone(CT))
        if state is None or td != last_trading_date:
            todays_starting = starting_balance + cum_pnl
            # Trailing DD peak should be the highest-ever EOD balance, not just today's.
            running_peak = max(
                state.peak_balance_eod if state else todays_starting,
                todays_starting,
            )
            state = DailyState(
                trade_date=td,
                starting_balance=todays_starting,
                peak_balance_eod=running_peak,
                max_loss_limit=running_peak - profile.max_loss_amount,
                daily_loss_limit=profile.daily_loss_limit,
            )
            last_trading_date = td

        # Determine which bar the strategy receives this iteration
        if aggregator is None:
            target_bar = one_min_bar
        else:
            bars_by_tf = aggregator.add_tick(one_min_bar.t, one_min_bar.c)
            # Use the higher-tf bar if one closed; otherwise we still want exit
            # checks on the 1-min bar, but no on_bar call for the strategy.
            target_bar = bars_by_tf.get(tf)

        # Exit checks always use the freshest 1-min bar (more accurate than
        # waiting for a 5-min close).
        for pos in list(open_positions):
            result = _exit_check(pos, one_min_bar, slip_stop_ticks,
                                  slip_target_ticks, tick)
            if result is None:
                continue
            exit_price, outcome = result
            direction = 1 if pos.side == "buy" else -1
            gross = direction * (exit_price - pos.entry_price) * point_value * pos.size
            fees = round_turn_fee * pos.size
            net = gross - fees
            cum_pnl += net
            outcome_t = one_min_bar.t
            trades.append(TradeOutcome(
                strategy=strategy.name, side=pos.side, size=pos.size,
                entry_t=pos.entry_bar_t, exit_t=outcome_t,
                entry_price=pos.entry_price, exit_price=exit_price,
                outcome=outcome, gross_pnl=gross, fees=fees, net_pnl=net,
                bars_held=bars_processed - bar_indices_for_pos.pop(id(pos), bars_processed),
            ))
            perf.record_close(
                net_pnl=net, side=pos.side, outcome=outcome,
                entry_price=pos.entry_price, exit_price=exit_price,
                closed_at=outcome_t,
            )
            if telemetry is not None and pos.bar_event_id is not None:
                telemetry.log_outcome(
                    pos.bar_event_id,
                    exit_t=outcome_t, exit_price=exit_price,
                    net_pnl=net, outcome=outcome,
                )
            equity_curve.append((outcome_t, cum_pnl))
            open_positions.remove(pos)

        # Strategy on_bar — only when a new bar at its timeframe just closed
        if target_bar is None:
            continue
        # Time-bucket gate (default ON): if the strategy declares preferred CT
        # windows in metadata, skip on_bar entirely outside those windows.
        # Exits still fire on every bar so positions opened in-window can close
        # out-of-window — that's realistic.
        if enforce_time_buckets:
            buckets = getattr(strategy.metadata, "time_buckets", []) or []
            if buckets and not _bar_in_buckets(target_bar.t, buckets):
                continue
        # Update market context once per strategy-tf bar
        context.update(target_bar)
        # Per-strategy phantom position drives the strategy's "already in position" guard
        signed_pos = sum(p.size if p.side == "buy" else -p.size for p in open_positions)
        sig = strategy.on_bar(
            target_bar, state=state, profile=profile,
            current_position=signed_pos,
            current_balance_unrealized=starting_balance + cum_pnl,
        )
        bar_event_id: int | None = None
        if telemetry is not None:
            bar_event_id = telemetry.log(
                bar=target_bar, timeframe=tf, strategy=strategy.name,
                signal=sig, context=context.features,
                position=signed_pos,
                balance=starting_balance + cum_pnl,
            )
        if sig is None or sig.size <= 0:
            continue
        bracket = sig.bracket
        stop_ticks = bracket.stop_loss_offset_ticks if bracket else 0
        target_ticks = bracket.take_profit_offset_ticks if bracket else 0
        # Apply entry slippage — entry price is target_bar close moved adverse by N ticks
        if sig.side == "buy":
            entry = target_bar.c + slip_entry_ticks * tick
            stop_price = entry - stop_ticks * tick
            target_price = entry + target_ticks * tick
        else:
            entry = target_bar.c - slip_entry_ticks * tick
            stop_price = entry + stop_ticks * tick
            target_price = entry - target_ticks * tick
        new_pos = _OpenPosition(
            side=sig.side, size=sig.size,
            entry_price=entry, stop_price=stop_price, target_price=target_price,
            entry_bar_t=target_bar.t, reason=sig.reason,
            bar_event_id=bar_event_id,
        )
        open_positions.append(new_pos)
        bar_indices_for_pos[id(new_pos)] = bars_processed

    # PerfTracker caps at 200 trades for live rolling metrics; for the backtest
    # we want lifetime metrics over EVERY trade. Recompute from the full list.
    metrics = _metrics_from_trades(trades, strategy.name)
    return BacktestReport(
        strategy=strategy.name,
        profile=profile.name,
        bars_processed=bars_processed,
        metrics=metrics,
        trades=trades,
        equity_curve=equity_curve,
    )


def _metrics_from_trades(trades: list[TradeOutcome], strategy: str) -> PerfMetrics:
    """Lifetime PerfMetrics computed across every trade — used by the backtest
    so reporting isn't constrained by PerfTracker's rolling-window cap.
    """
    import math
    if not trades:
        return PerfMetrics(
            strategy=strategy, window_label="lifetime",
            n_trades=0, net_pnl=0.0, win_rate=0.0, profit_factor=None,
            sharpe=0.0, max_drawdown=0.0, avg_win=0.0, avg_loss=0.0,
            best=0.0, worst=0.0,
        )
    pnls = [t.net_pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gw = sum(wins)
    gl = -sum(losses)
    pf = (gw / gl) if gl > 0 else None
    mean = sum(pnls) / len(pnls)
    if len(pnls) > 1:
        var = sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1)
        sd = math.sqrt(var)
    else:
        sd = 0.0
    sharpe = (mean / sd) * math.sqrt(252) if sd > 0 else 0.0

    # Max drawdown on cumulative equity
    peak = 0.0
    cum = 0.0
    max_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    return PerfMetrics(
        strategy=strategy, window_label="lifetime",
        n_trades=len(pnls), net_pnl=sum(pnls),
        win_rate=len(wins) / len(pnls), profit_factor=pf,
        sharpe=sharpe, max_drawdown=max_dd,
        avg_win=(sum(wins) / len(wins)) if wins else 0.0,
        avg_loss=(sum(losses) / len(losses)) if losses else 0.0,
        best=max(pnls), worst=min(pnls),
    )
