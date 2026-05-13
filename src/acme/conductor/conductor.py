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
from acme.calendar import CT, can_trade_now, in_econ_blackout, topstep_trading_date
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
from acme.context import MarketContext
from acme.contracts import MES
from acme.db import Db
from acme.perf.scoring import compute_confidence
from acme.perf.snapshot import write_snapshot
from acme.perf.tracker import PerfRegistry
from acme.regime.classifier import RegimeEngine, RegimeSnapshot
from acme.regime.habitat import eligible_strategies
from acme.registry import RegisteredStrategy, StrategyRegistry
from acme.risk import DailyState
from acme.strategies.base import Signal
from acme.telemetry import BarEventLogger

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
        telemetry: BarEventLogger | None = None,
        regime_engine: RegimeEngine | None = None,
        regime_timeframe_minutes: int = 5,
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
        # Per-bar telemetry — local sqlite, every (strategy, bar) writes one row.
        # MarketContext is per-timeframe so all strategies sharing a tf see the
        # same universal features on the same bar. Tests can pass mode="off"
        # to skip the sqlite write entirely.
        self._telemetry = telemetry or BarEventLogger(source="live", mode="full")
        self._context_by_tf: dict[int, MarketContext] = {}
        # Regime engine — updated only on bars at `regime_timeframe_minutes`.
        # If None, no regime gating is applied (legacy behavior).
        self._regime_engine = regime_engine
        self._regime_tf = regime_timeframe_minutes
        self._latest_regime: RegimeSnapshot | None = None
        # Kill-switch state — polled from operator_events on each bar.
        # `_kill_switch_active` reflects the latest event seen; flips
        # to True on a kill_switch_activated row and False on a
        # kill_switch_cleared row. `_kill_switch_last_id` tracks the
        # highest event id we've observed, so each new bar only fetches
        # rows newer than that (cheap incremental polling).
        self._kill_switch_active: bool = False
        self._kill_switch_last_id: int = 0

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
        timeframes = {
            s.instance.timeframe_minutes for s in self.registry.list_active()
            if s.instance is not None
        }
        # Ensure the regime timeframe is always available, even if no strategy
        # currently runs on it — the regime engine still needs bars there.
        if self._regime_engine is not None:
            timeframes.add(self._regime_tf)
        timeframes_sorted = sorted(timeframes) or [1]
        aggregator = MultiTimeframeAggregator(timeframes=timeframes_sorted)

        async for q in self.broker.stream_quotes(contract_id):
            try:
                await self._handle_quote(
                    q, aggregator, contract_id, starting_balance, state,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Per-quote exceptions should never tear down the live
                # loop. The 2026-05-12 day analysis found the runner
                # was exiting silently — likely an unhandled async
                # exception from one strategy / one bar. Log loudly,
                # keep going.
                log.error("live_loop_iteration_failed", error=str(e),
                          exc_info=True)

    async def _handle_quote(
        self, q, aggregator, contract_id: str,
        starting_balance: float, state: DailyState,
    ) -> None:
        """Extracted from _live_loop so per-iteration exceptions get caught."""
        price = q.last or q.bid or q.ask
        if price is None:
            return
        bars_by_tf = aggregator.add_tick(q.t, price)
        if not bars_by_tf:
            return
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
                        if cl.bar_event_id is not None:
                            self._telemetry.log_outcome(
                                cl.bar_event_id,
                                exit_t=cl.closed_at,
                                exit_price=cl.exit_price,
                                net_pnl=cl.net_pnl,
                                outcome=cl.outcome,
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

            # Per-strategy heartbeat — one row per active strategy per
            # bar. Lets the UI / watchdog detect a stale runner without
            # depending on broker_events (which only fires on closed
            # trades — could be silent for hours during slow markets).
            self._write_fleet_heartbeats(bar, contract_id)

    def _write_fleet_heartbeats(self, bar, contract_id: str) -> None:
        """Upsert one row in runtime_heartbeats per active strategy.

        position_state is derived from the per-strategy phantom position
        in dry-run (each strategy can have its own open position even
        though the broker-level position is single). In live mode, every
        strategy mirrors the conductor's broker position state.
        """
        if self.db is None:
            return
        is_short = self.position < 0
        is_long = self.position > 0
        for rec in self.registry.list_active():
            if rec.instance is None:
                continue
            if self.dry_run:
                phantom = self.dry_run_position_per_strategy.get(rec.name, 0)
                if phantom > 0:
                    pos_state = "long"
                elif phantom < 0:
                    pos_state = "short"
                else:
                    pos_state = "flat"
            else:
                pos_state = "long" if is_long else ("short" if is_short else "flat")
            try:
                self.db.write_heartbeat(
                    rec.name,
                    last_bar_ts=bar.t,
                    auth_ok=True,
                    consecutive_errors=0,
                    position_state=pos_state,
                    extra={
                        "contract_id": contract_id,
                        "mode": "paper" if self.dry_run else "live",
                        "broker": type(self.broker).__name__,
                        "lifecycle": rec.state,
                        "timeframe_minutes": rec.instance.timeframe_minutes,
                    },
                )
            except Exception as e:
                log.warning("fleet_heartbeat_write_failed",
                            strategy=rec.name, error=str(e))

    async def _process_bar(
        self,
        bar,
        tf: int,
        contract_id: str,
        starting_balance: float,
        state: DailyState,
    ) -> None:
        # Kill-switch poll: every bar, look for new operator_events
        # rows. Flip an activated → flatten everything immediately and
        # skip the rest of bar processing. Cheap because the query is
        # incremental (id > _kill_switch_last_id).
        await self._poll_kill_switch(contract_id)
        if self._kill_switch_active:
            # While the switch is active, no new entries are allowed —
            # any open positions were force-flattened on activation,
            # subsequent bars just no-op.
            return

        # Fleet-coordination force-flat: each strategy that implements
        # `wants_force_flat(bar)` is consulted before signal collection.
        # On True we close that strategy's position immediately — for live
        # mode via broker.flatten_all (the conductor only holds one
        # position so flattening is fleet-wide), for dry-run by clearing
        # the strategy's phantom positions.
        await self._honor_fleet_coordination(bar, tf, contract_id)

        # Update shared market context once per (tf, bar) — features will be
        # written into every per-strategy telemetry row below.
        ctx = self._context_by_tf.setdefault(tf, MarketContext())
        ctx.update(bar)
        ctx_features = ctx.features

        # Update regime classifier on bars at the regime timeframe (default 5m).
        # All strategies share the same most-recent snapshot for habitat gating.
        if self._regime_engine is not None and tf == self._regime_tf:
            try:
                blackout, _ = in_econ_blackout(bar.t.astimezone(CT))
            except Exception:
                blackout = False
            self._latest_regime = self._regime_engine.on_bar(bar, news_blackout=blackout)
            if self.db is not None:
                try:
                    self.db.insert_regime_snapshot(self._latest_regime.to_db_row())
                except Exception as e:
                    log.error("regime_persist_failed", error=str(e))

        # Habitat gating: if a regime is in force, restrict the active set to
        # strategies whose declared regime_fit qualifies them. Snapshot must
        # exist (not warmup) and not be chaotic / low-confidence.
        eligible: set[str] | None = None
        if self._latest_regime is not None:
            snap = self._latest_regime
            if snap.regime in ("chaotic", "compressing", "ambiguous") or snap.confidence < 0.4:
                if self.db is not None:
                    self.db.log_event(
                        "regime_block",
                        contract_id=contract_id, symbol="MES",
                        raw={
                            "regime": snap.regime,
                            "confidence": snap.confidence,
                            "ts": snap.ts.isoformat(),
                        },
                    )
                return
            eligible = set(eligible_strategies(self.registry, snap.regime, snap.confidence))

        # Collect signals from every active strategy whose timeframe matches.
        # Each strategy is told its OWN current phantom position so its internal
        # "already in a position" guard works per-strategy, not fleet-wide.
        candidates: list[_Candidate] = []
        for rec in self.registry.list_active():
            inst = rec.instance
            if inst is None or inst.timeframe_minutes != tf:
                continue
            # Habitat gating: if a regime snapshot is in force, only call on_bar
            # for strategies eligible in this regime. Strategies still log
            # telemetry below — that's a "bar I saw but didn't fire on" case.
            if eligible is not None and rec.name not in eligible:
                self._telemetry.log(
                    bar=bar, timeframe=tf, strategy=rec.name,
                    signal=None, context=ctx_features,
                    position=0,
                    balance=starting_balance + state.realized_pnl,
                )
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
            # Telemetry write — every (strategy, bar) regardless of fire/no-fire.
            bar_event_id = self._telemetry.log(
                bar=bar, timeframe=tf, strategy=rec.name,
                signal=sig, context=ctx_features,
                position=strat_pos,
                balance=starting_balance + state.realized_pnl,
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
                # Calendar gate uses bar.t (not wall clock) so backtests and unit
                # tests are reproducible regardless of when they run.
                allowed, gate_reason = can_trade_now(bar.t.astimezone(CT))
                if not allowed:
                    if self.db:
                        self.db.log_event(
                            "calendar_block",
                            contract_id=contract_id, symbol="MES",
                            strategy=rec.name,
                            raw={"reason": gate_reason, "side": sig.side, "size": sig.size},
                        )
                    continue
                self._open_phantom_position(rec.name, sig, bar, contract_id, bar_event_id)
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
        bar_event_id: int | None = None,
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
            bar_event_id=bar_event_id,
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

    async def _honor_fleet_coordination(
        self, bar, tf: int, contract_id: str,
    ) -> None:
        """For each registered strategy whose instance declares
        `wants_force_flat(bar) == True` on this bar, close its open
        position(s) before regular signal processing runs.

        Live mode: broker.flatten_all() (the conductor holds at most one
        position, so the flatten is fleet-wide; we also reset our local
        position cache).

        Dry-run mode: synthesize a DryRunClose for each phantom position
        in this strategy's bucket, valued at this bar's close (the
        approximation a market-order flatten would settle near). Clear
        the bucket; feed the closes to PerfTracker so per-strategy
        scoring stays accurate."""
        from acme.conductor.dry_run import DryRunClose
        flattened: list[str] = []
        for rec in self.registry.list_all():
            inst = rec.instance
            if inst is None:
                continue
            if inst.timeframe_minutes != tf:
                continue
            wff = getattr(inst, "wants_force_flat", None)
            if wff is None:
                continue
            try:
                if not wff(bar):
                    continue
            except Exception as e:
                log.warning("wants_force_flat_raised",
                            strategy=rec.name, error=str(e))
                continue

            if self.dry_run:
                open_positions = self.dry_run_open.get(rec.name, [])
                if not open_positions:
                    continue
                for p in open_positions:
                    sign = 1 if p.side == "buy" else -1
                    price_pnl = sign * (bar.c - p.entry_price) * self.contract.point_value * p.size
                    net_pnl = round(price_pnl - self.round_turn_fee * p.size, 2)
                    cl = DryRunClose(
                        strategy=p.strategy or rec.name,
                        side=p.side,
                        size=p.size,
                        entry_price=p.entry_price,
                        exit_price=bar.c,
                        net_pnl=net_pnl,
                        outcome="fleet_coord_close",
                        closed_at=bar.t,
                        bar_event_id=p.bar_event_id,
                    )
                    self.perf.record_close(
                        strategy=cl.strategy, net_pnl=cl.net_pnl,
                        side=cl.side, outcome=cl.outcome,
                        entry_price=cl.entry_price, exit_price=cl.exit_price,
                        closed_at=cl.closed_at,
                    )
                    if self.db is not None:
                        try:
                            self.db.log_event(
                                "dry_run_close",
                                contract_id=contract_id, symbol="MES",
                                side="sell" if cl.side == "buy" else "buy",
                                size=cl.size, price=cl.exit_price,
                                strategy=cl.strategy,
                                raw={
                                    "outcome": cl.outcome,
                                    "entry_price": cl.entry_price,
                                    "exit_price": cl.exit_price,
                                    "net_pnl": cl.net_pnl,
                                    "entry_reason": p.reason,
                                    "bar_t": bar.t.isoformat(),
                                },
                            )
                        except Exception as e:
                            log.warning("fleet_coord_dryrun_close_log_failed",
                                        error=str(e))
                self.dry_run_open[rec.name] = []
                self.dry_run_position_per_strategy[rec.name] = 0
                flattened.append(rec.name)
            else:
                # Live mode: broker.flatten_all is fleet-wide (one position).
                if self.position != 0:
                    try:
                        await self.broker.flatten_all()
                    except Exception as e:
                        log.error("fleet_coord_live_flatten_failed",
                                  strategy=rec.name, error=str(e))
                        continue
                    self.position = 0
                    if self.db is not None:
                        try:
                            self.db.log_event(
                                "flatten_triggered",
                                contract_id=contract_id, symbol="MES",
                                strategy=rec.name,
                                raw={"reason": "fleet_coord_close",
                                     "dry_run": False},
                            )
                        except Exception as e:
                            log.warning("fleet_coord_live_log_failed",
                                        error=str(e))
                    flattened.append(rec.name)

        if flattened:
            log.info("fleet_coordination_close",
                     strategies=flattened, bar_t=bar.t.isoformat())

    async def _poll_kill_switch(self, contract_id: str) -> None:
        """Read new operator_events rows (id > _kill_switch_last_id).
        On a kill_switch_activated row we force-flat everything and
        set _kill_switch_active=True; on kill_switch_cleared we just
        flip the flag back. No-op if the table or client is missing
        (e.g. the conductor was constructed without a db)."""
        if self.db is None:
            return
        client = getattr(self.db, "client", None) or getattr(self.db, "sb", None)
        if client is None:
            return
        try:
            res = (
                client.table("operator_events").select("id, kind")
                .in_("kind", ["kill_switch_activated", "kill_switch_cleared"])
                .gt("id", self._kill_switch_last_id)
                .order("id", desc=False).limit(20).execute()
            )
            rows = res.data or []
        except Exception as e:
            log.warning("kill_switch_poll_failed", error=str(e))
            return

        if not rows:
            return

        for row in rows:
            rid = int(row.get("id") or 0)
            kind = row.get("kind") or ""
            if rid <= self._kill_switch_last_id:
                continue
            self._kill_switch_last_id = rid
            if kind == "kill_switch_activated" and not self._kill_switch_active:
                self._kill_switch_active = True
                log.warning("kill_switch_activated", event_id=rid)
                # Force-flat immediately regardless of any open positions.
                await self._flatten_position(
                    contract_id, requested_by="kill_switch"
                )
            elif kind == "kill_switch_cleared" and self._kill_switch_active:
                self._kill_switch_active = False
                log.info("kill_switch_cleared", event_id=rid)

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
