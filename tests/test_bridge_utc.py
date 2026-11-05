from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from hitl_bridge.verify_utc import UtcVerification
from experiments.bridge_utc import parameters, run
from flatsat_controller.bench import Bench
from flatsat_controller.config import load_config
from flatsat_controller.runtime.context import RunContext
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


class BridgeUtcTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "app").mkdir()
        (root / "inventory.yml").write_text(
            "listen: 127.0.0.1:4242\ntargets:\n"
            "  - name: imu0\n    board: rpi_pico2/rp2350a/m33\n"
            "    probe_serial: 'E665485457168830'\n    simulation:\n"
            "      address: 1\n      port: /dev/fake-uart\n", encoding="utf-8"
        )
        (root / "controller.yml").write_text(
            "bridge_config: inventory.yml\nbuilds:\n  emulator:\n"
            f"    app_root: {root / 'app'}\n    board: rpi_pico2/rp2350a/m33\n"
            "    kind: build\n    west: /fake/west\n"
            "experiment:\n  timeout_s: 20\n  tolerance_ms: 10\n", encoding="utf-8"
        )
        self.config = load_config(root / "controller.yml")

    def tearDown(self):
        self.temp.cleanup()

    def test_parameters_resolve_imu_probe_and_simulation(self):
        params = parameters(self.config)
        self.assertEqual(params.target.name, "imu0")
        self.assertEqual(params.target.probe_serial, "E665485457168830")
        self.assertEqual(params.tolerance_ns, 10_000_000)
        self.assertEqual(params.build.kind, "build")

    def test_missing_identity_fails_before_flash(self):
        config = replace(self.config, bridge_config=replace(self.config.bridge_config, targets=()))
        with self.assertRaisesRegex(FlatsatError, "unknown flash target"):
            parameters(config)

    def test_flash_precedes_utc_and_failures_skip_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            run_dir.mkdir(exist_ok=True)
            runtime = RunContext(run_dir, Processes())
            events = []
            from flatsat_controller.bridge import Bridge
            bench = Bench(operator=None, firmware=Firmware(events), bridge=Bridge(self.config.bridge_config, runtime))
            result = UtcVerification("imu0", True, "UTC converged within tolerance", 10_000_000,
                                     True, True, 2, ())

            def verify(*args, **kwargs):
                events.append(("verify",))
                return result

            with patch("flatsat_controller.bridge.verify_utc", verify):
                verdict = run(bench, parameters(self.config))
            self.assertEqual([event[0] for event in events], ["flash", "verify"])
            self.assertEqual(verdict.outcome, "pass")
            self.assertTrue((run_dir / "utc_evidence.json").is_file())

            events.clear()
            failed = UtcVerification("imu0", False, "Timed out", 10_000_000,
                                     True, False, 0, ())
            with patch("flatsat_controller.bridge.verify_utc", lambda *a, **k: failed):
                verdict = run(bench, parameters(self.config))
            self.assertEqual(verdict.outcome, "fail")

            events.clear()
            bench = replace(bench, firmware=Firmware(events, fail=True))
            with patch("flatsat_controller.bridge.verify_utc", verify):
                with self.assertRaisesRegex(FlatsatError, "flash failed"):
                    run(bench, parameters(self.config))
            self.assertEqual([event[0] for event in events], ["flash"])

    def test_requires_emulator_build_and_nonzero_rounded_tolerance(self):
        with self.assertRaisesRegex(FlatsatError, "builds.emulator"):
            parameters(replace(self.config, builds={"default": self.config.builds["emulator"]}))
        with self.assertRaisesRegex(FlatsatError, "tolerance_ms"):
            parameters(replace(self.config, experiment={"tolerance_ms": 0.0000001}))
        with self.assertRaisesRegex(FlatsatError, "timeout_s"):
            parameters(replace(self.config, experiment={"timeout_s": 10**1000}))

    def test_verifier_startup_failure_retains_log_and_original_error(self):
        from flatsat_controller.bridge import Bridge

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = RunContext(root, Processes())
            bridge = Bridge(self.config.bridge_config, runtime)
            with patch("flatsat_controller.bridge.verify_utc", side_effect=OSError("UART busy")):
                with self.assertRaisesRegex(OSError, "UART busy"):
                    bridge.verify_utc("imu0", timeout_s=1, tolerance_ns=1, require_correction=True)
            self.assertTrue((root / "bridge-utc.log").exists())
            self.assertIn("UART busy", (root / "utc_evidence.json").read_text())


if __name__ == "__main__":
    unittest.main()
