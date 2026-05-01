"""Confidence scoring for the strategy fleet.

The confidence score is a value in [0, 1] computed from a strategy's rolling
performance metrics. Three uses (per the Phase B plan):

  1. Arbitration weight — when multiple strategies fire on the same bar,
     the conductor weights each candidate's composite score by its confidence.
  2. Promotion eligibility — SHADOW → PILOT requires confidence ≥ 0.55 for
     ≥3 consecutive snapshots; PILOT → LIVE requires ≥ 0.65.
  3. Auto-bench trigger — confidence below 0.20 with n ≥ 10 → bench.

Formula (from the plan):
    confidence = clip(0, 1,
        0.40 * (sharpe / 2.0)
      + 0.25 * ((profit_factor - 1) / 1)
      + 0.15 * min(1, n_trades / 50)
      + 0.10 * (1 - current_dd / max_allowed_dd)
      + 0.10 * lifecycle_floor
    )

Each term is capped at the [0, 1] range BEFORE weighting, so the total is
always in [0, 1]. The `lifecycle_floor` term gives a conservative head-start
to strategies in higher lifecycle states (a fresh SHADOW strategy starts at
~0.03, while a LIVE strategy with the same metrics starts at ~0.07). It
prevents the scoring from being too punishing during cold-start.
"""

from __future__ import annotations

from acme.perf.tracker import PerfMetrics
from acme.strategies.base import LifecycleState

# Default tier-2 max-allowed-DD for confidence's DD-health term. We use a
# conservative $400 — the eval-mode daily loss cutoff. Strategies that take
# bigger losses see their confidence drop faster.
DEFAULT_MAX_ALLOWED_DD = 400.0

LIFECYCLE_FLOOR: dict[LifecycleState, float] = {
    "BACKTEST": 0.0,
    "REPLAY":   0.1,
    "SHADOW":   0.3,
    "PILOT":    0.5,
    "LIVE":     0.7,
    "BENCH":    0.0,
    "RETIRED":  0.0,
}


def _clip01(x: float) -> float:
    if x < 0:
        return 0.0
    if x > 1:
        return 1.0
    return x


def compute_confidence(
    metrics: PerfMetrics,
    lifecycle: LifecycleState,
    *,
    max_allowed_dd: float = DEFAULT_MAX_ALLOWED_DD,
) -> float:
    """Per-strategy confidence in [0, 1]. See module docstring for the formula."""
    # Each term clipped individually before weighting.
    sharpe_term = _clip01(metrics.sharpe / 2.0)
    # No losing trades yet → can't compute PF. Treat as neutral 0.5.
    pf_term = (
        0.5 if metrics.profit_factor is None
        else _clip01((metrics.profit_factor - 1.0) / 1.0)
    )
    sample_term = _clip01(metrics.n_trades / 50.0)
    dd_term = _clip01(1.0 - (metrics.max_drawdown / max_allowed_dd)) if max_allowed_dd > 0 else 0.0
    lifecycle_term = _clip01(LIFECYCLE_FLOOR.get(lifecycle, 0.0))

    score = (
        0.40 * sharpe_term
        + 0.25 * pf_term
        + 0.15 * sample_term
        + 0.10 * dd_term
        + 0.10 * lifecycle_term
    )
    return _clip01(score)


# ---------- Promotion / bench thresholds ----------

PROMOTION_THRESHOLDS = {
    ("SHADOW", "PILOT"): 0.55,
    ("PILOT",  "LIVE"):  0.65,
}

AUTO_BENCH_CONFIDENCE = 0.20
AUTO_BENCH_MIN_SAMPLE = 10


def is_eligible_for_promotion(
    confidence: float,
    from_state: LifecycleState,
    to_state: LifecycleState,
) -> bool:
    threshold = PROMOTION_THRESHOLDS.get((from_state, to_state))
    if threshold is None:
        return False
    return confidence >= threshold


def should_auto_bench(
    metrics: PerfMetrics,
    confidence: float,
    lifecycle: LifecycleState,
) -> tuple[bool, str]:
    """Hard floor: a LIVE/PILOT strategy whose confidence drops below
    AUTO_BENCH_CONFIDENCE with sample n >= AUTO_BENCH_MIN_SAMPLE goes to BENCH.
    Returns (should_bench, reason)."""
    if lifecycle not in ("PILOT", "LIVE"):
        return False, ""
    if metrics.n_trades < AUTO_BENCH_MIN_SAMPLE:
        return False, ""
    if confidence < AUTO_BENCH_CONFIDENCE:
        return True, f"confidence={confidence:.3f} below floor {AUTO_BENCH_CONFIDENCE}"
    return False, ""
