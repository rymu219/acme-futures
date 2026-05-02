"""Habitat matching — given a regime, which strategies are eligible to fire?

Reads each strategy's existing `metadata.regime_fit` (declared in
`StrategyMetadata` on the strategy class). No parallel hardcoded habitat map —
the strategy is the source of truth for its own habitat preferences.

Mapping from regime label → regime_fit key:
  trending     → 'trending'
  ranging      → 'ranging'
  compressing  → []           (watch only — brief says no fires here)
  chaotic      → []           (all silent)
  ambiguous    → []           (conservative — defer)

Eligibility threshold: regime_fit[regime] >= 0.7
"""

from __future__ import annotations

from acme.registry import StrategyRegistry

ELIGIBILITY_THRESHOLD = 0.7

REGIME_TO_FIT_KEY = {
    "trending": "trending",
    "ranging": "ranging",
}


def eligible_strategies(
    registry: StrategyRegistry,
    regime: str,
    confidence: float,
    *,
    min_confidence: float = 0.4,
) -> list[str]:
    """Return active strategy names eligible to fire in this regime.

    Returns [] when:
      - regime is compressing/chaotic/ambiguous
      - confidence < min_confidence
      - no active strategy declares regime_fit[regime] >= 0.7
    """
    if confidence < min_confidence:
        return []
    fit_key = REGIME_TO_FIT_KEY.get(regime)
    if fit_key is None:
        return []
    eligible: list[str] = []
    for rec in registry.list_active():
        meta = rec.metadata
        if meta is None:
            continue
        fit = meta.regime_fit.get(fit_key, 0.0)
        if fit >= ELIGIBILITY_THRESHOLD:
            eligible.append(rec.name)
    return eligible


def habitat_match(strategy_metadata, regime: str) -> bool:
    """True if this regime is in the strategy's declared habitat.

    Used by analytics to flag matrix cells where a strategy's declared habitat
    aligns with the observed regime — those cells should show positive
    expectancy if the habitat declaration is correct.
    """
    fit_key = REGIME_TO_FIT_KEY.get(regime)
    if fit_key is None:
        return False
    fit = strategy_metadata.regime_fit.get(fit_key, 0.0)
    return fit >= ELIGIBILITY_THRESHOLD
