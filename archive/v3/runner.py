"""Multi-variant entry-point: one ProjectX connection, N parallel V3Runtime
instances, each running its own engine configuration.

Why multi-variant: today's analysis (docs/2026-05-05-trading-day-analysis.md)
showed v3-canon's PF degraded from OOS 2.21 to 1.08, primarily because the
filter is firing at cum_delta extremes far outside its calibrated range
(median |cum_delta| at entry was 16k vs 670 threshold) and opposite_signal
is exiting too eagerly (88% of exits, vs OOS 53%). Rather than picking one
fix, we ship four variant hypotheses alongside the canonical configuration
and let live shadow data compare them:

  - v3-canon       — control, OOS-validated config
  - v3-trail       — trailing stop / let-winners-run (option A from the brief)
  - v3-min2bar     — refuse opposite_signal exits before bar 2
  - v3-armor       — suppress opposite_signal exits when MFE >= 2 ATR
  - v3-pctile      — dynamic filter using 5th/95th percentile of recent cum_delta
  - v3.1-canon / -trail / -min2bar / -armor / -pctile — same engine flags as the
                     base v3-* siblings, plus the three safeguards from the
                     2026-05-07 win-loss anatomy: ATR ≤ 2.5 ceiling, hour
                     blacklist (06-08 and 11-12 CT), bar-1 fast-fail when
                     MAE > MFE × 1.5. Retroactively turns 2026-05-07 from
                     -$1,739 to +$321 across the family.
  - v4-loose-shorts — asymmetric pctile (5% longs, 12% shorts) — first short-bias
                     variant, added 2026-05-07 after the fleet fired 1,063 longs
                     vs 1 short over 4 days (see docs/2026-05-07-trading-day-analysis.md)
  - v4-trend-gate  — v3-canon entries gated by an EMA(20) trend classifier;
                     blocks counter-trend signals (skip-only, never inverts).
                     S1 from the 2026-05-07 post-mortem; PR-B in the v4 plan.
  - v4-overnight-bias — gates entries by Globex overnight direction (12-hour
                     buffer). S3 from the post-mortem; PR-C.
  - v4-vol-regime  — gates entries when realized vol is elevated + price is
                     directional (high-vol day = trending). S4; PR-C.
  - v4-trend-flip  — same EMA(20) trend classifier as v4-trend-gate, but
                     INVERTS counter-trend entries instead of skipping. S2
                     from the post-mortem; PR-D. Spicier than the gate
                     variants — only ship after the classifier has been
                     validated by the gate variants.
  - v5-mtf-anchor  — EMA(20) trend gate on 30-min bars resampled from our
                     2-min input. PR-F (follow-up to the v4 plan, sourced
                     from the Reddit-system review). Sees through 2-min
                     wiggle that the v4 trend classifier might over-react
                     to.

All variants share ONE ProjectXAdapter (SignalR fan-out at the broker layer
lets them all consume the same market hub connection). Each writes trades
tagged with its strategy_id; each has its own heartbeat / kill-switch row.

Modes:
  default   — submits real market orders
  --dry-run — phantom-position simulation; no orders submitted
"""

from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import dataclass
from typing import Any

import structlog

