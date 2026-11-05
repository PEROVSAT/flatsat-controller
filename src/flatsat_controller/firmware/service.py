from __future__ import annotations
from pathlib import Path
import json
import subprocess
from flatsat_controller.runtime.context import Executor, Recording
from flatsat_controller.types import FlatsatError
from .types import BuildRequest, FirmwareArtifact, FirmwareTools, Target, PICO2_BOARD
from .dbuild import (
    load_dbuild, write_dbuild, flight_env, dbuild_argv, build_argv, flash_argv,
    validate_probe_serial,
)


class Firmware:
    def __init__(
        self, tools: FirmwareTools, executor: Executor, recording: Recording
    ) -> None:
        self._tools = tools
        self._executor = executor
        self._recording = recording

    def build(self, request: BuildRequest) -> FirmwareArtifact:
        app_root = request.app_root.resolve()
        west = self._require_west(request.west or self._tools.west)
        _require_pico2_board(request.board)
        config_path = None
        selections = dict(request.selections)
        if request.kind == "dbuild":
            source = load_dbuild(app_root)
            selections = _resolved_selections(source, selections)
        elif selections:
            raise FlatsatError("Plain west build does not accept device selections")
        operation = self._recording.allocate_operation("build")
        if request.kind == "dbuild":
            config_path = operation / "dbuild.yml"
            write_dbuild(source, selections, config_path)

        build_dir = operation / "build"
        env = flight_env(west)
        if request.kind == "dbuild":
            assert config_path is not None
            argv = dbuild_argv(
                west, board=request.board, config=config_path, build_dir=build_dir
            )
        else:
            argv = build_argv(
                west, board=request.board, app_root=app_root, build_dir=build_dir
            )
        metadata = {
            "app_root": str(app_root),
            "board": request.board,
            "selections": selections,
            "kind": request.kind,
            "config_path": str(config_path) if config_path else None,
            "build_dir": str(build_dir),
            "west": str(west.resolve()),
            "argv": argv,
            **_git_info(app_root),
        }
        (operation / "build.json").write_text(
            json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
        )
        log_path = operation / "build.log"
        code = self._execute(
            "build",
            argv,
            cwd=app_root,
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
            selections=selections,
            config_path=config_path,
            app_root=app_root,
            kind=request.kind,
            west=west.resolve(),
        )

    def build_and_flash(self, target: Target, request: BuildRequest) -> FirmwareArtifact:
        """Validate the complete operation before building, then flash its image."""
        _require_pico2_board(request.board)
        _require_pico2_board(target.board)
        validate_probe_serial(target.probe_serial)
        self._require_west(request.west or self._tools.west)
        self._require_openocd()
        artifact = self.build(request)
        self.flash(target, artifact)
        return artifact

    def flash(self, target: Target, artifact: FirmwareArtifact) -> None:
        _require_pico2_board(artifact.board)
        _require_pico2_board(target.board)
        validate_probe_serial(target.probe_serial)
        west = self._require_west(artifact.west or self._tools.west)
        self._require_openocd()
        if not artifact.image_path.is_file():
            raise FlatsatError(f"Firmware image not found: {artifact.image_path}")
        operation = self._recording.allocate_operation("flash")
        env = flight_env(west)
        argv = flash_argv(
            west,
            build_dir=artifact.build_dir,
            openocd=self._tools.openocd,
            probe_serial=target.probe_serial,
        )
        (operation / "flash.json").write_text(json.dumps({
            "target": target.name,
            "board": target.board,
            "probe_serial": target.probe_serial,
            "image_path": str(artifact.image_path),
            "build_dir": str(artifact.build_dir),
            "app_root": str(artifact.app_root),
            "kind": artifact.kind,
            "config_path": str(artifact.config_path) if artifact.config_path else None,
            "west": str(west.resolve()),
            "openocd": str(self._tools.openocd.resolve()),
            "argv": argv,
        }, indent=2) + "\n", encoding="utf-8")
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
            west=str(west.resolve()),
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

    def _require_west(self, west: Path | None) -> Path:
        if west is None:
            raise FlatsatError("No west workspace configured for this build")
        if not west.is_file():
            raise FlatsatError(f"west binary not found: {west}")
        return west

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


def _resolved_selections(source: dict, overrides: dict[str, str]) -> dict[str, str]:
    devices = source["devices"]
    defaults = source.get("selections", {})
    if not isinstance(defaults, dict):
        raise FlatsatError("dbuild selections must be a mapping")
    resolved = {}
    for device, mode in {**defaults, **overrides}.items():
        if not isinstance(device, str) or not isinstance(mode, str):
            raise FlatsatError("Device selections must map string names to string modes")
        if device not in devices:
            raise FlatsatError(
                f"Unknown device selection {device!r}; known devices: {sorted(devices)}"
            )
        modes = devices[device].get("modes") if isinstance(devices[device], dict) else None
        if not isinstance(modes, dict):
            raise FlatsatError(f"Device {device!r} has no valid modes mapping")
        normalized = mode.lower()
        if normalized not in modes:
            raise FlatsatError(
                f"Device {device!r}: mode {mode!r} is not available; known modes: {sorted(modes)}"
            )
        resolved[device] = normalized
    return resolved


def _require_pico2_board(board: str) -> None:
    if board != PICO2_BOARD:
        raise FlatsatError(f"Only Pico 2 RP2350A ARM is supported; expected board {PICO2_BOARD}, got {board}")
