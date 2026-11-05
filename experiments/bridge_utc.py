"""Build, flash, and verify UTC synchronization for the named imu0 board."""

from __future__ import annotations

import math
import threading
import sys
from dataclasses import dataclass

from flatsat_controller.bench import Bench
from flatsat_controller.config import ControllerConfig
from flatsat_controller.firmware import BuildRequest, Target
from flatsat_controller.firmware.types import PICO2_BOARD
from flatsat_controller.types import FlatsatError, Verdict


@dataclass(frozen=True)
class BridgeUtcParameters:
    target: Target
    build: BuildRequest
    timeout_s: float
    tolerance_ns: int


def parameters(config: ControllerConfig) -> BridgeUtcParameters:
    """Resolve and validate all bench identities before any hardware operation."""
    bridge = config.bridge_config
    if bridge is None:
        raise FlatsatError("bridge-utc requires top-level bridge_config")
    try:
        identity = bridge.resolve_target("imu0")
    except ValueError as exc:
        raise FlatsatError(str(exc)) from exc
    if identity.board != PICO2_BOARD:
        raise FlatsatError(f"imu0 board mismatch: expected {PICO2_BOARD}, got {identity.board}")
    if not identity.probe_serial:
        raise FlatsatError("imu0 requires an explicit SWD probe_serial")
    if identity.simulation is None or not any(d.name == "imu0" for d in bridge.devices):
        raise FlatsatError("imu0 requires its configured simulation UART link")
    raw = dict(config.experiment)
    allowed = {"timeout_s", "tolerance_ms"}
    if set(raw) - allowed:
        raise FlatsatError(f"Unknown bridge-utc parameters: {sorted(set(raw) - allowed)}")
    timeout = raw.get("timeout_s", 20.0)
    tolerance = raw.get("tolerance_ms", 10.0)
    if not _positive_finite(timeout) or timeout > threading.TIMEOUT_MAX:
        raise FlatsatError("Invalid bridge-utc parameters: timeout_s must be positive and finite")
    if not _positive_finite(tolerance):
        raise FlatsatError("Invalid bridge-utc parameters: tolerance_ms must be positive and finite")
    try:
        tolerance_ns = round(float(tolerance) * 1_000_000)
    except (OverflowError, ValueError):
        tolerance_ns = 0
    if tolerance_ns <= 0 or tolerance_ns > sys.maxsize:
        raise FlatsatError("Invalid bridge-utc parameters: tolerance_ms is outside the supported nanosecond range")
    build = config.builds.get("emulator")
    if build is None:
        raise FlatsatError("bridge-utc requires an explicit builds.emulator firmware build")
    if build.kind != "build":
        raise FlatsatError("bridge-utc requires builds.emulator.kind: build")
    if build.board != identity.board:
        raise FlatsatError(f"imu0 build board mismatch: {build.board} != {identity.board}")
    return BridgeUtcParameters(
        Target("imu0", identity.board, identity.probe_serial), build,
        float(timeout), tolerance_ns,
    )


def run(bench: Bench, params: BridgeUtcParameters) -> Verdict:
    if bench.firmware is None or bench.bridge is None:
        raise FlatsatError("bridge-utc requires firmware and bridge_config")
    # Firmware's service validates west/OpenOCD/build compatibility before build.
    bench.firmware.build_and_flash(params.target, params.build)
    result = bench.bridge.verify_utc(
        "imu0", timeout_s=params.timeout_s, tolerance_ns=params.tolerance_ns,
        require_correction=True,
    )
    if result.passed:
        return Verdict("pass", (result.reason,))
    return Verdict("fail", (result.reason,))


def _positive_finite(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False