from acme.db import Db
from acme.ryan_spec.v3_runtime import (
    SESSION_END_CT,
    SESSION_OPEN_CT,
    V3Runtime,
    _parse_hhmm,
)

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class _VariantSpec:
    """One v3 variant's engine-flag configuration."""
    strategy_id: str
    description: str
    # engine flags (defaults match canonical)
    enable_trailing_stop: bool = False
    trail_be_lock_atr_mult: float = 1.0
    trail_atr_mult: float = 1.0
    min_bars_before_opposite_exit: int = 0
    opposite_signal_armor_mfe_atr: float | None = None
    filter_mode: str = "static"
    filter_pctile_window_bars: int = 60
    filter_pctile: float = 5.0
    # Asymmetric short-side pctile. None → symmetric (use filter_pctile for
    # both legs). Set to a wider value (e.g. 10) to make the short leg fire
    # more often on a market like MES where cum_delta drifts negative.
    filter_pctile_short: float | None = None
    # v4 regime gate. None → bare v3 engine, no regime override. Otherwise a
    # short string identifier resolved by `_resolve_classifier()` to a callable.
    # Backward-compatible default: every existing v3 variant has this None and
    # is unaffected by the wrapper.
    regime_classifier_name: str | None = None
    regime_gate_mode: str = "gate"
    # Buffer depth for the regime classifier. 60 bars = ~2h is enough for the
    # EMA(20)+10 trend classifier; deeper for vol-regime (needs 120-bar
    # baseline) and overnight (needs ~360 bars to span Globex into RTH).
    regime_history_bars: int = 60
    # Time-of-day exits. Defaults False across the fleet right now — see
    # 2026-05-06: user wants 24-hour shadow data with pure-thesis exits
    # (stop / opposite_signal only). Flip back to True per-variant if you
    # want to honor RTH session_end / time_stop again.
    enable_session_end_exit: bool = False
    enable_time_stop: bool = False
    # v3.1 refinements (PR-G — sourced from the 2026-05-07 win-loss
    # anatomy). All default to off. The v3.1-* variants flip them on; the
    # base v3-* variants stay bit-identical to their pre-PR behavior.
    entry_atr_ceiling: float | None = None
    entry_hour_blacklist_ct: tuple[int, ...] = ()
    enable_bar1_fast_fail: bool = False
    bar1_fast_fail_mae_mfe_ratio: float = 1.5


