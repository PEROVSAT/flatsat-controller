from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from queue import Queue
import time

from experiments.imu_update.experiment import _verify_readback, parameters
from experiments.imu_update.mpu6050 import Sample, expected_burst
from flatsat_controller.config import BenchConfig, Device
from flatsat_controller.firmware import FirmwareTools
from flatsat_controller.types import FlatsatError
from flatsat_controller.bridge import UtcObserver


class FakeSession:
    def __init__(self, records):
        self.received = []
        self.records = iter(records)
        self.pending_envelope_bytes = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def receive(self, _timeout):
        try:
            self.received.append(next(self.records))
        except StopIteration:
            raise TimeoutError("no more frames")

    def send_frame(self, _frame):
        pass


class FakeLink:
    config = SimpleNamespace(device_address=1, controller_address=253)

    def __init__(self, records):
        self.session = FakeSession(records)

    @contextmanager
    def connect(self, **_kwargs):
        yield self.session


class ImuUpdateTests(unittest.TestCase):
    def _params(self, timeout=0.01):
        sample = SimpleNamespace(sequence=1701, accel=(0.0, 0.0, 9.80665),
                                 gyro=(0.15, -0.25, 0.35), temperature_c=25.0)
        return SimpleNamespace(sample=sample, boot_timeout_s=0.1, read_timeout_s=timeout,
                               accel_range_g=2, gyro_range_dps=250)

    def _tx(self, txid, sequence, data):
        return SimpleNamespace(transaction_id=txid, flags=0, write_count=1, write_length=1,
                               read_count=14, read_length=14, read_register=0x3b,
                               read_data=data, sample_sequence=sequence)

    def _run_readback(self, *, post_data, final_count=2):
        params = self._params()
        expected = expected_burst(params.sample, 2, 250)
        baseline = self._tx(1, 0, b"\x01" * 14)
        post = self._tx(2, params.sample.sequence, post_data) if post_data is not None else None
        link = FakeLink([baseline] + ([post] if post else []))
        statuses = iter([
            SimpleNamespace(transactions=0, active_sample_sequence=0, dropped_transactions=0, rx_errors=0, rejected_samples=0),
            SimpleNamespace(transactions=1, active_sample_sequence=0, dropped_transactions=0, rx_errors=0, rejected_samples=0),
            SimpleNamespace(transactions=final_count, active_sample_sequence=params.sample.sequence, dropped_transactions=0, rx_errors=0, rejected_samples=0),
        ])

        def consume(session, cursor, _config, _reasons):
            return len(session.received), session.received[cursor:]

        with patch("experiments.imu_update.experiment._get_status", side_effect=lambda *_a, **_k: next(statuses)), \
             patch("experiments.imu_update.experiment._consume_transactions", side_effect=consume):
            result = _verify_readback(link, params)
        return result, expected

    def test_accepts_only_complete_expected_readback(self):
        params = self._params()
        expected = expected_burst(params.sample, 2, 250)
        result, _ = self._run_readback(post_data=expected)
        self.assertTrue(result["passed"], result["reasons"])

    def test_rejects_wrong_bytes_early_timeout_and_counter_reset(self):
        result, expected = self._run_readback(post_data=b"\x02" * 14)
        self.assertFalse(result["passed"])
        result, _ = self._run_readback(post_data=None)
        self.assertFalse(result["passed"])
        result, expected = self._run_readback(post_data=expected, final_count=0)
        self.assertFalse(result["passed"])
        self.assertTrue(any("counter" in reason or "covered" in reason for reason in result["reasons"]))

    def test_mpu6050_conversion_matches_known_bytes_and_clips(self):
        sample = Sample(1, (1.0, -1.0, 9.80665), (0.1, -0.2, 0.3), 30.0)
        self.assertEqual(expected_burst(sample, 2, 250), bytes.fromhex("0687f9794000f75402effa2308cc"))
        clipped = Sample(2, (100.0, -100.0, 0.0), (100.0, -100.0, 0.0), -100.0)
        burst = expected_burst(clipped, 2, 250)
        self.assertEqual(
            [int.from_bytes(burst[i:i + 2], "big", signed=True) for i in range(0, 14, 2)],
            [32767, -32768, 0, -32768, 32767, -32768, 0],
        )

    def test_missing_inventory_identity_fails_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            emulator = root / "emulated-devices" / "mpu6050"
            emulator.mkdir(parents=True)
            app = root / "flight"
            app.mkdir()
            west = root / "west"
            west.write_text("#!/bin/sh\n")
            west.chmod(0o755)
            openocd = root / "openocd"
            openocd.write_text("#!/bin/sh\n")
            openocd.chmod(0o755)
            (app / "dbuild.yml").write_text(
                "selections: {IMU: hardware, AMU: public-mock, FLASH: virtual, CONSOLE: uart}\n"
                "devices: {IMU: {modes: {hardware: {}, public-mock: {}}}, AMU: {modes: {public-mock: {}}}, FLASH: {modes: {virtual: {}}}, CONSOLE: {modes: {uart: {}}}}\n"
            )
            config = BenchConfig(
                workspace=root,
                devices={"imu": Device("imu", "IMUSERIAL", "/dev/fake", 1, 115200)},
                tools=FirmwareTools(west, openocd),
                flight_app=app,
            )
            with self.assertRaisesRegex(FlatsatError, "obc"):
                parameters(config)

    def test_current_utc_streak_discards_stale_and_accepts_two_sequential_ids(self):
        class Runtime:
            def write_metadata(self, _name, _value):
                pass

        queue = Queue()
        stale = time.time_ns() - 1_000_000
        fresh = time.time_ns() + 1_000_000

        def observation(identifier, t1):
            return SimpleNamespace(device="imu0",
                probe=SimpleNamespace(id=identifier, t1_ns=t1), t3_ns=t1 + 1,
                t4_ns=t1 + 2, result=SimpleNamespace(error_ns=0))

        queue.put(observation(9, stale))
        queue.put(observation(42, fresh))
        queue.put(observation(43, fresh + 10))
        result = UtcObserver(queue, Runtime(), "imu0").verify_current(0.1, 10_000_000)
        self.assertTrue(result["passed"])
        self.assertEqual([item.probe.id for item in result["observations"]], [42, 43])


if __name__ == "__main__":
    unittest.main()
