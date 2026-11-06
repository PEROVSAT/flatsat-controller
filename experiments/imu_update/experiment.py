"""Update the MPU6050 emulator and OBC, then prove one sensor-bus readback."""

from __future__ import annotations

import threading
import time
import os
from dataclasses import dataclass

from hitl_bridge.codec import ImuStimulusBody, encode_imu_stimulus

from flatsat_controller.bench import Bench
from flatsat_controller.config import BenchConfig
from flatsat_controller.firmware import BuildRequest, Target
from flatsat_controller.firmware.dbuild import load_dbuild
from flatsat_controller.firmware.service import _resolved_selections
from flatsat_controller.firmware.types import PICO2_BOARD
from flatsat_controller.types import FlatsatError, Verdict
from .mpu6050 import (
    Sample,
    expected_burst as _expected_burst_for,
    finite_number as _finite_number,
    parse_sample as _parse_sample,
    consume_transactions as _consume_transactions,
    merge_transactions as _merge_transactions,
    get_status as _get_status,
    counter_changed as _counter_changed,
)
from . import settings


@dataclass(frozen=True)
class ImuUpdateParameters:
    imu_target: Target
    obc_target: Target
    emulator_build: BuildRequest
    obc_build: BuildRequest
    sample: Sample
    accel_range_g: int
    gyro_range_dps: int
    boot_timeout_s: float
    read_timeout_s: float
    utc_timeout_s: float
    tolerance_ns: int


def parameters(config: BenchConfig) -> ImuUpdateParameters:
    imu_target = config.require_target("imu")
    obc_target = config.require_target("obc")
    uart = config.require_uart("imu")
    if uart.address != 1 or uart.baud != 115200:
        raise FlatsatError("imu-update flashes the stock MPU6050 firmware and requires imu address 1 at 115200 baud")

    emulator = BuildRequest(config.emulator_app, PICO2_BOARD, {}, "build", config.tools.west)
    if not emulator.app_root.is_dir():
        raise FlatsatError(f"Emulator app is missing: {emulator.app_root}")
    if config.tools.west is None or not config.tools.west.is_file() or not os.access(config.tools.west, os.X_OK):
        raise FlatsatError(f"imu-update requires the workspace west executable: {config.tools.west}")
    if not config.tools.openocd.is_file() or not os.access(config.tools.openocd, os.X_OK):
        raise FlatsatError(f"imu-update requires the OpenOCD executable: {config.tools.openocd}")
    flight_app = config.require_flight_app()
    flight_west = config.flight_west
    if flight_west is None or not flight_west.is_file() or not os.access(flight_west, os.X_OK):
        raise FlatsatError(f"imu-update requires the flight west executable: {flight_west}")
    source = load_dbuild(flight_app)
    resolved = _resolved_selections(source, settings.FLIGHT_SELECTIONS)
    obc_build = BuildRequest(flight_app, PICO2_BOARD, resolved, "dbuild", flight_west)

    try:
        sample = _sample(settings.SAMPLE)
        accel_range = settings.ACCEL_RANGE_G
        gyro_range = settings.GYRO_RANGE_DPS
        if isinstance(accel_range, bool) or not isinstance(accel_range, int) or accel_range not in (2, 4, 8, 16):
            raise ValueError("accel_range_g must be 2, 4, 8, or 16")
        if isinstance(gyro_range, bool) or not isinstance(gyro_range, int) or gyro_range not in (250, 500, 1000, 2000):
            raise ValueError("gyro_range_dps must be 250, 500, 1000, or 2000")
        boot = _positive(settings.BOOT_TIMEOUT_S, "boot_timeout_s")
        read = _positive(settings.READ_TIMEOUT_S, "read_timeout_s")
        utc = _positive(settings.UTC_TIMEOUT_S, "utc_timeout_s")
        tolerance = _positive(settings.TOLERANCE_MS, "tolerance_ms")
        tolerance_ns = round(tolerance * 1_000_000)
        if tolerance_ns <= 0 or tolerance_ns > 2**63 - 1:
            raise ValueError("tolerance_ms is outside supported range")
        if read > 20:
            raise ValueError("read_timeout_s cannot exceed the 20-second stimulus window")
    except (TypeError, ValueError, OverflowError) as exc:
        raise FlatsatError(f"Invalid imu-update parameters: {exc}") from exc
    return ImuUpdateParameters(
        imu_target, obc_target,
        emulator, obc_build, sample, int(accel_range), int(gyro_range), boot, read, utc, tolerance_ns,
    )


