"""Habitat eligibility tests — guards against the brief's anti mis-classification.

Critical assertion: anti must be eligible in TRENDING (Raschke pullback-in-trend),
NOT in RANGING. The brief had this wrong; if someone re-imports those constants,
this test should fail loudly.
"""

import pytest

from acme.contracts import MES
from acme.regime.habitat import (
    ELIGIBILITY_THRESHOLD,
    REGIME_TO_FIT_KEY,
    eligible_strategies,
    habitat_match,
)
from acme.registry import StrategyRegistry
from acme.strategies.anti import AntiStrategy
from acme.strategies.bb_mr import BollingerMeanReversionStrategy
from acme.strategies.donchian import DonchianBreakoutStrategy
from acme.strategies.ema_cross import EmaCrossStrategy
from acme.strategies.orb import OpeningRangeBreakoutStrategy
from acme.strategies.supertrend import SupertrendStrategy
from acme.strategies.turtle_soup import TurtleSoupStrategy
from acme.strategies.turtles_system2 import TurtlesSystem2Strategy

ALL_SEEDS = [
    ("ema_cross", EmaCrossStrategy),
    ("anti", AntiStrategy),
    ("orb", OpeningRangeBreakoutStrategy),
    ("donchian", DonchianBreakoutStrategy),
    ("bb_mr", BollingerMeanReversionStrategy),
    ("turtle_soup", TurtleSoupStrategy),
    ("supertrend", SupertrendStrategy),
    ("turtles_system2", TurtlesSystem2Strategy),
]


@pytest.fixture
def loaded_registry() -> StrategyRegistry:
    reg = StrategyRegistry(db=None)
    for name, cls in ALL_SEEDS:
        reg.upsert(name=name, version="1", state="SHADOW", tier=2)
        reg.attach_instance(name, cls(contract=MES))
    return reg


def test_anti_is_trending_not_ranging(loaded_registry):
    """Anti is Raschke pullback-in-trend; needs trending regime."""
    fit = AntiStrategy.metadata.regime_fit
    assert fit["trending"] >= ELIGIBILITY_THRESHOLD, "anti must qualify for TRENDING"
    assert fit["ranging"] < ELIGIBILITY_THRESHOLD, "anti must NOT qualify for RANGING"


def test_eligible_in_trending(loaded_registry):
    eligible = eligible_strategies(loaded_registry, "trending", confidence=0.8)
    assert "anti" in eligible
    assert "ema_cross" in eligible
    assert "donchian" in eligible
    assert "supertrend" in eligible
    assert "turtles_system2" in eligible
    # mean-reversion should NOT be in trending
    assert "bb_mr" not in eligible
    assert "turtle_soup" not in eligible


def test_eligible_in_ranging(loaded_registry):
    eligible = eligible_strategies(loaded_registry, "ranging", confidence=0.8)
    assert "bb_mr" in eligible
    assert "turtle_soup" in eligible
    # trend strategies should NOT be in ranging
    assert "anti" not in eligible
    assert "ema_cross" not in eligible
    assert "donchian" not in eligible


def test_chaotic_silences_all(loaded_registry):
    assert eligible_strategies(loaded_registry, "chaotic", confidence=1.0) == []


def test_compressing_silences_all(loaded_registry):
    assert eligible_strategies(loaded_registry, "compressing", confidence=1.0) == []


def test_ambiguous_returns_empty(loaded_registry):
    assert eligible_strategies(loaded_registry, "ambiguous", confidence=1.0) == []


def test_low_confidence_returns_empty(loaded_registry):
    assert eligible_strategies(loaded_registry, "trending", confidence=0.3) == []


def test_habitat_match_helper():
    assert habitat_match(AntiStrategy.metadata, "trending") is True
    assert habitat_match(AntiStrategy.metadata, "ranging") is False
    assert habitat_match(BollingerMeanReversionStrategy.metadata, "ranging") is True
    assert habitat_match(BollingerMeanReversionStrategy.metadata, "trending") is False
    # Unknown regime label → no match
    assert habitat_match(AntiStrategy.metadata, "chaotic") is False


def test_regime_to_fit_key_only_maps_actionable_regimes():
    """trending and ranging are mapped; the other three (compressing/chaotic/
    ambiguous) intentionally are not — they should silence the fleet."""
    assert "trending" in REGIME_TO_FIT_KEY
    assert "ranging" in REGIME_TO_FIT_KEY
    assert "chaotic" not in REGIME_TO_FIT_KEY
    assert "compressing" not in REGIME_TO_FIT_KEY
    assert "ambiguous" not in REGIME_TO_FIT_KEY
