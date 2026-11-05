from __future__ import annotations

import math
import struct
import time
from dataclasses import dataclass

from hitl_bridge.codec import (
    PSP_COMMAND_GET_STATUS,
    PSP_FRAME_LOG,
    PSP_LOG_I2C_TRANSACTION,
    I2cTransactionBody,
    encode_get_status,
    encode_imu_stimulus,
    ImuStimulusBody,
    peek_i2c_transaction,
    peek_prefix,
    peek_result,
    peek_status,
)

from flatsat_controller.bench import Bench
from flatsat_controller.config import ControllerConfig
from flatsat_controller.types import FlatsatError, Verdict


@dataclass(frozen=True)
class Sample:
    sequence: int
    accel: tuple[float, float, float]
    gyro: tuple[float, float, float]
    temperature_c: float


@dataclass(frozen=True)
class Mpu6050ABParameters:
    sample_a: Sample
    sample_b: Sample
    accel_range_g: int
    gyro_range_dps: int
    timeout_s: float


def parameters(config: ControllerConfig) -> Mpu6050ABParameters:
    raw = dict(config.experiment)
    allowed = {"sample_a", "sample_b", "accel_range_g", "gyro_range_dps", "timeout_s"}
    if set(raw) - allowed:
        raise FlatsatError(f"Unknown mpu6050-ab parameters: {sorted(set(raw) - allowed)}")
    try:
        a = _sample(raw["sample_a"], "sample_a")
        b = _sample(raw["sample_b"], "sample_b")
        accel_range = raw.get("accel_range_g", 2)
        gyro_range = raw.get("gyro_range_dps", 250)
        timeout = raw.get("timeout_s", 10.0)
        if (
            isinstance(accel_range, bool)
            or not isinstance(accel_range, int)
            or accel_range not in (2, 4, 8, 16)
        ):
            raise ValueError("accel_range_g must be 2, 4, 8, or 16")
        if (
            isinstance(gyro_range, bool)
            or not isinstance(gyro_range, int)
            or gyro_range not in (250, 500, 1000, 2000)
        ):
            raise ValueError("gyro_range_dps must be 250, 500, 1000, or 2000")
        if not _finite_number(timeout) or timeout <= 0:
            raise ValueError("timeout_s must be positive and finite")
        if a.sequence == b.sequence:
            raise ValueError("sample sequences must differ")
        if 0xffffffff in (a.sequence, b.sequence):
            raise ValueError("sample sequence 0xffffffff is reserved")
        return Mpu6050ABParameters(a, b, accel_range, gyro_range, float(timeout))
    except (KeyError, TypeError, ValueError) as exc:
        raise FlatsatError(f"Invalid mpu6050-ab parameters: {exc}") from exc