def _positive(value: object, name: str) -> float:
    if not _finite_number(value) or value <= 0 or value > threading.TIMEOUT_MAX:
        raise ValueError(f"{name} must be positive, finite, and supported")
    return float(value)


def _sample(raw: object) -> Sample:
    sample = _parse_sample(raw, "sample")
    if sample.sequence == 0xffffffff:
        raise ValueError("sample.sequence 0xffffffff is reserved")
    return sample


def run(bench: Bench, params: ImuUpdateParameters) -> Verdict:
    if bench.firmware is None or bench.bridge is None:
        raise FlatsatError("imu-update requires firmware and bridge")
    with bench.bridge.start("imu") as (link, utc):
        # Keep the same bridge and UART alive across both builds/flashes.
        bench.firmware.build_and_flash(params.imu_target, params.emulator_build)
        _wait_emulator_ready(link, params.boot_timeout_s)
        bench.firmware.build_and_flash(params.obc_target, params.obc_build)
        utc_result = utc.verify_current(params.utc_timeout_s, params.tolerance_ns)
        if not utc_result["passed"]:
            return Verdict("fail", (utc_result["reason"],))
        readiness = _verify_readback(link, params)
        link.runtime.write_metadata("imu_update_evidence", readiness)
        return Verdict("pass" if readiness["passed"] else "invalid", tuple(readiness["reasons"]) or ("emulator and OBC updated; UTC and complete post-stimulus sensor-bus readback verified",))


