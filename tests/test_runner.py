from __future__ import annotations

import json
import os
import signal
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

import yaml

from flatsat_controller.config import BenchConfig as BenchSettings, Device
from flatsat_controller.firmware import BuildRequest, FirmwareTools, Firmware
from flatsat_controller.assembly import assemble_bench
from flatsat_controller.runtime import RunContext
from flatsat_controller.processes import ProcessGroup
from flatsat_controller.runner import run_experiment
from flatsat_controller.types import FlatsatError, Verdict


def BenchConfig(*, app_root, board, west, openocd=None):
    tools = FirmwareTools(west, west if openocd is None else openocd)
    return BenchSettings(
        workspace=app_root.parent,
        devices={"obc": Device("obc", "OBC-SERIAL")},
        tools=tools,
        flight_app=app_root,
        west=west,
    )


@dataclass(frozen=True)
class GenericParameters:
    build: BuildRequest


def run_installed(name, func, config, **kwargs):
    parameters = GenericParameters(
        BuildRequest(config.flight_app, "rpi_pico2/rp2350a/m33", kind="dbuild", west=config.flight_west)
    )
    return run_experiment(
        name,
        func,
        parameters,
        bench_factory=lambda runtime: assemble_bench(runtime, config),
        **kwargs,
    )


DBUILD_SOURCE = {
    "selections": {},
    "devices": {
        "IMU": {"modes": {"public-mock": {}}},
        "AMU": {"modes": {"public-mock": {}}},
        "FLASH": {"modes": {"virtual": {}, "pico2": {}}},
        "CONSOLE": {"modes": {"no-console": {}, "usb": {}, "logs": {}, "uart": {}}},
    },
}


def _build_dir_from_argv(argv: list[str]) -> Path | None:
    for i, part in enumerate(argv):
        if part == "-d" and i + 1 < len(argv):
            return Path(argv[i + 1])
    return None


class FakeProcessGroup:
    def __init__(self, returncode: int = 0, *, fail_build: bool = False) -> None:
        self.returncode = returncode if not fail_build else 1
        self.records: list[dict] = []
        self.close_calls = 0

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        log_path: Path,
    ) -> int:
        self.records.append(
            {"argv": list(argv), "cwd": cwd, "env": dict(env), "log_path": log_path}
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("fake log\n")
        code = self.returncode
        if code == 0 and "dbuild" in argv:
            build_dir = _build_dir_from_argv(argv)
            if build_dir is not None:
                image = build_dir / "zephyr" / "zephyr.hex"
                image.parent.mkdir(parents=True, exist_ok=True)
                image.write_bytes(b":00000001FF\n")
        return code

    def start(self, argv, *, cwd, env, log_path):
        raise NotImplementedError

    def close(self) -> list[str]:
        self.close_calls += 1
        return []


class BenchBuildFailureTests(unittest.TestCase):
    def test_build_failure_records_action_and_runner_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "app"
            app.mkdir()
            (app / "dbuild.yml").write_text(yaml.safe_dump(DBUILD_SOURCE))
            west = root / "west"
            west.write_text("#!/bin/sh\n")
            west.chmod(0o755)

            cfg = BenchConfig(app_root=app, board="rpi_pico2", west=west)
            fake = FakeProcessGroup(fail_build=True)

            def exp(bench, parameters):
                bench.firmware.build(
                    BuildRequest(
                        parameters.build.app_root,
                        parameters.build.board,
                        {
                            "IMU": "public-mock",
                            "AMU": "public-mock",
                    "FLASH": "pico2",
                    "CONSOLE": "usb",
                        },
                    )
                )

            runs = root / "runs"
            result = run_installed(
                "t",
                exp,
                cfg,
                runs_root=runs,
                processes=fake,
            )
            self.assertEqual(result.status, "failed")
            self.assertIsNone(result.verdict)
            self.assertTrue((result.run_dir / "result.json").is_file())
            actions = [
                json.loads(line)
                for line in (result.run_dir / "actions.jsonl").read_text().splitlines()
            ]
            self.assertEqual(actions[0]["action"], "build")
            self.assertEqual(actions[0]["returncode"], 1)


class SuccessfulExperimentTests(unittest.TestCase):
    def test_generic_experiment_records_result_and_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "app"
            app.mkdir()
            west = root / "west"
            west.write_text("#!/bin/sh\n")
            west.chmod(0o755)
            cfg = BenchConfig(app_root=app, board="rpi_pico2", west=west)

            def exp(_bench, parameters):
                self.assertEqual(parameters.build.app_root, app)
                return Verdict("pass", ("generic sequence complete",))

            result = run_installed(
                "generic",
                exp,
                cfg,
                runs_root=root / "runs",
                processes=FakeProcessGroup(),
            )
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.verdict, "pass")
            self.assertEqual(result.reasons, ("generic sequence complete",))
            self.assertTrue((result.run_dir / "parameters.json").is_file())
            self.assertTrue((result.run_dir / "result.json").is_file())


