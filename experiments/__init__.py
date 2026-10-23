"""Explicit experiment registration and per-experiment parameter parsing."""

from dataclasses import dataclass
from collections.abc import Callable
from typing import Generic, TypeVar

from flatsat_controller.bench import Bench
from flatsat_controller.types import Verdict
from flatsat_controller.config import ControllerConfig
from .flash_public_mock import (
    run as flash_public_mock,
    parameters as public_mock_parameters,
)


ParametersT = TypeVar("ParametersT")


@dataclass(frozen=True)
class Experiment(Generic[ParametersT]):
    run: Callable[[Bench, ParametersT], Verdict | None]
    parameters: Callable[[ControllerConfig], ParametersT]


EXPERIMENTS = {
    "flash-public-mock": Experiment(flash_public_mock, public_mock_parameters),
}