def run(bench: Bench, params: Mpu6050ABParameters) -> Verdict:
    link = bench.simulation_link
    if link is None:
        raise FlatsatError("mpu6050-ab requires simulation_link configuration")
    if bench.targets and "obc" not in bench.targets:
        raise FlatsatError("mpu6050-ab expects the OBC target named 'obc'")

    reasons: list[str] = []
    observed_transactions: dict[int, I2cTransactionBody] = {}
    duplicate_transaction_ids: set[int] = set()
    frame_cursor = 0
    request_id = 1
    try:
        with link.connect() as session:
            initial_status = _get_status(session, request_id, params.timeout_s, link.config)
            baseline_count = initial_status.transactions
            request_id += 1
            for sample in (params.sample_a, params.sample_b):
                session.send_frame(
                    encode_imu_stimulus(
                        link.config.device_address,
                        time.time_ns(),
                        ImuStimulusBody(
                            sample.sequence,
                            *sample.accel,
                            *sample.gyro,
                            sample.temperature_c,
                        ),
                        source=link.config.controller_address,
                    )
                )
                request_id += 1
                request_id = _await_active(
                    session, sample.sequence, request_id, params.timeout_s, link.config
                )
                frame_cursor, records = _consume_transactions(
                    session, frame_cursor, link.config, reasons
                )
                _merge_transactions(
                    observed_transactions, duplicate_transaction_ids, records
                )
                seen = _sample_read_observed(
                    observed_transactions, baseline_count, sample.sequence
                )
                deadline = time.monotonic() + params.timeout_s
                while time.monotonic() < deadline and not seen:
                    session.receive(max(0.001, deadline - time.monotonic()))
                    frame_cursor, records = _consume_transactions(
                        session, frame_cursor, link.config, reasons
                    )
                    _merge_transactions(
                        observed_transactions, duplicate_transaction_ids, records
                    )
                    seen = _sample_read_observed(
                        observed_transactions, baseline_count, sample.sequence
                    )
                if not seen:
                    reasons.append(
                        f"timed out waiting for an I2C read of sample {sample.sequence}"
                    )
            status = _get_status(session, request_id, params.timeout_s, link.config)
            frame_cursor, records = _consume_transactions(
                session, frame_cursor, link.config, reasons
            )
            _merge_transactions(
                observed_transactions, duplicate_transaction_ids, records
            )
            expected_count = (status.transactions - baseline_count) & 0xffffffff
            if expected_count > 10_000:
                reasons.append(
                    "device transaction counter advanced beyond the bounded A/B window"
                )
                expected_count = 0
            expected_ids = {
                (baseline_count + offset) & 0xffffffff
                for offset in range(1, expected_count + 1)
            }
            drain_deadline = time.monotonic() + params.timeout_s
            while (
                expected_ids - observed_transactions.keys()
                and time.monotonic() < drain_deadline
            ):
                try:
                    session.receive(max(0.001, drain_deadline - time.monotonic()))
                except TimeoutError:
                    frame_cursor, records = _consume_transactions(
                        session, frame_cursor, link.config, reasons
                    )
                    _merge_transactions(
                        observed_transactions, duplicate_transaction_ids, records
                    )
                    break
                frame_cursor, records = _consume_transactions(
                    session, frame_cursor, link.config, reasons
                )
                for record in records:
                    if record.transaction_id in observed_transactions:
                        duplicate_transaction_ids.add(record.transaction_id)
                    else:
                        observed_transactions[record.transaction_id] = record
            missing_ids = expected_ids - observed_transactions.keys()
            if missing_ids:
                reasons.append(
                    "I2C transaction ID gap or tail loss: capture incomplete through "
                    "final status snapshot; missing "
                    f"transaction IDs {sorted(missing_ids)}"
                )
            for transaction_id in sorted(
                expected_ids & duplicate_transaction_ids
            ):
                reasons.append(f"duplicate I2C transaction ID {transaction_id}")
            for transaction_id in expected_ids & observed_transactions.keys():
                transaction = observed_transactions[transaction_id]
                if transaction.flags:
                    reasons.append(
                        f"transaction {transaction_id} has flags 0x{transaction.flags:04x}"
                    )
                if (
                    transaction.write_count > transaction.write_length
                    or transaction.read_count > transaction.read_length
                ):
                    reasons.append(
                        f"transaction {transaction_id} capture is truncated"
                    )
            for sample in (params.sample_a, params.sample_b):
                matching = [
                    observed_transactions[transaction_id]
                    for transaction_id in expected_ids & observed_transactions.keys()
                    if observed_transactions[transaction_id].sample_sequence
                    == sample.sequence
                ]
                if not any(
                    transaction.read_register == 0x3b
                    and transaction.read_count == 14
                    and len(transaction.read_data) == 14
                    for transaction in matching
                ):
                    reasons.append(
                        f"no complete 14-byte read of sample {sample.sequence} "
                        "appeared through the final status snapshot"
                    )
                expected_burst = _expected_burst(sample, params)
                for transaction in matching:
                    if (
                        transaction.read_register == 0x3b
                        and transaction.read_count == 14
                        and len(transaction.read_data) == 14
                        and transaction.read_data != expected_burst
                    ):
                        reasons.append(
                            f"sample {sample.sequence} burst mismatch: got "
                            f"{transaction.read_data.hex()}, expected {expected_burst.hex()}"
                        )
            if _counter_changed(initial_status.dropped_transactions, status.dropped_transactions):
                reasons.append("device dropped-transaction counter increased or reset during A/B")
            if _counter_changed(initial_status.rx_errors, status.rx_errors):
                reasons.append("device receive-error counter increased or reset during A/B")
            if _counter_changed(initial_status.rejected_samples, status.rejected_samples):
                reasons.append("device rejected samples during A/B")
            if session.pending_envelope_bytes:
                reasons.append("incomplete or unframed bytes remained on the simulation link")
    except TimeoutError as exc:
        reasons.append(str(exc))
    if reasons:
        return Verdict("invalid", tuple(dict.fromkeys(reasons)))
    return Verdict("pass", ("A/B sensor-bus readback matched the configured ranges and samples; this is not a flight telemetry verdict.",))