# The fleet. To disable a variant, comment it out or set ACME_V3_VARIANTS env
# var to a comma-separated subset of strategy_ids.
VARIANTS: list[_VariantSpec] = [
    _VariantSpec(
        strategy_id="v3-canon",
        description="Canonical OOS-validated configuration (control)",
    ),
    _VariantSpec(
        strategy_id="v3-trail",
        description="Trailing stop: BE-lock at +1 ATR, trail 1 ATR behind MFE past +2 ATR",
        enable_trailing_stop=True,
        trail_be_lock_atr_mult=1.0,
        trail_atr_mult=1.0,
    ),
    _VariantSpec(
        strategy_id="v3-min2bar",
        description="Refuse opposite_signal exit before bar 2 — drops 1-bar noise trades",
        min_bars_before_opposite_exit=2,
    ),
    _VariantSpec(
        strategy_id="v3-armor",
        description="Suppress opposite_signal when MFE >= 2 ATR — let winners run past first reversal",
        opposite_signal_armor_mfe_atr=2.0,
    ),
    _VariantSpec(
        strategy_id="v3-pctile",
        description="Percentile-based filter: bottom 5% of last 60 bars instead of static -670",
        filter_mode="pctile",
        filter_pctile_window_bars=60,
        filter_pctile=5.0,
    ),
    # ─── v3.1 family (PR-G) ────────────────────────────────────────────
    # Each v3.1-* mirrors its v3-* counterpart's engine flags AND adds the
    # three refinements from the 2026-05-07 win-loss anatomy
    # (docs/2026-05-07-v3-win-loss-anatomy.md):
    #   • entry_atr_ceiling=2.5 — losses fired at median ATR 2.36-3.67;
    #     wins at 2.02-2.27. A 2.5 ceiling cleanly separates the cohorts.
    #   • entry_hour_blacklist_ct=(6,7,8,11,12) — hours where every
    #     variant ran 7-20% WR. Mostly pre-RTH ramp + mid-morning chop.
    #   • enable_bar1_fast_fail=True — bar-1 cut when MAE > MFE × 1.5.
    #     Wins have MAE ≪ MFE by bar 1; losses already inverted.
    # Retroactively applied to 2026-05-07 the fleet flips from -$1,739 to
    # +$321 (v3-armor stays red even after the gate, expected). Live data
    # over the next sessions will tell us whether this generalizes.
    _VariantSpec(
        strategy_id="v3.1-canon",
        description="v3-canon + ATR≤2.5 entry / hour gate / bar-1 fast-fail",
        entry_atr_ceiling=2.5,
        entry_hour_blacklist_ct=(6, 7, 8, 11, 12),
        enable_bar1_fast_fail=True,
    ),
    _VariantSpec(
        strategy_id="v3.1-trail",
        description="v3-trail + ATR≤2.5 entry / hour gate / bar-1 fast-fail",
        enable_trailing_stop=True,
        trail_be_lock_atr_mult=1.0,
        trail_atr_mult=1.0,
        entry_atr_ceiling=2.5,
        entry_hour_blacklist_ct=(6, 7, 8, 11, 12),
        enable_bar1_fast_fail=True,
    ),
    _VariantSpec(
        strategy_id="v3.1-min2bar",
        description="v3-min2bar + ATR≤2.5 entry / hour gate / bar-1 fast-fail",
        min_bars_before_opposite_exit=2,
        entry_atr_ceiling=2.5,
        entry_hour_blacklist_ct=(6, 7, 8, 11, 12),
        enable_bar1_fast_fail=True,
    ),
    _VariantSpec(
        strategy_id="v3.1-armor",
        description="v3-armor + ATR≤2.5 entry / hour gate / bar-1 fast-fail",
        opposite_signal_armor_mfe_atr=2.0,
        entry_atr_ceiling=2.5,
        entry_hour_blacklist_ct=(6, 7, 8, 11, 12),
        enable_bar1_fast_fail=True,
    ),
    _VariantSpec(
        strategy_id="v3.1-pctile",
        description="v3-pctile + ATR≤2.5 entry / hour gate / bar-1 fast-fail",
        filter_mode="pctile",
        filter_pctile_window_bars=60,
        filter_pctile=5.0,
        entry_atr_ceiling=2.5,
        entry_hour_blacklist_ct=(6, 7, 8, 11, 12),
        enable_bar1_fast_fail=True,
    ),
    # 2026-05-07 post-mortem (docs/2026-05-07-trading-day-analysis.md): the
    # static filter is structurally long-biased on MES because cum_delta drifts
    # negative — over 4 days the fleet fired 1,063 longs vs 1 short. This
    # variant loosens the short leg to top 12% (vs the default 5%) so shorts
    # actually qualify, while keeping the long leg at the canonical 5%. First
    # of the v4 series (S5 from the post-mortem).
    _VariantSpec(
        strategy_id="v4-loose-shorts",
        description="Asymmetric pctile: bottom 5% longs, top 12% shorts",
        filter_mode="pctile",
        filter_pctile_window_bars=60,
        filter_pctile=5.0,
        filter_pctile_short=12.0,
    ),
    # PR-B of the v4 plan (S1 from the post-mortem). Wraps v3-canon entry
    # logic with an EMA-trend regime classifier; blocks counter-trend entries
    # but does NOT flip them. On a strong-trend day like 2026-05-07 this would
    # have skipped the catastrophic counter-trend longs entirely. Conservative
    # — can only ever reduce trade count, never invert direction.
    _VariantSpec(
        strategy_id="v4-trend-gate",
        description="EMA(20) trend gate: skip v3 entries that go against the recent regime",
        regime_classifier_name="trend_ema",
        regime_gate_mode="gate",
    ),
    # PR-C of the v4 plan (S3). Mechanizes "I knew today was a short day"
    # by inferring the day's bias from the direction of the buffer window
    # (~12 hours / 360 bars covers Globex overnight into the current RTH).
    # Blocks counter-bias entries; same skip-only safety as v4-trend-gate.
    _VariantSpec(
        strategy_id="v4-overnight-bias",
        description="Overnight Globex direction gate: skip v3 entries against the day's bias",
        regime_classifier_name="overnight_bias",
        regime_gate_mode="gate",
        regime_history_bars=360,
    ),
    # PR-C of the v4 plan (S4). High realized vol typically coincides with
    # directional days (today = +50% ATR vs the 5/5 baseline). When vol is
    # elevated AND price is moving, treat the move as the regime; otherwise
    # treat as chop. 120-bar baseline.
    _VariantSpec(
        strategy_id="v4-vol-regime",
        description="High-vol regime gate: skip v3 counter-trend entries when vol expanded + directional",
        regime_classifier_name="vol_regime",
        regime_gate_mode="gate",
        regime_history_bars=120,
    ),
    # PR-D of the v4 plan (S2 — the spicy one). Same EMA(20) trend
    # classifier as v4-trend-gate, but instead of *skipping* counter-trend
    # entries we *invert* them: long signal in trend_down → short entry,
    # stop pivoted symmetrically. The thesis: at cum_delta extremes during
    # a strong-trend day, the extreme is *continuation* not *exhaustion*.
    # Highest upside if the regime classifier is reliable; active wrong-side
    # trades if it misclassifies a chop day. Ride v4-trend-gate alongside
    # as the safer cousin.
    _VariantSpec(
        strategy_id="v4-trend-flip",
        description="EMA(20) trend flip: invert v3 entries that go against the recent regime",
        regime_classifier_name="trend_ema",
        regime_gate_mode="flip",
    ),
    # PR-F (followup, sourced from the Reddit S&R bot review at
    # docs/2026-05-07-trading-day-analysis.md). Same EMA(20) trend gate as
    # v4-trend-gate but applied to a higher timeframe — 30-min bars
    # resampled from our 2-min input. A 2-min cum_delta extreme during a
    # FLAT 30-min regime is "real" mean-reversion territory; the same
    # extreme during a strong 30-min trend is the fade-into-trend trap.
    # This variant catches the latter when the 2-min EMA is too twitchy
    # to see it. regime_history_bars=480 covers EMA(20)+10 lookback on
    # the resampled series (~16h of buffer).
    _VariantSpec(
        strategy_id="v5-mtf-anchor",
        description="30-min EMA trend gate (resampled from 2-min): skip v3 entries against the higher-TF regime",
        regime_classifier_name="higher_tf_alignment",
        regime_gate_mode="gate",
        regime_history_bars=480,
    ),
]


