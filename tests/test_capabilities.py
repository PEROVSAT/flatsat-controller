from __future__ import annotations
import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch
import yaml
from flatsat_controller.bench import Bench
from flatsat_controller.cli import main
from flatsat_controller.config import load_config
from flatsat_controller.firmware import (
    BuildRequest,
    Firmware,
    FirmwareTools,
    Target,
    flash_argv,
)
from flatsat_controller.runtime import Operator, RunContext
from flatsat_controller.runner import run_experiment
from flatsat_controller.types import FlatsatError, Verdict
from experiments import Experiment
from test_runner import FakeProcessGroup, DBUILD_SOURCE


class CapabilityTests(unittest.TestCase):
    def test_config_init_writes_editable_valid_yaml_without_overwriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "flatsat-controller").mkdir()
            (root / ".west").mkdir()
            (root / ".west" / "config").touch()
            with patch("flatsat_controller.cli.find_workspace", return_value=root):
                main(["config", "init"])
                content = (root / "bench.yml").read_text()
                parsed = yaml.safe_load(content)
                self.assertEqual(parsed["tools"], {"openocd": None})
                self.assertEqual(parsed["flight"], {"app_root": None, "west": None})
                with self.assertRaises(SystemExit) as caught:
                    main(["config", "init"])
                self.assertEqual(caught.exception.code, 1)
                self.assertEqual((root / "bench.yml").read_text(), content)

    def test_operator_and_runner_require_no_firmware_installation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeProcessGroup()

            @dataclass(frozen=True)
            class Parameters:
                message: str

            def experiment(bench, params):
                self.assertIsNone(bench.firmware)
                self.assertEqual(bench.targets, {})
                bench.operator.confirm(params.message)
                return Verdict("pass")

            result = run_experiment(
                "manual",
                experiment,
                Parameters("Inspect"),
                runs_root=Path(tmp),
                processes=fake,
                bench_factory=lambda runtime: Bench(
                    Operator(runtime, auto_confirm=True)
                ),
            )
            self.assertEqual(result.verdict, "pass")
            self.assertEqual(fake.close_calls, 1)
            self.assertEqual(
                json.loads((result.run_dir / "parameters.json").read_text()),
                {"message": "Inspect"},
            )

    def test_cli_supports_registered_no_firmware_experiment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "bench.yml"
            (root / "flatsat-controller").mkdir()
            (root / ".west").mkdir()
            (root / ".west" / "config").touch()
            config.write_text("devices: {}\n")

            @dataclass(frozen=True)
            class Parameters:
                message: str

            def experiment(bench, params):
                bench.operator.confirm(params.message)

            entry = Experiment(experiment, lambda config: Parameters("Check bench"), "manual")
            with patch.dict("flatsat_controller.cli.EXPERIMENTS", {"manual": entry}):
                with patch("flatsat_controller.cli.find_workspace", return_value=root):
                    with self.assertRaises(SystemExit) as caught:
                        main(["run", "manual", "--yes"])
                self.assertEqual(caught.exception.code, 0)
            default_runs = root / "flatsat-controller" / "experiments" / "manual" / "runs"
            result = next(default_runs.glob("*/result.json"))
            self.assertRegex(result.parent.name, r"^\d{8}T\d{6}Z(?:-\d+)?$")
            self.assertEqual(json.loads(result.read_text())["status"], "completed")

    def test_cli_displays_failed_and_invalid_verdicts_and_exits_nonzero(self):
        import contextlib
        import io
        from flatsat_controller.types import ExperimentResult

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "bench.yml"
            config.write_text("{}\n")
            (root / "flatsat-controller").mkdir(exist_ok=True)
            (root / ".west").mkdir(exist_ok=True)
            (root / ".west" / "config").touch()
            (root / "bench.yml").write_text("devices: {}\n")
            entry = Experiment(lambda *_: None, lambda _: None, "manual")
            for verdict in ("fail", "invalid"):
                with self.subTest(verdict=verdict):
                    result = ExperimentResult(
                        "manual",
                        "completed",
                        verdict,
                        ("readback did not satisfy the run criteria",),
                        root,
                        (),
                    )
                    stdout = io.StringIO()
                    with (
                        patch.dict(
                            "flatsat_controller.cli.EXPERIMENTS",
                            {"manual": entry},
                        ),
                        patch(
                            "flatsat_controller.cli.run_experiment",
                            return_value=result,
                        ),
                        patch("flatsat_controller.cli.find_workspace", return_value=root),
                        contextlib.redirect_stdout(stdout),
                    ):
                        with self.assertRaises(SystemExit) as caught:
                            main(["run", "manual", "--runs-root", str(root / "runs")])
                    self.assertEqual(caught.exception.code, 1)
                    self.assertIn(f"verdict: {verdict}", stdout.getvalue())
                    self.assertIn("readback did not satisfy", stdout.getvalue())

    def test_two_apps_preserve_artifacts_and_flash_selected_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tools = FirmwareTools(root / "west", root / "openocd")
            tools.west.touch()
            tools.openocd.touch()
            fake = FakeProcessGroup()
            runtime = RunContext(root, fake)
            firmware = Firmware(tools, fake, runtime)
            artifacts = []
            for index in range(2):
                app = root / f"app-{index}"
                app.mkdir()
                catalog = yaml.safe_load(yaml.safe_dump(DBUILD_SOURCE))
                catalog["devices"]["IMU"]["modes"][f"mode-{index}"] = {}
                (app / "dbuild.yml").write_text(yaml.safe_dump(catalog))
                selections = {"IMU": f"mode-{index}"}
                request = BuildRequest(app, "rpi_pico2/rp2350a/m33", selections)
                selections["IMU"] = "changed-after-construction"
                artifacts.append(firmware.build(request))
            first, second = artifacts
            self.assertNotEqual(first.build_dir, second.build_dir)
            self.assertNotEqual(first.config_path, second.config_path)
            for index, artifact in enumerate(artifacts):
                self.assertTrue(artifact.image_path.is_file())
                self.assertEqual(
                    yaml.safe_load(artifact.config_path.read_text())["selections"],
                    {"IMU": f"mode-{index}"},
                )
                self.assertEqual(
                    (artifact.build_dir.parent / "build.log").read_text(), "fake log\n"
                )
                metadata = json.loads(
                    (artifact.build_dir.parent / "build.json").read_text()
                )
                self.assertEqual(
                    metadata["app_root"], str((root / f"app-{index}").resolve())
                )
                self.assertEqual(metadata["board"], "rpi_pico2/rp2350a/m33")
            firmware.flash(Target("emulator", "rpi_pico2/rp2350a/m33", "probe_12:34"), first)
            self.assertEqual(fake.records[-1]["cwd"], first.app_root)
            self.assertEqual(
                fake.records[-1]["argv"][-2:],
                ["--cmd-pre-init", "adapter serial probe_12:34"],
            )
            firmware.flash(Target("obc", "rpi_pico2/rp2350a/m33"), second)
            self.assertNotIn("--cmd-pre-init", fake.records[-1]["argv"])
            actions = [
                json.loads(line)
                for line in (root / "actions.jsonl").read_text().splitlines()
            ]
            self.assertEqual(actions[-2]["target"], "emulator")
            self.assertEqual(len({entry["operation_dir"] for entry in actions}), 4)
            with self.assertRaises(FlatsatError):
                firmware.flash(Target("wrong", "rpi_pico2/rp2350a/hazard3"), first)
            self.assertEqual(len(fake.records), 4)

    def test_probe_serial_rejects_tcl_injection(self):
        with self.assertRaises(FlatsatError):
            flash_argv(
                Path("/west"),
                build_dir=Path("/build"),
                openocd=Path("/openocd"),
                probe_serial="x; shutdown",
            )

    def test_factory_failure_is_finalized_and_cleanup_failure_retained(self):
        class BrokenClose(FakeProcessGroup):
            def close(self):
                super().close()
                raise RuntimeError("cleanup problem")

        with tempfile.TemporaryDirectory() as tmp:
            fake = BrokenClose()

            def factory(runtime):
                raise RuntimeError("assembly problem")

            result = run_experiment(
                "broken",
                lambda *_: None,
                None,
                runs_root=Path(tmp),
                processes=fake,
                bench_factory=factory,
            )
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.reasons, ("assembly problem",))
            self.assertEqual(result.cleanup_errors, ("cleanup problem",))
            self.assertEqual(fake.close_calls, 1)
            self.assertTrue((result.run_dir / "result.json").is_file())

    def test_interrupted_firmware_records_action_and_runner_cleans(self):
        class Interrupted(FakeProcessGroup):
            def run(self, *args, **kwargs):
                raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dbuild.yml").write_text(yaml.safe_dump(DBUILD_SOURCE))
            west = root / "west"
            west.touch()
            fake = Interrupted()

            def factory(runtime):
                return Bench(
                    Operator(runtime),
                    firmware=Firmware(FirmwareTools(west), fake, runtime),
                )

            result = run_experiment(
                "interrupt",
                lambda bench, request: bench.firmware.build(request),
                BuildRequest(root, "rpi_pico2/rp2350a/m33"),
                runs_root=root / "runs",
                processes=fake,
                bench_factory=factory,
            )
            self.assertEqual(result.status, "interrupted")
            self.assertEqual(fake.close_calls, 1)
            record = json.loads((result.run_dir / "actions.jsonl").read_text())
            self.assertEqual(record["error"], "KeyboardInterrupt")


