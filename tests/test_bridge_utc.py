from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from experiments.utc_sync import parameters, run
from hitl_bridge.verify_utc import UtcVerification
from flatsat_controller.bench import Bench
from flatsat_controller.config import BenchConfig, Device
from flatsat_controller.firmware import FirmwareTools
from flatsat_controller.types import FlatsatError


class Processes:
    def run(self, *args, **kwargs):
        raise AssertionError("not used")

    def close(self):
        return []


class Firmware:
    def __init__(self, events, fail=False):
        self.events, self.fail = events, fail

    def build_and_flash(self, target, request):
        self.events.append(("flash", target.probe_serial, request.kind))
        if self.fail:
            raise FlatsatError("flash failed")


class BridgeCapability:
    def __init__(self, events):
        self.events = events
        self.passed = True

    @contextmanager
    def start(self, name):
        self.events.append(("start", name))
        observer = self
        class Link:
            pass
        yield Link(), observer

    def verify_current(self, timeout, tolerance):
        self.events.append(("verify", timeout, tolerance))
        return {"passed": self.passed, "reason": "UTC converged" if self.passed else "Timed out",
                "tolerance_ns": tolerance, "consecutive_in_tolerance": 2 if self.passed else 0}

    def verify_utc(self, name, *, timeout_s, tolerance_ns, require_correction):
        self.events.append(("verify", timeout_s, tolerance_ns, require_correction))
        return UtcVerification(name, self.passed, "UTC converged" if self.passed else "Timed out",
                               tolerance_ns, require_correction, self.passed,
                               2 if self.passed else 0, ())


class BridgeUtcTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "emulated-devices/mpu6050").mkdir(parents=True)
        west = self.root / "west"
        west.write_text("#!/bin/sh\n")
        west.chmod(0o755)
        openocd = self.root / "openocd"
        openocd.write_text("#!/bin/sh\n")
        openocd.chmod(0o755)
        self.config = BenchConfig(
            workspace=self.root,
            devices={"imu": Device("imu", "E665485457168830", "/dev/fake-uart", 1, 115200)},
            tools=FirmwareTools(west, openocd),
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_parameters_resolve_imu_probe_simulation_and_stock_build(self):
        params = parameters(self.config)
        self.assertEqual(params.target.name, "imu")
        self.assertEqual(params.target.probe_serial, "E665485457168830")
        self.assertEqual(params.tolerance_ns, 10_000_000)
        self.assertEqual(params.build.kind, "build")
        self.assertEqual(params.build.app_root, self.root / "emulated-devices/mpu6050")

    def test_missing_identity_or_wrong_stock_firmware_link_fails_preflight(self):
        with self.assertRaisesRegex(FlatsatError, "explicit devices.imu.probe_serial"):
            parameters(replace(self.config, devices={"imu": Device("imu", uart="/dev/fake", address=1)}))
        with self.assertRaisesRegex(FlatsatError, "address 1"):
            parameters(replace(self.config, devices={"imu": Device("imu", "SERIAL-1", "/dev/fake", 2, 115200)}))

    def test_flash_precedes_utc_and_failures_skip_verification(self):
        events = []
        capability = BridgeCapability(events)
        bench = Bench(operator=None, firmware=Firmware(events), bridge=capability)
        verdict = run(bench, parameters(self.config))
        self.assertEqual([event[0] for event in events], ["flash", "verify"])
        self.assertTrue(events[1][3], "cold-clock correction remains mandatory")
        self.assertEqual(verdict.outcome, "pass")
        events.clear()
        capability.passed = False
        verdict = run(bench, parameters(self.config))
        self.assertEqual(verdict.outcome, "fail")
        events.clear()
        bench = replace(bench, firmware=Firmware(events, fail=True))
        with self.assertRaisesRegex(FlatsatError, "flash failed"):
            run(bench, parameters(self.config))
        self.assertEqual([event[0] for event in events], ["flash"])

    def test_bridge_observer_records_current_verification_evidence(self):
        from flatsat_controller.bridge import UtcObserver
        from queue import Queue
        class Runtime:
            def __init__(self): self.metadata = {}
            def write_metadata(self, name, value): self.metadata[name] = value
        runtime = Runtime()
        observer = UtcObserver(Queue(), runtime, "imu")
        result = observer.verify_current(0.001, 10_000_000)
        self.assertFalse(result["passed"])
        self.assertEqual(runtime.metadata["utc_evidence"]["device"], "imu")


if __name__ == "__main__":
    unittest.main()
