"""Conductor — the single owner of broker session + position state.

Responsibilities:
  - Authenticate broker, resolve the front-month contract, warm up strategies
  - Drive the live quote stream into per-timeframe bar aggregators
  - On each completed bar at a strategy's declared timeframe, call its on_bar
  - Collect signals into the arbitrator → log signal_emitted / signal_arbitrated
    / signal_suppressed events
  - Check calendar gate; submit (live) or simulate (dry-run) the winning signal
  - Run the flat-first protocol on direction reversals (close → 60s cooldown
    → re-evaluate)
  - Maintain phantom dry-run positions and log their closes

Strategies never touch the broker. Only the conductor does. This is the
architectural answer to the no-hedging rule.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

import structlog

from acme.broker.base import BrokerAdapter
from acme.calendar import CT, can_trade_now, topstep_trading_date
from acme.conductor.arbitrator import (
    ArbitrationContext,
    ArbitrationResult,
    _Candidate,
    arbitrate,
)
from acme.conductor.bar_aggregator import MultiTimeframeAggregator
from acme.conductor.dry_run import DryRunPosition, check_dry_run_exits
from acme.conductor.flat_first import FlatFirstFSM
from acme.config import Config
from acme.contracts import MES
from acme.db import Db
from acme.perf.scoring import compute_confidence
from acme.perf.snapshot import write_snapshot
from acme.perf.tracker import PerfRegistry
from acme.registry import RegisteredStrategy, StrategyRegistry
from acme.risk import DailyState
from acme.strategies.base import Signal

log = structlog.get_logger(__name__)

SNAPSHOT_INTERVAL_SEC = 30


class Conductor:
    def __init__(
        self,
        broker: BrokerAdapter,
        db: Db | None,
        config: Config,
        registry: StrategyRegistry,
        *,
        dry_run: bool = False,
    ) -> None:
        self.broker = broker
        self.db = db
        self.config = config
        self.registry = registry
        self.dry_run = dry_run
        self.contract = MES
        self.round_turn_fee = config.eval_profile.round_turn_fees.get("MES", 0.0)
        self.flat_first = FlatFirstFSM(cooldown_seconds=60)
        # Live position state (real broker position; updated on submit + fills).
        self.position: int = 0
        # Phantom dry-run state — per-strategy buckets so each shadow strategy's
        # would-be P&L is tracked independently. Map: strategy_name -> open positions.
        self.dry_run_open: dict[str, list[DryRunPosition]] = {}
        # Per-strategy net phantom position (signed; positive = net long).
        self.dry_run_position_per_strategy: dict[str, int] = {}
        # Per-strategy rolling performance metrics. Updated on every closed trade.
        self.perf = PerfRegistry()

    # ---------- main entry ----------

    async def run_forever(self) -> None:
        await self.broker.authenticate()
        account = await self.broker.get_account()
        starting_balance = float(account.get("balance") or account.get("Balance") or 0.0)
        contract_id = await self.broker.resolve_contract("MES")

        # Backfill PerfTracker from historical dry_run_close events so the
        # leaderboard reflects past runs immediately rather than starting empty.
        try:
            n_backfill = self.perf.backfill_from_db(self.db)
            if n_backfill > 0:
                log.info("perf_backfilled", n_events=n_backfill)
                # Push a fresh snapshot to Supabase so strategy.score is current.
                if self.db is not None:
                    write_snapshot(self.db, self.perf, self.registry)
        except Exception as e:
            log.warning("perf_backfill_failed", error=str(e))

        log.info("conductor_start", account_id=account.get("id"),
                 contract_id=contract_id,
                 strategies=[s.name for s in self.registry.list_active()])

        state = DailyState(
            trade_date=topstep_trading_date(datetime.now(CT)),
            starting_balance=starting_balance,
            peak_balance_eod=starting_balance,
            max_loss_limit=starting_balance - self.config.eval_profile.max_loss_amount,
            daily_loss_limit=self.config.eval_profile.daily_loss_limit,
        )

        await self._warm_up_strategies(contract_id, starting_balance, state)

        snap_task = asyncio.create_task(self._snapshot_loop())
        try:
            await self._live_loop(contract_id, starting_balance, state)
        finally:
            snap_task.cancel()
            with contextlib.suppress(Exception):
                await snap_task

    # ---------- warmup ----------

    async def _warm_up_strategies(
        self, contract_id: str, starting_balance: float, state: DailyState
    ) -> None:
        end = datetime.now(UTC)
        for rec in self.registry.list_active():
            inst = rec.instance
            if inst is None:
                continue
            n_bars = inst.required_history_bars() + 5
            start = end - timedelta(minutes=n_bars * inst.timeframe_minutes)
            bars = await self.broker.get_bars(
                contract_id,
                unit=2,
                unit_number=inst.timeframe_minutes,
                start=start,
                end=end,
            )
            log.info("warmup_bars", strategy=rec.name, count=len(bars))
            for b in bars:
                inst.on_bar(
                    b,
                    state=state,
                    profile=self.config.eval_profile,
                    current_position=0,
                    current_balance_unrealized=starting_balance,
                )

    # ---------- live loop ----------

    async def _live_loop(
        self, contract_id: str, starting_balance: float, state: DailyState
    ) -> None:
        # Build a single MultiTimeframeAggregator across all timeframes the active
        # strategies want. In B1 only ema_cross (1m) is active so this is just 1m.
        timeframes = sorted({
            s.instance.timeframe_minutes for s in self.registry.list_active()
            if s.instance is not None
        }) or [1]
        aggregator = MultiTimeframeAggregator(timeframes=timeframes)

        async for q in self.broker.stream_quotes(contract_id):
            price = q.last or q.bid or q.ask
            if price is None:
                continue
            bars_by_tf = aggregator.add_tick(q.t, price)
            if not bars_by_tf:
                continue
            for tf, bar in bars_by_tf.items():
                # Phantom dry-run exits checked on every completed bar at any tf.
                # Walk every strategy's bucket; positions opened by tf-1m strategies
                # close against tf-1m bars (etc.), so the right thing is to attempt
                # exits for every bucket on every bar — exits are gated by price.
                if self.dry_run:
                    for sname, open_positions in self.dry_run_open.items():
                        if not open_positions:
                            continue
                        delta, closes = check_dry_run_exits(
                            open_positions, bar, contract_id,
                            self.contract.point_value, self.round_turn_fee, self.db,
                        )
                        self.dry_run_position_per_strategy[sname] = (
                            self.dry_run_position_per_strategy.get(sname, 0) + delta
                        )
                        # Feed every close into the per-strategy perf tracker.
                        for cl in closes:
                            self.perf.record_close(
                                strategy=cl.strategy,
                                net_pnl=cl.net_pnl,
                                side=cl.side,
                                outcome=cl.outcome,
                                entry_price=cl.entry_price,
                                exit_price=cl.exit_price,
                                closed_at=cl.closed_at,
                            )
                        # Push a fresh perf snapshot to Supabase on every closed trade
                        # so the leaderboard moves in real-time.
                        if closes and self.db is not None:
                            try:
                                write_snapshot(self.db, self.perf, self.registry)
                            except Exception as e:
                                log.error("perf_snapshot_on_close_failed", error=str(e))

                # Tick the flat-first FSM; if cooldown elapsed, we'll re-eval below.
                self.flat_first.tick(datetime.now(UTC))

                await self._process_bar(bar, tf, contract_id, starting_balance, state)

    async def _process_bar(
        self,
        bar,
        tf: int,
        contract_id: str,
        starting_balance: float,
        state: DailyState,
    ) -> None:
        # Collect signals from every active strategy whose timeframe matches.
        # Each strategy is told its OWN current phantom position so its internal
        # "already in a position" guard works per-strategy, not fleet-wide.
        candidates: list[_Candidate] = []
        for rec in self.registry.list_active():
            inst = rec.instance
            if inst is None or inst.timeframe_minutes != tf:
                continue
            if self.dry_run:
                strat_pos = self.dry_run_position_per_strategy.get(rec.name, 0)
            else:
                strat_pos = self.position if rec.is_executable else 0
            sig = inst.on_bar(
                bar,
                state=state,
                profile=self.config.eval_profile,
                current_position=strat_pos,
                current_balance_unrealized=starting_balance + state.realized_pnl,
            )
            if sig is None:
                continue
            self._emit_signal_event(rec, sig, bar)
            if rec.metadata is not None:
                candidates.append(_Candidate(
                    name=rec.name, signal=sig,
                    metadata=rec.metadata, tier=rec.tier,
                ))

            # In dry-run, every active strategy gets its phantom position tracked
            # independently — even strategies in SHADOW state. Arbitration still
            # runs (logs winner/suppressed for analytics) but doesn't gate phantom
            # opens. This gives B3's leaderboard real per-strategy data to score.
            if self.dry_run and sig.size > 0:
                # Calendar gate still applies (don't simulate trades during forbidden windows).
                allowed, gate_reason = can_trade_now(datetime.now(CT))
                if not allowed:
                    if self.db:
                        self.db.log_event(
                            "calendar_block",
                            contract_id=contract_id, symbol="MES",
                            strategy=rec.name,
                            raw={"reason": gate_reason, "side": sig.side, "size": sig.size},
                        )
                    continue
                self._open_phantom_position(rec.name, sig, bar, contract_id)
            elif self.dry_run and sig.size == 0:
                if self.db:
                    self.db.log_event(
                        "risk_block",
                        contract_id=contract_id, symbol="MES",
                        strategy=rec.name,
                        raw={"reason": sig.reason},
                    )

        if not candidates:
            return

        # Build arbitration context (regime placeholder until the classifier
        # is wired up — defaults to None which the arbitrator treats as 'quiet').
        confidences = {
            rec.name: compute_confidence(
                self.perf.metrics_for(rec.name) or self._empty_metrics(rec.name),
                rec.state,
            )
            for rec in self.registry.list_active() if rec.instance is not None
        }
        ctx = ArbitrationContext(regime=None, now_ct=datetime.now(CT), confidences=confidences)
        result = arbitrate(candidates, ctx)
        self._emit_arbitration_events(result, bar)

        # In dry-run we've already opened phantom positions per-strategy above.
        # The remaining execution path is for live (non-dry-run) PILOT/LIVE only.
        if self.dry_run:
            return

        if not result.has_winner:
            return
        winner = result.winner
        # If size==0, the strategy itself blocked (risk gate inside on_bar).
        if winner.signal.size <= 0:
            if self.db:
                self.db.log_event(
                    "risk_block",
                    contract_id=contract_id, symbol="MES",
                    strategy=winner.strategy,
                    raw={"reason": winner.signal.reason},
                )
            return

        # Only execute if winner's strategy is in PILOT or LIVE state (SHADOW shows
        # in arbitration logs but never trades).
        winner_rec = self.registry.get(winner.strategy)
        if not winner_rec.is_executable:
            log.info("shadow_winner_not_executed",
                     strategy=winner.strategy, state=winner_rec.state)
            return

        # Calendar gate (account-level, applies to every signal).
        allowed, gate_reason = can_trade_now(datetime.now(CT))
        if not allowed:
            if self.db:
                self.db.log_event(
                    "calendar_block",
                    contract_id=contract_id, symbol="MES",
                    strategy=winner.strategy,
                    raw={
                        "reason": gate_reason,
                        "side": winner.signal.side,
                        "size": winner.signal.size,
                    },
                )
            return

        # Flat-first: if we hold a position opposite to winner.signal.side, route
        # through the FSM.
        held = self.position
        if held != 0:
            current_dir = "buy" if held > 0 else "sell"
            if winner.signal.side != current_dir:
                if self.flat_first.is_blocking:
                    log.info("flat_first_blocking_new_signal",
                             state=self.flat_first.status.state)
                    return
                self.flat_first.request_reversal(
                    winner.signal.side, datetime.now(UTC),
                    reason=f"{winner.strategy}:{winner.signal.reason}",
                )
                await self._flatten_position(contract_id, winner.strategy)
                self.flat_first.on_position_closed(datetime.now(UTC))
                return  # signal will be re-evaluated after cooldown

        await self._execute_signal(
            winner.strategy, winner.signal, bar, contract_id,
        )

    # ---------- execution paths ----------

    async def _execute_signal(
        self, strategy_name: str, signal: Signal, bar, contract_id: str,
    ) -> None:
        # Dry-run signals are handled in _process_bar (per-strategy phantoms).
        # This path is for live (non-dry-run) execution only.
        order_id = await self.broker.submit_market_order(
            contract_id,
            signal.side,
            signal.size,
            custom_tag=f"{strategy_name}",
            bracket=signal.bracket,
        )
        signed = signal.size if signal.side == "buy" else -signal.size
        self.position += signed
        if self.db:
            self.db.log_event(
                "order_submitted",
                contract_id=contract_id, symbol="MES",
                side=signal.side, size=signal.size,
                strategy=strategy_name,
                raw={"order_id": order_id, "reason": signal.reason},
            )

    def _open_phantom_position(
        self, strategy_name: str, signal: Signal, bar, contract_id: str,
    ) -> None:
        stop_ticks = signal.bracket.stop_loss_offset_ticks if signal.bracket else 0
        target_ticks = signal.bracket.take_profit_offset_ticks if signal.bracket else 0
        tick = self.contract.tick_size
        if signal.side == "buy":
            stop_price = bar.c - stop_ticks * tick
            target_price = bar.c + target_ticks * tick
        else:
            stop_price = bar.c + stop_ticks * tick
            target_price = bar.c - target_ticks * tick
        pos = DryRunPosition(
            side=signal.side,
            size=signal.size,
            entry_price=bar.c,
            stop_price=stop_price,
            target_price=target_price,
            entry_bar_t=bar.t,
            reason=signal.reason,
            strategy=strategy_name,
        )
        self.dry_run_open.setdefault(strategy_name, []).append(pos)
        signed = signal.size if signal.side == "buy" else -signal.size
        self.dry_run_position_per_strategy[strategy_name] = (
            self.dry_run_position_per_strategy.get(strategy_name, 0) + signed
        )
        if self.db:
            self.db.log_event(
                "dry_run_signal",
                contract_id=contract_id, symbol="MES",
                side=signal.side, size=signal.size, price=bar.c,
                strategy=strategy_name,
                raw={
                    "reason": signal.reason,
                    "stop_ticks": stop_ticks,
                    "target_ticks": target_ticks,
                    "stop_price": stop_price,
                    "target_price": target_price,
                    "bar_close": bar.c,
                },
            )
        log.info("dry_run_signal", strategy=strategy_name,
                 side=signal.side, size=signal.size, price=bar.c)

    async def _flatten_position(self, contract_id: str, requested_by: str) -> None:
        if self.dry_run:
            # Close all phantom positions across all strategies immediately.
            self.dry_run_open.clear()
            self.dry_run_position_per_strategy.clear()
        else:
            await self.broker.flatten_all()
            self.position = 0
        if self.db:
            self.db.log_event(
                "flatten_triggered",
                contract_id=contract_id, symbol="MES",
                strategy=requested_by,
                raw={"reason": "flat_first_reversal", "dry_run": self.dry_run},
            )

    def _empty_metrics(self, strategy: str):
        from acme.perf.tracker import PerfMetrics
        return PerfMetrics(
            strategy=strategy, window_label="empty",
            n_trades=0, net_pnl=0.0, win_rate=0.0, profit_factor=None,
            sharpe=0.0, max_drawdown=0.0,
            avg_win=0.0, avg_loss=0.0, best=0.0, worst=0.0,
        )

    # ---------- event emission ----------

    def _emit_signal_event(self, rec: RegisteredStrategy, sig: Signal, bar) -> None:
        if self.db is None:
            return
        self.db.log_event(
            "signal_emitted",
            contract_id=None, symbol="MES",
            side=sig.side, size=sig.size, price=bar.c,
            strategy=rec.name,
            raw={
                "reason": sig.reason,
                "tier": rec.tier,
                "lifecycle": rec.state,
                "bar_t": bar.t.isoformat(),
            },
        )

    def _emit_arbitration_events(self, result: ArbitrationResult, bar) -> None:
        if self.db is None:
            return
        if result.winner is not None:
            self.db.log_event(
                "signal_arbitrated",
                symbol="MES",
                side=result.winner.signal.side,
                size=result.winner.signal.size,
                strategy=result.winner.strategy,
                raw={
                    "composite_score": result.winner.composite_score,
                    "score_breakdown": result.winner.score_breakdown,
                    "reason": result.winner.signal.reason,
                    "n_candidates": 1 + len(result.suppressed),
                    "bar_t": bar.t.isoformat(),
                },
            )
        for sup in result.suppressed:
            self.db.log_event(
                "signal_suppressed",
                symbol="MES",
                side=sup.signal.side,
                size=sup.signal.size,
                strategy=sup.strategy,
                raw={
                    "composite_score": sup.composite_score,
                    "score_breakdown": sup.score_breakdown,
                    "winner": result.winner.strategy if result.winner else None,
                    "bar_t": bar.t.isoformat(),
                },
            )

    # ---------- snapshot loop ----------

    async def _snapshot_loop(self) -> None:
        while True:
            try:
                account = await self.broker.get_account()
                positions = await self.broker.get_positions()
                net = sum(p.size for p in positions)
                balance = float(account.get("balance") or account.get("Balance") or 0.0)
                if self.db:
                    self.db.log_event(
                        "account_snapshot",
                        account_id=str(account.get("id") or account.get("Id") or ""),
                        raw={
                            "balance": balance,
                            "can_trade": bool(account.get("canTrade", True)),
                            "net_position": net,
                            "positions": [
                                {"contract_id": p.contract_id, "size": p.size,
                                 "avg_price": p.avg_price}
                                for p in positions
                            ],
                            "active_strategies": [s.name for s in self.registry.list_active()],
                        },
                    )
            except Exception as e:
                log.error("snapshot_failed", error=str(e))
            await asyncio.sleep(SNAPSHOT_INTERVAL_SEC)