def _sample(raw: object, name: str) -> Sample:
    if not isinstance(raw, dict) or set(raw) - {"sequence", "accel_m_s2", "gyro_rad_s", "temperature_c"}:
        raise ValueError(f"{name} requires sequence, accel_m_s2, gyro_rad_s, temperature_c")
    seq = raw["sequence"]
    if isinstance(seq, bool) or not isinstance(seq, int) or not 0 <= seq <= 0xffffffff:
        raise ValueError(f"{name}.sequence must be u32")
    accel, gyro, temp = raw["accel_m_s2"], raw["gyro_rad_s"], raw["temperature_c"]
    if not isinstance(accel, list) or len(accel) != 3 or not isinstance(gyro, list) or len(gyro) != 3:
        raise ValueError(f"{name} accel_m_s2 and gyro_rad_s must each have three values")
    values = (*accel, *gyro, temp)
    if any(not _finite_number(v) for v in values):
        raise ValueError(f"{name} values must be finite numbers")
    if any(abs(v) > 3.4028235e38 for v in values):
        raise ValueError(f"{name} values must fit in the wire-format float32 fields")
    return Sample(seq, tuple(map(float, accel)), tuple(map(float, gyro)), float(temp))


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _round_away(value: float) -> int:
    if math.isinf(value):
        return 32768 if value > 0 else -32769
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


def _expected_burst(sample: Sample, params: Mpu6050ABParameters) -> bytes:
    accel_sensitivity = {2: 16384, 4: 8192, 8: 4096, 16: 2048}[params.accel_range_g]
    gyro_sensitivity = {250: 131.0, 500: 65.5, 1000: 32.8, 2000: 16.4}[params.gyro_range_dps]
    accel = [_signed16(_round_away(_f32(v) * accel_sensitivity / 9.80665)) for v in sample.accel]
    gyro = [_signed16(_round_away(math.degrees(_f32(v)) * gyro_sensitivity)) for v in sample.gyro]
    temp = _signed16(_round_away((_f32(sample.temperature_c) - 36.53) * 340.0))
    return struct.pack(">7h", *(accel + [temp] + gyro))


def _f32(value: float) -> float:
    try:
        return struct.unpack("<f", struct.pack("<f", value))[0]
    except OverflowError:
        return math.copysign(float("inf"), value)


def _signed16(value: int) -> int:
    return max(-32768, min(32767, value))


def _counter_changed(initial: int, current: int) -> bool:
    return current != initial


def _consume_transactions(session, cursor, config, reasons):
    transactions = []
    frames = session.received
    for frame in frames[cursor:]:
        prefix = peek_prefix(frame)
        if (
            prefix is None
            or prefix.source != config.device_address
            or prefix.destination not in (0, config.controller_address)
            or prefix.kind != PSP_FRAME_LOG
        ):
            continue
        transaction = None
        if len(frame) > 20 and frame[20] == PSP_LOG_I2C_TRANSACTION:
            transaction = peek_i2c_transaction(frame)
            if transaction is None:
                reasons.append("malformed or truncated I2C transaction frame")
                continue
        if transaction is None:
            continue
        transactions.append(transaction)
    return len(frames), transactions


def _merge_transactions(observed, duplicates, records):
    for record in records:
        transaction_id = record.transaction_id
        if transaction_id in observed:
            duplicates.add(transaction_id)
        else:
            observed[transaction_id] = record


def _sample_read_observed(transactions, baseline_count, sequence):
    for transaction_id, transaction in transactions.items():
        distance = (transaction_id - baseline_count) & 0xffffffff
        if (
            0 < distance < 0x80000000
            and transaction.sample_sequence == sequence
            and transaction.read_register == 0x3b
        ):
            return True
    return False


def _await_active(session, sequence: int, request_id: int, timeout: float, config):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = _get_status(session, request_id, max(0.001, deadline - time.monotonic()), config)
        if status.active_sample_sequence == sequence:
            return request_id + 1
        request_id += 1
    raise TimeoutError(f"Timed out waiting for device to adopt sample {sequence}")


def _get_status(session, request_id: int, timeout: float, config):
    session.send_frame(encode_get_status(config.device_address, time.time_ns(), request_id, source=config.controller_address))
    deadline = time.monotonic() + timeout
    result_ok = False
    status_seen = None
    while time.monotonic() < deadline:
        frame = session.receive(max(0.001, deadline - time.monotonic()))
        prefix = peek_prefix(frame)
        if (
            prefix is None
            or prefix.source != config.device_address
            or prefix.destination not in (0, config.controller_address)
            or prefix.kind != PSP_FRAME_LOG
        ):
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
