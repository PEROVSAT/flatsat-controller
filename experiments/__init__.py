"""Registered, code-owned FlatSat experiments."""

from dataclasses import dataclass
from collections.abc import Callable
from typing import Generic, TypeVar

from flatsat_controller.bench import Bench
from flatsat_controller.types import Verdict
from flatsat_controller.config import BenchConfig
from .utc_sync.experiment import run as bridge_utc, parameters as bridge_utc_parameters
from .imu_update.experiment import run as imu_update, parameters as imu_update_parameters

ParametersT = TypeVar("ParametersT")


@dataclass(frozen=True)
class Experiment(Generic[ParametersT]):
    run: Callable[[Bench, ParametersT], Verdict | None]
    parameters: Callable[[BenchConfig], ParametersT]
    directory: str


EXPERIMENTS = {
    "utc-sync": Experiment(bridge_utc, bridge_utc_parameters, "utc_sync"),
    "imu-update": Experiment(imu_update, imu_update_parameters, "imu_update"),
}
