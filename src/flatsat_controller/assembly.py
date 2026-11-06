"""The single composition point for the installed bench."""

from collections.abc import Callable

from flatsat_controller.config import BenchConfig
from flatsat_controller.runtime.context import RunContext
from flatsat_controller.bench import Bench
from flatsat_controller.firmware import Firmware, Target
from flatsat_controller.firmware.types import PICO2_BOARD
from flatsat_controller.runtime import Operator
from flatsat_controller.bridge import Bridge


def assemble_bench(
    runtime: RunContext,
    config: BenchConfig,
    *,
    auto_confirm: bool = False,
    input_fn: Callable[[str], str] | None = None,
) -> Bench:
    runtime.write_metadata("installation", {
        "workspace": str(config.workspace),
        "devices": config.devices,
        "tools": config.tools,
    })
    return Bench(
        operator=Operator(runtime, auto_confirm=auto_confirm, input_fn=input_fn),
        targets={name: Target(name, PICO2_BOARD, device.probe_serial)
                 for name, device in config.devices.items() if device.probe_serial},
        firmware=Firmware(config.tools, runtime.processes, runtime),
        bridge=Bridge(config.bridge_config, runtime),
    )