class CleanupFailureTests(unittest.TestCase):
    def test_cleanup_failure_changes_success_status_preserving_verdict(self):
        class BrokenClose(FakeProcessGroup):
            def close(self):
                return ["child did not stop"]

        with tempfile.TemporaryDirectory() as tmp:
            result = run_experiment(
                "cleanup",
                lambda *_: Verdict("pass", ("readings matched",)),
                None,
                runs_root=Path(tmp),
                processes=BrokenClose(),
                bench_factory=lambda runtime: object(),
            )
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.verdict, "pass")
            self.assertEqual(result.reasons, ("readings matched",))
            self.assertEqual(result.cleanup_errors, ("child did not stop",))
            saved = json.loads((result.run_dir / "result.json").read_text())
            self.assertEqual(saved["status"], "failed")
            self.assertEqual(saved["cleanup_errors"], ["child did not stop"])

    def test_cleanup_failure_preserves_execution_failure_and_interruption(self):
        class BrokenClose(FakeProcessGroup):
            def close(self):
                return ["child did not stop"]

        for failure, status, reasons in [
            (FlatsatError("original failure"), "failed", ("original failure",)),
            (KeyboardInterrupt(), "interrupted", ()),
        ]:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:

                def experiment(*_):
                    raise failure

                result = run_experiment(
                    "cleanup",
                    experiment,
                    None,
                    runs_root=Path(tmp),
                    processes=BrokenClose(),
                    bench_factory=lambda runtime: object(),
                )
                self.assertEqual(result.status, status)
                self.assertEqual(result.reasons, reasons)
                self.assertEqual(result.cleanup_errors, ("child did not stop",))

    def test_cli_prints_cleanup_errors_and_exits_nonzero(self):
        import contextlib
        import io
        from flatsat_controller.types import ExperimentResult

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "bench.yml"
            (root / "flatsat-controller").mkdir()
            (root / ".west").mkdir()
            (root / ".west" / "config").touch()
            config.write_text("devices: {}")
            entry = Experiment(lambda *_: None, lambda _: None, "manual")
            result = ExperimentResult(
                "manual", "failed", "pass", (), root, ("child did not stop",)
            )
            output = io.StringIO()
            with (
                patch.dict("flatsat_controller.cli.EXPERIMENTS", {"manual": entry}),
                patch("flatsat_controller.cli.run_experiment", return_value=result),
                patch("flatsat_controller.cli.find_workspace", return_value=root),
                contextlib.redirect_stderr(output),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as caught:
                    main(["run", "manual"])
            self.assertEqual(caught.exception.code, 1)
            self.assertIn("cleanup error: child did not stop", output.getvalue())

    def test_process_cleanup_continues_after_timeout_or_oserror(self):
        import io
        import subprocess
        from unittest.mock import Mock
        from flatsat_controller.runtime.processes import ProcessGroup, ProcessHandle

        for failure in [
            subprocess.TimeoutExpired(["stuck"], 2),
            OSError("stop failed"),
        ]:
            with self.subTest(failure=type(failure).__name__):
                group = ProcessGroup()
                stuck = Mock()
                stuck.poll.return_value = None
                stuck.wait.side_effect = failure
                if isinstance(failure, OSError):
                    stuck.terminate.side_effect = failure
                other = Mock()
                other.poll.return_value = None
                other.wait.return_value = 0
                logs = [io.StringIO(), io.StringIO()]
                handles = [
                    ProcessHandle(1, ["stuck"], Path("one.log"), stuck, logs[0]),
                    ProcessHandle(2, ["other"], Path("two.log"), other, logs[1]),
                ]
                for handle in handles:
                    group._append(handle)
                errors = group.close()
                self.assertEqual(len(errors), 1)
                self.assertIn("pid 1", errors[0])
                other.terminate.assert_called_once()
                other.wait.assert_called_once_with(timeout=2.0)
                self.assertTrue(all(log.closed for log in logs))
                if isinstance(failure, subprocess.TimeoutExpired):
                    stuck.kill.assert_called_once()
                    self.assertEqual(stuck.wait.call_count, 2)
