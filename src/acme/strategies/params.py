"""Parameter manifest — each strategy declares its tunable knobs in a uniform
shape so the Inspector can show them at-a-glance and the future sweep tab
can read them from a single source of truth.

Convention (no enforcement): each strategy class adds a classmethod

    @classmethod
    def tunable_params(cls) -> list[ParameterSpec]: ...

returning the parameters that can be swept. Defaults must match the
strategy's Config dataclass defaults (validated by tests/test_params.py).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParameterSpec:
    name: str
    type: type
    default: int | float
    min_value: int | float
    max_value: int | float
    step: int | float
    description: str = ""

    def __post_init__(self) -> None:
        if self.min_value > self.default:
            raise ValueError(f"{self.name}: min_value {self.min_value} > default {self.default}")
        if self.default > self.max_value:
            raise ValueError(f"{self.name}: default {self.default} > max_value {self.max_value}")
        if self.step <= 0:
            raise ValueError(f"{self.name}: step must be > 0")
