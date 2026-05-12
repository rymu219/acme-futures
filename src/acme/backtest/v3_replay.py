"""Backtest harness for the v3 / v3.1 / v4 / v5 fleet.

The seed-fleet harness in `bar_replay.py` is built around the old
`Strategy.on_bar(...) → Signal` interface and bracket fills. The Ryan-Spec
v3 engine has a fundamentally different shape:

  RyanSpecV3Engine.on_bar(bar, *, bar_delta, cum_delta_session) → Decision
  Decisions become positions via engine.open_position(...) / close_position()
  Exits are emitted by the engine itself (stop / opposite_signal / etc.)

This module:

1. Aggregates 1-min cached Databento bars into 2-min bars (the engine's
   native cadence).
2. Synthesizes a `cum_delta_session` proxy from each bar's OHLCV (the live
   runner uses quote-tick reconstruction; we don't have ticks in cache).
3. Drives all 16 variant engines from a single bar stream — same bars,
   different gates, fair A/B.
4. Captures per-trade outcomes (entry/exit, MFE/MAE, exit_reason) for
   downstream stats.

The cum_delta synthesis is the biggest fidelity gap vs live. Live uses
last-trade-vs-mid quote ticks; we derive direction-of-pressure from the
bar's close-position-in-range scaled by volume. Acceptable proxy for
backtest-grade comparisons; not pretending to be tick-exact.
"""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from datetime import time as dtime
from typing import Any

from acme.broker.base import Bar
from acme.calendar import CT, topstep_trading_date
from acme.contracts import MES
from acme.ryan_spec.v3_engine import RyanSpecV3Engine
from acme.ryan_spec.v3_tick_delta import SESSION_OPEN_CT
from acme.ryan_spec.v4_engine import V4GatedEngine

ROUND_TURN_COMMISSION = 0.70  # matches v3_runtime constant


# ----- bar aggregation ---------------------------------------------------

def aggregate_to_2min(bars_1m: Iterator[Bar]) -> Iterator[Bar]:
    """Combine consecutive 1-min bars into 2-min bars.

    Buckets are aligned to even minutes (00:00, 00:02, ...). A 2-min bar
    starting at 00:00 covers the 00:00 and 00:01 1-min bars.
    """
    pair: list[Bar] = []
    for b in bars_1m:
        if pair and (b.t.minute % 2 == 0):
            yield _merge_bars(pair)
            pair = [b]
        else:
            pair.append(b)
    if pair:
        yield _merge_bars(pair)


def _merge_bars(group: list[Bar]) -> Bar:
    return Bar(
        t=group[0].t,
        o=group[0].o,
        h=max(b.h for b in group),
        l=min(b.l for b in group),
        c=group[-1].c,
        v=sum(b.v for b in group),
    )


# ----- cum_delta synthesis ----------------------------------------------

def synth_bar_delta(bar: Bar) -> int:
    """Approximate the bar's signed delta from OHLCV.

    Maps close position within [low, high] to [-1, +1], multiplies by volume.
    Bar that closed at high → +volume. Closed at low → -volume. Closed at
    midpoint → 0. Bars with zero range (low == high) emit 0.
    """
    if bar.h <= bar.l:
        return 0
    pos = (bar.c - bar.l) / (bar.h - bar.l)   # 0..1
    bias = 2.0 * pos - 1.0                     # -1..+1
    return int(round(bar.v * bias))


# ----- engine construction (variant spec → engine instance) -------------

def build_engine_from_spec(spec: Any) -> RyanSpecV3Engine | V4GatedEngine:
    """Build the right engine for a `_VariantSpec` (defined in acme.runner).

    Mirrors `_build_runtime` in runner.py but skips the broker/Db/runtime
    layers — we only need the bar-driving engine.
    """
    # All v3-style engine kwargs (passed identically whether wrapped or not)
    engine_kwargs: dict[str, Any] = dict(
        # delta_source defaults to "quote" → unit-weight threshold of -670.
        # The synth_bar_delta produces unit-weight values too (volume-scaled,
        # not size-weighted), so the unit-weighted threshold is what we want.
        filter_thresh=-670,
        enable_trailing_stop=spec.enable_trailing_stop,
        trail_be_lock_atr_mult=spec.trail_be_lock_atr_mult,
        trail_atr_mult=spec.trail_atr_mult,
        min_bars_before_opposite_exit=spec.min_bars_before_opposite_exit,
        opposite_signal_armor_mfe_atr=spec.opposite_signal_armor_mfe_atr,
        filter_mode=spec.filter_mode,
        filter_pctile_window_bars=spec.filter_pctile_window_bars,
        filter_pctile=spec.filter_pctile,
        filter_pctile_short=spec.filter_pctile_short,
        enable_session_end_exit=spec.enable_session_end_exit,
        enable_time_stop=spec.enable_time_stop,
        entry_atr_ceiling=spec.entry_atr_ceiling,
        entry_hour_blacklist_ct=spec.entry_hour_blacklist_ct,
        enable_bar1_fast_fail=spec.enable_bar1_fast_fail,
        bar1_fast_fail_mae_mfe_ratio=spec.bar1_fast_fail_mae_mfe_ratio,
    )

    if spec.regime_classifier_name is None:
        return RyanSpecV3Engine(**engine_kwargs)

    # v4 / v5 wrapped engine. Resolve the classifier callable.
    from acme.runner import _resolve_classifier
    classifier = _resolve_classifier(spec.regime_classifier_name)
    return V4GatedEngine(
        classifier=classifier,
        gate_mode=spec.regime_gate_mode,
        regime_history_bars=spec.regime_history_bars,
        **engine_kwargs,
    )


# ----- trade record ------------------------------------------------------

