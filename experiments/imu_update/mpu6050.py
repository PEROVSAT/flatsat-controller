"""MPU6050 sample conversion and transaction evidence helpers for IMU update."""

from __future__ import annotations

import math
import struct
import time
from dataclasses import dataclass

from hitl_bridge.codec import (
    PSP_COMMAND_GET_STATUS,
    PSP_FRAME_LOG,
    PSP_LOG_I2C_TRANSACTION,
    encode_get_status,
    peek_i2c_transaction,
    peek_prefix,
    peek_result,
    peek_status,
)
from flatsat_controller.types import FlatsatError


@dataclass(frozen=True)
class Sample:
    sequence: int
    accel: tuple[float, float, float]
    gyro: tuple[float, float, float]
    temperature_c: float


def parse_sample(raw: object, name: str = "sample") -> Sample:
    required = {"sequence", "accel_m_s2", "gyro_rad_s", "temperature_c"}
    if not isinstance(raw, dict) or set(raw) != required:
        raise ValueError(f"{name} requires sequence, accel_m_s2, gyro_rad_s, temperature_c")
    seq = raw["sequence"]
    if isinstance(seq, bool) or not isinstance(seq, int) or not 0 <= seq <= 0xffffffff:
        raise ValueError(f"{name}.sequence must be u32")
    accel, gyro, temp = raw["accel_m_s2"], raw["gyro_rad_s"], raw["temperature_c"]
    if not isinstance(accel, list) or len(accel) != 3 or not isinstance(gyro, list) or len(gyro) != 3:
        raise ValueError(f"{name} accel_m_s2 and gyro_rad_s must each have three values")
    values = (*accel, *gyro, temp)
    if any(not finite_number(v) for v in values):
        raise ValueError(f"{name} values must be finite numbers")
    if any(abs(v) > 3.4028235e38 for v in values):
        raise ValueError(f"{name} values must fit in the wire-format float32 fields")
    return Sample(seq, tuple(map(float, accel)), tuple(map(float, gyro)), float(temp))


def finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def round_away(value: float) -> int:
    if math.isinf(value):
        return 32768 if value > 0 else -32769
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


def expected_burst(sample: Sample, accel_range_g: int, gyro_range_dps: int) -> bytes:
    accel_sensitivity = {2: 16384, 4: 8192, 8: 4096, 16: 2048}[accel_range_g]
    gyro_sensitivity = {250: 131.0, 500: 65.5, 1000: 32.8, 2000: 16.4}[gyro_range_dps]
    accel = [_signed16(round_away(_f32(v) * accel_sensitivity / 9.80665)) for v in sample.accel]
    gyro = [_signed16(round_away(math.degrees(_f32(v)) * gyro_sensitivity)) for v in sample.gyro]
    temp = _signed16(round_away((_f32(sample.temperature_c) - 36.53) * 340.0))
    return struct.pack(">7h", *(accel + [temp] + gyro))


def _f32(value: float) -> float:
    try:
        return struct.unpack("<f", struct.pack("<f", value))[0]
    except OverflowError:
        return math.copysign(float("inf"), value)


def _signed16(value: int) -> int:
    return max(-32768, min(32767, value))


def counter_changed(initial: int, current: int) -> bool:
    return current != initial


def consume_transactions(session, cursor, config, reasons):
    transactions = []
    frames = session.received
    for frame in frames[cursor:]:
        prefix = peek_prefix(frame)
        if (prefix is None or prefix.source != config.device_address
                or prefix.destination not in (0, config.controller_address)
                or prefix.kind != PSP_FRAME_LOG):
            continue
        if len(frame) > 20 and frame[20] == PSP_LOG_I2C_TRANSACTION:
            transaction = peek_i2c_transaction(frame)
            if transaction is None:
                reasons.append("malformed or truncated I2C transaction frame")
                continue
            transactions.append(transaction)
    return len(frames), transactions


def merge_transactions(observed, duplicates, records):
    for record in records:
        transaction_id = record.transaction_id
        if transaction_id in observed:
            duplicates.add(transaction_id)
        else:
            observed[transaction_id] = record


def get_status(session, request_id: int, timeout: float, config):
    session.send_frame(encode_get_status(
        config.device_address, time.time_ns(), request_id, source=config.controller_address
    ))
    deadline = time.monotonic() + timeout
    result_ok = False
    status_seen = None
    while time.monotonic() < deadline:
        frame = session.receive(max(0.001, deadline - time.monotonic()))
        prefix = peek_prefix(frame)
        if (prefix is None or prefix.source != config.device_address
                or prefix.destination not in (0, config.controller_address)
                or prefix.kind != PSP_FRAME_LOG):
            continue
        result = peek_result(frame)
        if result and result.request_id == request_id:
            if result.command_kind != PSP_COMMAND_GET_STATUS or result.status != 1:
                raise FlatsatError(f"GetStatus rejected with status {result.status}")
            result_ok = True
        status = peek_status(frame)
        if status is not None:
            status_seen = status
        if status_seen is not None and result_ok:
            return status_seen
    raise TimeoutError("Timed out waiting for GetStatus result and status log")
