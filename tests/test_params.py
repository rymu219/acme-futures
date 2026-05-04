"""Cross-strategy validation that every strategy publishes a parameter manifest
and that defaults match the strategy's Config dataclass."""

from dataclasses import asdict

import pytest

from acme.strategies.anti import AntiStrategy
from acme.strategies.bb_mr import BollingerMeanReversionStrategy
from acme.strategies.donchian import DonchianBreakoutStrategy
from acme.strategies.ema_cross import EmaCrossStrategy
from acme.strategies.orb import OpeningRangeBreakoutStrategy
from acme.strategies.params import ParameterSpec
from acme.strategies.supertrend import SupertrendStrategy
from acme.strategies.turtle_soup import TurtleSoupStrategy
from acme.strategies.turtles_system2 import TurtlesSystem2Strategy

ALL_STRATEGIES = [
    EmaCrossStrategy, AntiStrategy, OpeningRangeBreakoutStrategy,
    BollingerMeanReversionStrategy, DonchianBreakoutStrategy,
    TurtleSoupStrategy, SupertrendStrategy, TurtlesSystem2Strategy,
]


@pytest.mark.parametrize("cls", ALL_STRATEGIES)
def test_tunable_params_returns_specs(cls):
    params = cls.tunable_params()
    assert len(params) >= 1
    for p in params:
        assert isinstance(p, ParameterSpec)
        # Defaults must obey min/max
        assert p.min_value <= p.default <= p.max_value
        assert p.step > 0


@pytest.mark.parametrize("cls", ALL_STRATEGIES)
def test_param_defaults_match_config(cls):
    """Every name in tunable_params() must exist on the Config dataclass with
    the same default value. Catches drift between manifest and reality."""
    inst = cls()
    config_defaults = asdict(inst.config)
    for p in cls.tunable_params():
        assert p.name in config_defaults, (
            f"{cls.__name__}: tunable param '{p.name}' not in Config dataclass"
        )
        # Allow int↔float equivalence (e.g., 25 vs 25.0)
        assert float(config_defaults[p.name]) == float(p.default), (
            f"{cls.__name__}.{p.name}: Config default {config_defaults[p.name]} "
            f"!= manifest default {p.default}"
        )


def test_invalid_paramspec_default_outside_bounds():
    with pytest.raises(ValueError):
        ParameterSpec("x", int, default=10, min_value=20, max_value=30, step=1)


def test_invalid_paramspec_step_zero():
    with pytest.raises(ValueError):
        ParameterSpec("x", int, default=5, min_value=0, max_value=10, step=0)