@dataclass
class BacktestTrade:
    strategy_id: str
    direction: str
    entry_t: datetime
    exit_t: datetime
    entry_price: float
    exit_price: float
    exit_reason: str
    pnl_dollars: float
    bars_held: int
    atr_at_entry: float
    cum_delta_at_entry: int
    mfe_atr: float | None = None
    mae_atr: float | None = None


@dataclass
class BacktestResult:
    bars_processed: int
    bars_2m: int
    trades: dict[str, list[BacktestTrade]] = field(default_factory=dict)


# ----- session-aware cum_delta tracker ----------------------------------

class _CumDeltaSession:
    """Maintains a session-cumulative delta that resets at 08:30 CT each day.

    Mirrors the live runner's `LiveBarDeltaBuilder._maybe_reset_session`
    semantics so the engine sees the same cum_delta_session shape it gets
    in production.
    """

    def __init__(self, session_open_ct: dtime = SESSION_OPEN_CT) -> None:
        self._open = session_open_ct
        self._cum: int = 0
        self._last_session_date = None

    def update(self, bar_t: datetime, bar_delta: int) -> int:
        """Apply this bar's delta and return the post-update cum_delta_session."""
        # Compute Topstep trading date in CT to detect session crossings.
        bar_ct = bar_t.astimezone(CT)
        td = topstep_trading_date(bar_ct)
        # Reset when (a) we crossed into a new trading date AND (b) the bar
        # is at-or-after session open. Bars before 08:30 CT on a new trading
        # date are still "overnight extending the prior session" for our
        # purposes — same convention v3_tick_delta uses.
        if (self._last_session_date is None or td != self._last_session_date) \
                and bar_ct.time() >= self._open:
            self._cum = 0
            self._last_session_date = td
        self._cum += bar_delta
        return self._cum


# ----- main replay -------------------------------------------------------

def replay(
    bars_1m: Iterator[Bar],
    variants: Sequence[Any],
) -> BacktestResult:
    """Drive every variant through the bar stream.

    `variants` is a sequence of `_VariantSpec` instances (from acme.runner).
    Returns a BacktestResult with one trade list per strategy_id.
    """
    engines: dict[str, RyanSpecV3Engine | V4GatedEngine] = {
        spec.strategy_id: build_engine_from_spec(spec) for spec in variants
    }
    trades: dict[str, list[BacktestTrade]] = {sid: [] for sid in engines}

    # Track open position per variant. Stored in the engine itself via
    # `engine.position`, but we mirror the entry context for trade-record
    # construction at exit time.
    pending: dict[str, dict[str, Any]] = {sid: {} for sid in engines}

    delta_session = _CumDeltaSession()
    bars_2m_count = 0
    bars_processed = 0

    for bar_2m in aggregate_to_2min(bars_1m):
        bars_2m_count += 1
        bars_processed += 2  # each 2m comes from two 1m bars (approx; trailing partial OK)
        bar_delta = synth_bar_delta(bar_2m)
        cum_delta_session = delta_session.update(bar_2m.t, bar_delta)

        for sid, engine in engines.items():
            d = engine.on_bar(
                bar_2m,
                bar_delta=bar_delta,
                cum_delta_session=cum_delta_session,
            )
            if d.action == "enter":
                # Engine recommends entry; open the phantom position.
                engine.open_position(
                    direction=d.direction,
                    entry_ts=d.bar_ts,
                    entry_fill_price=d.entry_price,
                    atr_at_entry=d.atr_at_entry,
                    cum_delta_at_entry=d.cum_delta_at_entry,
                )
                pending[sid] = {
                    "direction": d.direction,
                    "entry_t": d.bar_ts,
                    "entry_price": d.entry_price,
                    "atr_at_entry": d.atr_at_entry,
                    "cum_delta_at_entry": d.cum_delta_at_entry,
                    "entry_bars": bars_2m_count,
                    "stop_price": d.stop_price,
                }
            elif d.action == "exit" and pending[sid]:
                # Settle the phantom trade. Exit price depends on reason:
                #   stop  → fill at the engine's CURRENT stop_price (which may
                #           have been ratcheted by trail-stop logic; using the
                #           entry-time stop_price would mis-price every trail
                #           variant's winners as full stops)
                #   other → bar close (engine's on_bar fired at close)
                ctx = pending[sid]
                pos = engine.position  # snapshot before close_position clears it
                exit_price = (
                    pos.stop_price
                    if d.reason == "stop" and pos is not None
                    else bar_2m.c
                )
                sign = 1 if ctx["direction"] == "long" else -1
                gross = (exit_price - ctx["entry_price"]) * sign * MES.point_value
                net = gross - ROUND_TURN_COMMISSION

                # MFE/MAE — captured before close_position clears the engine state.
                mfe_atr = (pos.max_favorable_excursion / pos.atr_at_entry
                           if pos and pos.atr_at_entry > 0 else None)
                mae_atr = (pos.max_adverse_excursion / pos.atr_at_entry
                           if pos and pos.atr_at_entry > 0 else None)

                trades[sid].append(BacktestTrade(
                    strategy_id=sid,
                    direction=ctx["direction"],
                    entry_t=ctx["entry_t"],
                    exit_t=d.bar_ts or bar_2m.t,
                    entry_price=ctx["entry_price"],
                    exit_price=exit_price,
                    exit_reason=d.reason,
                    pnl_dollars=net,
                    bars_held=bars_2m_count - ctx["entry_bars"],
                    atr_at_entry=ctx["atr_at_entry"],
                    cum_delta_at_entry=ctx["cum_delta_at_entry"],
                    mfe_atr=mfe_atr,
                    mae_atr=mae_atr,
                ))
                engine.close_position()
                pending[sid] = {}

    return BacktestResult(
        bars_processed=bars_processed,
        bars_2m=bars_2m_count,
        trades=trades,
    )