def _wait_emulator_ready(link, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    request_id = 1
    with link.connect(capture_name="emulator-ready-frames.jsonl") as session:
        while time.monotonic() < deadline:
            try:
                _get_status(session, request_id, min(1.0, deadline - time.monotonic()), link.config)
                return
            except TimeoutError:
                request_id += 1
    raise TimeoutError("emulator GetStatus not ready before OBC flash")


def _verify_readback(link, params: ImuUpdateParameters) -> dict:
    reasons: list[str] = []
    config = link.config
    request_id = 1
    transactions = {}
    duplicates = set()
    cursor = 0
    matching_tx = None
    baseline_bytes = None
    expected = _expected_burst_for(params.sample, params.accel_range_g, params.gyro_range_dps)
    baseline = None
    final = None
    wanted = False
    try:
        with link.connect(capture_name="imu-update-frames.jsonl") as session:
            # Reset-ready confirmation is retried only within a bounded boot window.
            boot_deadline = time.monotonic() + params.boot_timeout_s
            status = None
            while status is None and time.monotonic() < boot_deadline:
                try:
                    status = _get_status(session, request_id, min(1.0, boot_deadline - time.monotonic()), config)
                except TimeoutError:
                    request_id += 1
            if status is None:
                raise TimeoutError("emulator GetStatus via the bridge did not complete during the post-update boot window")
            cursor, records = _consume_transactions(session, cursor, config, reasons)
            _merge_transactions(transactions, duplicates, records)
            boot_count = status.transactions
            baseline_deadline = time.monotonic() + params.boot_timeout_s
            baseline_read = None
            while time.monotonic() < baseline_deadline and baseline_read is None:
                try:
                    session.receive(min(0.5, max(0.001, baseline_deadline - time.monotonic())))
                except TimeoutError:
                    pass
                cursor, records = _consume_transactions(session, cursor, config, reasons)
                _merge_transactions(transactions, duplicates, records)
                candidates = [t for t in transactions.values()
                              if _clean_burst(t)
                              and 0 < ((t.transaction_id - boot_count) & 0xffffffff) < 0x80000000]
                if candidates:
                    baseline_read = candidates[-1]
            if baseline_read is None:
                reasons.append("no clean normal OBC 14-byte MPU6050 read observed after boot")
                return {"passed": False, "reasons": reasons}
            baseline = _get_status(session, request_id + 1, params.boot_timeout_s, config)
            baseline_count = baseline.transactions
            baseline_distance = (baseline_read.transaction_id - boot_count) & 0xffffffff
            baseline_covered = 0 < baseline_distance <= ((baseline_count - boot_count) & 0xffffffff)
            if not baseline_covered:
                reasons.append("normal baseline read is not covered by the OBC status counter")
                return {"passed": False, "reasons": reasons, "stimulus_sequence": params.sample.sequence}
            if baseline.active_sample_sequence == params.sample.sequence:
                reasons.append("stimulus sequence is already active before injection; stale readback is unsafe")
                return {"passed": False, "reasons": reasons}
            baseline_bytes = baseline_read.read_data
            if baseline_bytes == expected:
                reasons.append("stimulus expected bytes equal baseline read; a value change is not proven")
                return {"passed": False, "reasons": reasons, "stimulus_sequence": params.sample.sequence,
                        "baseline_read_hex": baseline_bytes.hex(), "expected_read_hex": expected.hex()}
            stimulus_start = time.monotonic()
            session.send_frame(encode_imu_stimulus(config.device_address, time.time_ns(),
                ImuStimulusBody(params.sample.sequence, *params.sample.accel, *params.sample.gyro,
                                params.sample.temperature_c), source=config.controller_address))
            deadline = stimulus_start + params.read_timeout_s
            wrong_matching_read = False
            while time.monotonic() < deadline:
                try:
                    session.receive(min(0.5, max(0.001, deadline - time.monotonic())))
                except TimeoutError:
                    pass
                cursor, records = _consume_transactions(session, cursor, config, reasons)
                _merge_transactions(transactions, duplicates, records)
                for transaction in records:
                    distance = (transaction.transaction_id - baseline_count) & 0xffffffff
                    if 0 < distance < 0x80000000 and transaction.sample_sequence == params.sample.sequence:
                        matching_tx = transaction
                        wanted = transaction.read_register == 0x3b and transaction.read_count == 14 and len(transaction.read_data) == 14 and transaction.read_data == expected and transaction.flags == 0 and transaction.read_count <= transaction.read_length and transaction.write_count <= transaction.write_length
                        if transaction.read_register == 0x3b and transaction.read_count == 14 and len(transaction.read_data) == 14 and transaction.read_data != expected:
                            wrong_matching_read = True
                if wanted and not reasons:
                    break
            final = _get_status(session, request_id + 2, 1.0, config)
            cursor, records = _consume_transactions(session, cursor, config, reasons)
            _merge_transactions(transactions, duplicates, records)
            expected_count = (final.transactions - baseline_count) & 0xffffffff
            if expected_count > 10000:
                reasons.append("device transaction counter advanced beyond the bounded stimulus window")
            decisive_id = matching_tx.transaction_id if matching_tx is not None else None
            distance = ((decisive_id - baseline_count) & 0xffffffff) if decisive_id is not None else 0
            forward = 0 < distance <= min(expected_count, 10000)
            evidence_ids = {(baseline_count + i) & 0xffffffff for i in range(1, distance + 1)} if forward else set()
            if not forward:
                reasons.append("matching transaction is not covered by the final status counter")
            if evidence_ids - transactions.keys():
                reasons.append(f"I2C capture lost transaction IDs through decisive read: {sorted(evidence_ids - transactions.keys())}")
            if evidence_ids & duplicates:
                reasons.append(f"duplicate I2C transaction IDs through decisive read: {sorted(evidence_ids & duplicates)}")
            for txid in evidence_ids & transactions.keys():
                tx = transactions[txid]
                if tx.flags or tx.write_count > tx.write_length or tx.read_count > tx.read_length:
                    reasons.append(f"transaction {txid} is flagged or truncated")
            if matching_tx is None:
                reasons.append(f"no completed I2C transaction for injected sequence {params.sample.sequence}")
            elif not wanted:
                reasons.append("post-injection readback did not match the exact clean 0x3B 14-byte expected burst")
            if wrong_matching_read:
                reasons.append("a completed read for the injected sequence returned incorrect bytes")
            if _counter_changed(baseline.dropped_transactions, final.dropped_transactions):
                reasons.append("device dropped-transaction counter changed during stimulus")
            if _counter_changed(baseline.rx_errors, final.rx_errors):
                reasons.append("device receive-error counter changed during stimulus")
            if _counter_changed(baseline.rejected_samples, final.rejected_samples):
                reasons.append("device rejected a sample during stimulus")
    except TimeoutError as exc:
        reasons.append(str(exc))
    return {"passed": not reasons, "reasons": list(dict.fromkeys(reasons)),
            "stimulus_sequence": params.sample.sequence,
            "matching_transaction": _transaction_json(matching_tx), "expected_read_hex": expected.hex(),
            "baseline_read_hex": baseline_bytes.hex() if baseline_bytes is not None else None,
            "baseline_status": _status_json(baseline), "final_status": _status_json(final)}


def _clean_burst(transaction) -> bool:
    return (transaction.read_register == 0x3b and transaction.read_count == 14
            and len(transaction.read_data) == 14 and transaction.flags == 0
            and transaction.read_count <= transaction.read_length
            and transaction.write_count <= transaction.write_length)


def _transaction_json(transaction):
    if transaction is None:
        return None
    return {key: value.hex() if isinstance(value, bytes) else value
            for key, value in transaction.__dict__.items()}


def _status_json(status):
    if status is None:
        return None
    return dict(status.__dict__)
