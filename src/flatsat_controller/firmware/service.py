from __future__ import annotations
from pathlib import Path
import json
import subprocess
from flatsat_controller.runtime.context import Executor, Recording
from flatsat_controller.types import FlatsatError
from .types import BuildRequest, FirmwareArtifact, FirmwareTools, Target
from .dbuild import load_dbuild, write_dbuild, flight_env, dbuild_argv, flash_argv


class Firmware:
    def __init__(
        self, tools: FirmwareTools, executor: Executor, recording: Recording
    ) -> None:
        self._tools = tools
        self._executor = executor
        self._recording = recording

    def build(self, request: BuildRequest) -> FirmwareArtifact:
        self._require_west()
        operation = self._recording.allocate_operation("build")
        source = load_dbuild(request.app_root)

        config_path = operation / "dbuild.yml"
        write_dbuild(source, request.selections, config_path)

        build_dir = operation / "build"
        env = flight_env(self._tools.west)
        argv = dbuild_argv(
            self._tools.west,
            board=request.board,
            config=config_path,
            build_dir=build_dir,
        )
        metadata = {
            "app_root": str(request.app_root.resolve()),
            "board": request.board,
            "selections": dict(request.selections),
            "config_path": str(config_path),
            "build_dir": str(build_dir),
            "west": str(self._tools.west),
            "argv": argv,
            **_git_info(request.app_root),
        }
        (operation / "build.json").write_text(
            json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
        )
        log_path = operation / "build.log"
        code = self._execute(
            "build",
            argv,
            cwd=request.app_root,
            env=env,
            operation=operation,
            log_path=log_path,
        )
        if code != 0:
            raise FlatsatError(_command_failure("build", log_path, code))

        image_path = build_dir / "zephyr" / "zephyr.hex"
        if not image_path.is_file():
            raise FlatsatError(f"Build succeeded but hex image missing: {image_path}")

        return FirmwareArtifact(
            image_path=image_path,
            build_dir=build_dir,
            board=request.board,
            selections=dict(request.selections),
            config_path=config_path,
            app_root=request.app_root,
        )

    def flash(self, target: Target, artifact: FirmwareArtifact) -> None:
        self._require_west()
        self._require_openocd()
        if target.board != artifact.board:
            raise FlatsatError(
                f"Target {target.name} board {target.board} does not match artifact {artifact.board}"
            )
        operation = self._recording.allocate_operation("flash")
        env = flight_env(self._tools.west)
        argv = flash_argv(
            self._tools.west,
            build_dir=artifact.build_dir,
            openocd=self._tools.openocd,
            probe_serial=target.probe_serial,
        )
        log_path = operation / "flash.log"
        code = self._execute(
            "flash",
            argv,
            cwd=artifact.app_root,
            env=env,
            operation=operation,
            log_path=log_path,
            target=target.name,
            probe_serial=target.probe_serial,
            image_path=str(artifact.image_path),
            build_dir=str(artifact.build_dir),
        )
        if code != 0:
            raise FlatsatError(_command_failure("flash", log_path, code))

    def _execute(
        self,
        action: str,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        operation: Path,
        log_path: Path,
        **metadata: object,
    ) -> int:
        record = {
            "action": action,
            "operation_dir": str(operation),
            "argv": argv,
            **metadata,
        }
        try:
            code = self._executor.run(argv, cwd=cwd, env=env, log_path=log_path)
        except BaseException as exc:
            self._recording.record(
                {**record, "error": type(exc).__name__, "message": str(exc)}
            )
            raise
        self._recording.record({**record, "returncode": code})
        return code

    def _require_west(self) -> None:
        if not self._tools.west.is_file():
            raise FlatsatError(f"west binary not found: {self._tools.west}")

    def _require_openocd(self) -> None:
        if not self._tools.openocd.is_file():
            raise FlatsatError(f"openocd binary not found: {self._tools.openocd}")


def _command_failure(label: str, log_path: Path, code: int) -> str:
    tail = _tail_lines(log_path, 30)
    msg = f"{label} failed (exit {code}); log: {log_path}"
    if tail:
        msg += "\n--- log tail ---\n" + tail
    return msg


def _tail_lines(path: Path, n: int) -> str:
    if not path.is_file():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])


def _git_info(app_root: Path) -> dict[str, str | bool | None]:
    try:
        rev = subprocess.run(
            ["git", "-C", str(app_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(app_root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return {"git_rev": rev, "git_dirty": bool(status.strip())}
    except (subprocess.CalledProcessError, OSError):
        return {"git_rev": None, "git_dirty": None}
