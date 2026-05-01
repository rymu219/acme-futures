"""Signal arbitrator.

When multiple strategies fire on the same bar, the conductor needs ONE winner
to execute (or, in dry-run, to log as the "would-have-traded" choice). The
arbitrator computes a composite score per candidate and picks the highest:

    composite = 3.0 * tier_weight
              + 2.0 * regime_fit
              + 1.5 * time_bucket_fit
              + 1.0 * confidence

  - tier_weight: 1.0 (tier 1), 0.66 (tier 2), 0.33 (tier 3)
  - regime_fit: from strategy metadata, given the current RegimeSnapshot
  - time_bucket_fit: 1.0 if current CT time is in the strategy's preferred
    buckets, else 0.5
  - confidence: live from the perf registry [0, 1]

Tie-breaker: lower strategy name lexicographically (deterministic).

`arbitrate_b1` is preserved for backward-compat tests but the conductor uses
`arbitrate_b3` (this module's main function) at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from acme.strategies.base import RegimeLabel, Signal, StrategyMetadata

Direction = Literal["buy", "sell"]
CT = ZoneInfo("America/Chicago")


@dataclass(frozen=True)
class ScoredSignal:
    strategy: str
    signal: Signal
    composite_score: float
    score_breakdown: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ArbitrationResult:
    winner: ScoredSignal | None
    suppressed: list[ScoredSignal] = field(default_factory=list)
    notes: str = ""

    @property
    def has_winner(self) -> bool:
        return self.winner is not None

    @property
    def has_actionable_winner(self) -> bool:
        return self.winner is not None and self.winner.signal.size > 0


@dataclass(frozen=True)
class ArbitrationContext:
    """Read-only context the conductor passes per bar so the arbitrator can
    score each candidate without reaching into other modules.
    """
    regime: RegimeLabel | None     # None during early warmup; treated as 'quiet'
    now_ct: datetime
    confidences: dict[str, float]   # strategy name -> confidence in [0,1]


@dataclass(frozen=True)
class _Candidate:
    name: str
    signal: Signal
    metadata: StrategyMetadata
    tier: int


_TIER_WEIGHTS = {1: 1.0, 2: 0.66, 3: 0.33}


def _tier_weight(tier: int) -> float:
    return _TIER_WEIGHTS.get(tier, 0.33)


def _time_bucket_fit(now_ct: datetime, buckets: list[str]) -> float:
    """1.0 if the current CT time falls inside any of the strategy's preferred
    buckets, else 0.5. Bucket format: 'HH:MM-HH:MM' (CT, 24h).
    """
    if not buckets:
        return 0.5
    cur = now_ct.astimezone(CT).time()
    for bucket in buckets:
        try:
            start_s, end_s = bucket.split("-")
            sh, sm = (int(x) for x in start_s.split(":"))
            eh, em = (int(x) for x in end_s.split(":"))
        except (ValueError, IndexError):
            continue
        from datetime import time as _time
        start = _time(sh, sm)
        end = _time(eh, em)
        if start <= cur <= end:
            return 1.0
    return 0.5


def _regime_fit(metadata: StrategyMetadata, regime: RegimeLabel | None) -> float:
    if regime is None:
        # Treat warmup as 'quiet' for arbitration purposes (don't punish trend
        # bots before the regime classifier has data).
        regime = "quiet"
    return float(metadata.regime_fit.get(regime, 0.0))


def _composite_score(c: _Candidate, ctx: ArbitrationContext) -> tuple[float, dict[str, float]]:
    tw = _tier_weight(c.tier)
    rf = _regime_fit(c.metadata, ctx.regime)
    tb = _time_bucket_fit(ctx.now_ct, c.metadata.time_buckets)
    conf = float(ctx.confidences.get(c.name, 0.0))
    breakdown = {
        "tier_weight": round(tw, 4),
        "regime_fit": round(rf, 4),
        "time_bucket_fit": round(tb, 4),
        "confidence": round(conf, 4),
    }
    composite = 3.0 * tw + 2.0 * rf + 1.5 * tb + 1.0 * conf
    return composite, breakdown


def arbitrate(
    candidates: list[_Candidate],
    ctx: ArbitrationContext,
) -> ArbitrationResult:
    """Composite-score arbitration. The first call's lexicographic tie-breaker
    keeps the result deterministic when scores tie exactly.
    """
    if not candidates:
        return ArbitrationResult(winner=None, notes="no_signals")
    scored: list[tuple[float, dict[str, float], _Candidate]] = []
    for cand in candidates:
        composite, breakdown = _composite_score(cand, ctx)
        scored.append((composite, breakdown, cand))
    # Sort by (-score, name) so highest score first; ties resolved by name asc
    scored.sort(key=lambda t: (-t[0], t[2].name))
    winner_score, winner_breakdown, winner_cand = scored[0]
    winner = ScoredSignal(
        strategy=winner_cand.name,
        signal=winner_cand.signal,
        composite_score=round(winner_score, 4),
        score_breakdown=winner_breakdown,
    )
    suppressed = [
        ScoredSignal(
            strategy=cand.name, signal=cand.signal,
            composite_score=round(score, 4),
            score_breakdown=breakdown,
        )
        for score, breakdown, cand in scored[1:]
    ]
    return ArbitrationResult(winner=winner, suppressed=suppressed)


# ---------- Backward-compat shim for B1-era tests ----------

def arbitrate_b1(
    candidates: list[tuple[str, Signal]],
) -> ArbitrationResult:
    """B1 fallback: single-strategy pass-through with lexicographic tie-break.
    Preserved so existing tests still pass; new code should use `arbitrate`.
    """
    if not candidates:
        return ArbitrationResult(winner=None, notes="no_signals")
    sorted_candidates = sorted(candidates, key=lambda c: c[0])
    name, sig = sorted_candidates[0]
    winner = ScoredSignal(
        strategy=name, signal=sig,
        composite_score=1.0,
        score_breakdown={"b1_passthrough": 1.0},
    )
    suppressed = [
        ScoredSignal(
            strategy=n, signal=s,
            composite_score=0.0,
            score_breakdown={"b1_single_strategy_only": 0.0},
        )
        for n, s in sorted_candidates[1:]
    ]
    return ArbitrationResult(winner=winner, suppressed=suppressed)
