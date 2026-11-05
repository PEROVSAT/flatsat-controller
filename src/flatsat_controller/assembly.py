"""The single composition point for the installed bench."""

from collections.abc import Callable

from flatsat_controller.config import ControllerConfig
from flatsat_controller.runtime.context import RunContext
from flatsat_controller.bench import Bench
from flatsat_controller.firmware import Firmware
from flatsat_controller.runtime import Operator
from flatsat_controller.simulation import SimulationLink


def assemble_bench(
    runtime: RunContext,
    config: ControllerConfig,
    *,
    auto_confirm: bool = False,
    input_fn: Callable[[str], str] | None = None,
) -> Bench:
    runtime.write_metadata("installation", config.installation)
    if config.simulation_link is not None:
        runtime.write_metadata("simulation_link", config.simulation_link)
    return Bench(
        operator=Operator(runtime, auto_confirm=auto_confirm, input_fn=input_fn),
        targets=config.installation.targets,
        firmware=Firmware(config.installation.tools, runtime.processes, runtime)
        if config.installation.tools
        else None,
        simulation_link=(
            SimulationLink(config.simulation_link, runtime)
            if config.simulation_link
            else None
        ),
    )