class ExperimentReturnTests(unittest.TestCase):
    def test_verdict_return_completes_with_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "app"
            app.mkdir()
            west = root / "west"
            west.write_text("#!/bin/sh\n")
            west.chmod(0o755)
            cfg = BenchConfig(app_root=app, board="b", west=west)

            def exp(_bench, _config):
                return Verdict("fail", ("readings",))

            result = run_installed(
                "t",
                exp,
                cfg,
                runs_root=root / "runs",
                processes=FakeProcessGroup(),
            )
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.verdict, "fail")
            self.assertEqual(result.reasons, ("readings",))

    def test_unexpected_return_fails_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "app"
            app.mkdir()
            west = root / "west"
            west.write_text("#!/bin/sh\n")
            west.chmod(0o755)
            cfg = BenchConfig(app_root=app, board="b", west=west)

            def exp(_bench, _config):
                return "not-a-verdict"

            result = run_installed(
                "t",
                exp,
                cfg,
                runs_root=root / "runs",
                processes=FakeProcessGroup(),
            )
            self.assertEqual(result.status, "failed")
            self.assertIsNone(result.verdict)


class KeyboardInterruptTests(unittest.TestCase):
    def test_interrupt_still_closes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "app"
            app.mkdir()
            west = root / "west"
            west.write_text("#!/bin/sh\n")
            west.chmod(0o755)
            cfg = BenchConfig(app_root=app, board="rpi_pico2", west=west)
            fake = FakeProcessGroup()

            def exp(_bench, _config):
                raise KeyboardInterrupt

            result = run_installed(
                "t",
                exp,
                cfg,
                runs_root=root / "runs",
                processes=fake,
            )
            self.assertEqual(result.status, "interrupted")
            self.assertTrue((result.run_dir / "result.json").is_file())
            self.assertEqual(fake.close_calls, 1)


class ProcessGroupCloseTests(unittest.TestCase):
    def test_close_terminates_subprocess(self) -> None:
        group = ProcessGroup()
        self.addCleanup(group.close)
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "sleep.log"
            handle = group.start(
                ["sleep", "30"],
                cwd=Path(tmp),
                env={"PATH": "/usr/bin:/bin"},
                log_path=log,
            )
            self.assertTrue(handle.running)
            time.sleep(0.2)
            errors = group.close()
            self.assertEqual(errors, [])
            self.assertIsNotNone(handle.poll())


class ProcessGroupRunInterruptTests(unittest.TestCase):
    def test_sigint_leaves_child_tracked_then_close_kills(self) -> None:
        group = ProcessGroup()
        self.addCleanup(group.close)
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "sleep.log"

            def send_sigint() -> None:
                time.sleep(0.5)
                os.kill(os.getpid(), signal.SIGINT)

            threading.Thread(target=send_sigint, daemon=True).start()
            with self.assertRaises(KeyboardInterrupt):
                group.run(
                    ["sleep", "30"],
                    cwd=Path(tmp),
                    env={"PATH": "/usr/bin:/bin"},
                    log_path=log,
                )

            self.assertEqual(len(group._handles), 1)
            pid = group._handles[0].pid
            self.assertTrue(group._handles[0].running)
            errors = group.close()
            self.assertEqual(errors, [])
            self.assertEqual(group._handles, [])
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)


class BenchMissingUf2Tests(unittest.TestCase):
    def test_missing_uf2_after_success_raises(self) -> None:
        class NoUf2Fake(FakeProcessGroup):
            def run(self, argv, *, cwd, env, log_path):
                self.records.append({"argv": argv})
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text("ok")
                return 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "app"
            app.mkdir()
            (app / "dbuild.yml").write_text(yaml.safe_dump(DBUILD_SOURCE))
            west = root / "west"
            west.write_text("#!/bin/sh\n")
            west.chmod(0o755)
            run_dir = root / "run"
            run_dir.mkdir()
            runtime = RunContext(run_dir, NoUf2Fake())
            firmware = Firmware(FirmwareTools(west), runtime.processes, runtime)
            with self.assertRaises(FlatsatError):
                firmware.build(BuildRequest(app, "b", {"IMU": "public-mock"}))


if __name__ == "__main__":
    unittest.main()
