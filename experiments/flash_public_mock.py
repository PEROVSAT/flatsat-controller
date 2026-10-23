from __future__ import annotations
from flatsat_controller.bench import Bench
from dataclasses import dataclass
from flatsat_controller.config import ControllerConfig
from flatsat_controller.types import FlatsatError
from flatsat_controller.firmware import BuildRequest, load_dbuild
from flatsat_controller.firmware.presets import public_mock_selections


@dataclass(frozen=True)
class PublicMockParameters:
    build: BuildRequest
    target: str = "obc"
    flash_mode: str = "pico2"
    console_mode: str = "logs"


def parameters(config: ControllerConfig) -> PublicMockParameters:
    raw = dict(config.experiment)
    if set(raw) - {"build", "target", "flash_mode", "console_mode"}:
        raise FlatsatError("Unknown public-mock experiment parameters")
    if any(not isinstance(value, str) or not value for value in raw.values()):
        raise FlatsatError("Public-mock parameters must be nonempty strings")
    build_name = raw.pop("build", "default")
    if build_name not in config.builds:
        raise FlatsatError(f"Unknown build {build_name!r}")
    result = PublicMockParameters(config.builds[build_name], **raw)
    if result.target not in config.installation.targets:
        raise FlatsatError(f"Unknown experiment target {result.target!r}")
    return result


def run(bench: Bench, parameters: PublicMockParameters) -> None:
    catalog = load_dbuild(parameters.build.app_root)["devices"]
    selections = public_mock_selections(
        catalog,
        roles={"FLASH": parameters.flash_mode, "CONSOLE": parameters.console_mode},
    )
    request = BuildRequest(
        parameters.build.app_root, parameters.build.board, selections
    )
    if bench.firmware is None:
        raise RuntimeError("This experiment requires firmware capability")
    artifact = bench.firmware.build(request)
    bench.firmware.flash(bench.targets[parameters.target], artifact)