# Maps the short-string identifier in `_VariantSpec.regime_classifier_name`
# to the actual callable. New classifiers (overnight, vol_regime, ...) added
# in PR-C just append rows here. Keeping the registry centralised (rather
# than embedding callables in the spec) keeps the spec dataclass simple
# and human-readable.
_CLASSIFIER_REGISTRY: dict[str, Any] = {}


def _resolve_classifier(name: str | None) -> Any:
    if name is None:
        return None
    if not _CLASSIFIER_REGISTRY:
        # Lazy import: keep ryan_spec.* off the module-import path for env-
        # less unit tests that import `acme.runner` to inspect VARIANTS.
        from acme.ryan_spec.v4_regime import (
            classify_higher_tf_alignment,
            classify_overnight_bias,
            classify_trend_ema,
            classify_vol_regime,
        )
        _CLASSIFIER_REGISTRY["trend_ema"] = classify_trend_ema
        _CLASSIFIER_REGISTRY["overnight_bias"] = classify_overnight_bias
        _CLASSIFIER_REGISTRY["vol_regime"] = classify_vol_regime
        _CLASSIFIER_REGISTRY["higher_tf_alignment"] = classify_higher_tf_alignment
    if name not in _CLASSIFIER_REGISTRY:
        raise SystemExit(
            f"Unknown regime_classifier_name={name!r}. "
            f"Known: {sorted(_CLASSIFIER_REGISTRY)}"
        )
    return _CLASSIFIER_REGISTRY[name]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Acme Futures multi-variant runner")
    p.add_argument("--dry-run", action="store_true",
                   help="Log intended orders to Supabase without submitting them")
    return p.parse_args()


def _select_variants() -> list[_VariantSpec]:
    """Filter the fleet to a subset if ACME_V3_VARIANTS is set."""
    requested = os.getenv("ACME_V3_VARIANTS", "").strip()
    if not requested:
        return VARIANTS
    wanted = {s.strip() for s in requested.split(",") if s.strip()}
    selected = [v for v in VARIANTS if v.strategy_id in wanted]
    if not selected:
        raise SystemExit(
            f"ACME_V3_VARIANTS={requested!r} matched no known variant. "
            f"Known: {[v.strategy_id for v in VARIANTS]}"
        )
    return selected


