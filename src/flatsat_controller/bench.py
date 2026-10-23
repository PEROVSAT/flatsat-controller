from dataclasses import dataclass, field
from collections.abc import Mapping
from flatsat_controller.firmware import Firmware, Target
from flatsat_controller.runtime import Operator


@dataclass(frozen=True)
class Bench:
    operator: Operator
    targets: Mapping[str, Target] = field(default_factory=dict)
    firmware: Firmware | None = None
