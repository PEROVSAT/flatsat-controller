"""Build, flash, and verify UTC synchronization for the logical imu device."""

from __future__ import annotations

import math
import threading
import sys
import os
from dataclasses import dataclass

from flatsat_controller.bench import Bench
from flatsat_controller.config import BenchConfig
from flatsat_controller.firmware import BuildRequest, Target
from flatsat_controller.firmware.types import PICO2_BOARD
from flatsat_controller.types import FlatsatError, Verdict
from . import settings


@dataclass(frozen=True)
class BridgeUtcParameters:
    target: Target
    build: BuildRequest
    timeout_s: float
    tolerance_ns: int


def parameters(config: BenchConfig) -> BridgeUtcParameters:
    """Resolve and validate all bench identities before any hardware operation."""
    target = config.require_target("imu")
    uart = config.require_uart("imu")
    if uart.address != 1 or uart.baud != 115200:
        raise FlatsatError("utc-sync flashes the stock MPU6050 firmware and requires imu address 1 at 115200 baud")
    timeout = settings.TIMEOUT_S
    tolerance = settings.TOLERANCE_MS
    if not _positive_finite(timeout) or timeout > threading.TIMEOUT_MAX:
        raise FlatsatError("Invalid utc-sync parameters: timeout_s must be positive and finite")
    if not _positive_finite(tolerance):
        raise FlatsatError("Invalid utc-sync parameters: tolerance_ms must be positive and finite")
    try:
        tolerance_ns = round(float(tolerance) * 1_000_000)
    except (OverflowError, ValueError):
        tolerance_ns = 0
    if tolerance_ns <= 0 or tolerance_ns > sys.maxsize:
        raise FlatsatError("Invalid utc-sync parameters: tolerance_ms is outside the supported nanosecond range")
    if not config.emulator_app.is_dir():
        raise FlatsatError(f"Emulator app is missing: {config.emulator_app}")
    if config.tools.west is None or not config.tools.west.is_file() or not os.access(config.tools.west, os.X_OK):
        raise FlatsatError(f"utc-sync requires the workspace west executable: {config.tools.west}")
    if not config.tools.openocd.is_file() or not os.access(config.tools.openocd, os.X_OK):
        raise FlatsatError(f"utc-sync requires OpenOCD: {config.tools.openocd}")
    build = BuildRequest(config.emulator_app, PICO2_BOARD, {}, "build", config.tools.west)
    return BridgeUtcParameters(
        target, build,
        float(timeout), tolerance_ns,
    )


def run(bench: Bench, params: BridgeUtcParameters) -> Verdict:
    if bench.firmware is None or bench.bridge is None:
        raise FlatsatError("utc-sync requires firmware and bridge")
    # Firmware's service validates west/OpenOCD/build compatibility before build.
    bench.firmware.build_and_flash(params.target, params.build)
    result = bench.bridge.verify_utc(
        "imu", timeout_s=params.timeout_s, tolerance_ns=params.tolerance_ns,
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