def _build_runtime(
    spec: _VariantSpec, broker: Any, db: Db, *, dry_run: bool,
) -> V3Runtime:
    """Construct one V3Runtime configured per the variant spec."""
    contract = os.getenv("ACME_CONTRACT_SYMBOL", "MES")
    delta_source = os.getenv("ACME_DELTA_SOURCE", "quote")
    if delta_source not in ("quote", "trade"):
        raise SystemExit(f"Invalid ACME_DELTA_SOURCE={delta_source!r}")
    risk = int(os.getenv("ACME_RISK_CONTRACTS", "1") or "1")
    session_open = _parse_hhmm(
        os.getenv("ACME_SESSION_OPEN_CT") or SESSION_OPEN_CT.strftime("%H:%M"),
        field_name="ACME_SESSION_OPEN_CT",
    )
    session_end = _parse_hhmm(
        os.getenv("ACME_SESSION_END_CT") or SESSION_END_CT.strftime("%H:%M"),
        field_name="ACME_SESSION_END_CT",
    )
    return V3Runtime(
        broker=broker, db=db,
        contract_symbol=contract,
        mode="paper" if dry_run else "live",
        delta_source=delta_source,  # type: ignore[arg-type]
        risk_contracts=risk,
        session_open_ct=session_open,
        session_end_ct=session_end,
        dry_run=dry_run,
        strategy_id=spec.strategy_id,
        enable_trailing_stop=spec.enable_trailing_stop,
        trail_be_lock_atr_mult=spec.trail_be_lock_atr_mult,
        trail_atr_mult=spec.trail_atr_mult,
        min_bars_before_opposite_exit=spec.min_bars_before_opposite_exit,
        opposite_signal_armor_mfe_atr=spec.opposite_signal_armor_mfe_atr,
        filter_mode=spec.filter_mode,  # type: ignore[arg-type]
        filter_pctile_window_bars=spec.filter_pctile_window_bars,
        filter_pctile=spec.filter_pctile,
        filter_pctile_short=spec.filter_pctile_short,
        enable_session_end_exit=spec.enable_session_end_exit,
        enable_time_stop=spec.enable_time_stop,
        entry_atr_ceiling=spec.entry_atr_ceiling,
        entry_hour_blacklist_ct=spec.entry_hour_blacklist_ct,
        enable_bar1_fast_fail=spec.enable_bar1_fast_fail,
        bar1_fast_fail_mae_mfe_ratio=spec.bar1_fast_fail_mae_mfe_ratio,
        regime_classifier=_resolve_classifier(spec.regime_classifier_name),
        regime_gate_mode=spec.regime_gate_mode,  # type: ignore[arg-type]
        regime_history_bars=spec.regime_history_bars,
    )


async def _amain(dry_run: bool) -> None:
    from acme.broker.projectx import ProjectXAdapter

    db = Db()
    broker = ProjectXAdapter()
    variants = _select_variants()

    runtimes = [
        _build_runtime(spec, broker, db, dry_run=dry_run) for spec in variants
    ]
    log.info(
        "runner_starting_multi_variant",
        dry_run=dry_run,
        variants=[r.strategy_id for r in runtimes],
        count=len(runtimes),
    )
    for spec in variants:
        log.info("variant_enabled", strategy_id=spec.strategy_id,
                 description=spec.description)

    try:
        # TaskGroup propagates exceptions and cancels siblings on any failure.
        # Each runtime opens its own quote stream consumer queue against the
        # adapter's shared SignalR connection — the fan-out happens inside
        # ProjectXAdapter._market_mux.
        async with asyncio.TaskGroup() as tg:
            for rt in runtimes:
                tg.create_task(rt.run(), name=f"runtime:{rt.strategy_id}")
    finally:
        await broker.aclose()


def main() -> None:
    args = _parse_args()
    if args.dry_run:
        log.info("runner_dry_run_mode", note="orders will be LOGGED but NOT submitted")
    try:
        asyncio.run(_amain(dry_run=args.dry_run))
    except KeyboardInterrupt:
        log.info("runner_stopped_by_user")


if __name__ == "__main__":
    main()
