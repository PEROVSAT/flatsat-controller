from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Mapping
import yaml
from flatsat_controller.types import FlatsatError
from flatsat_controller.firmware import FirmwareTools, Target, BuildRequest


@dataclass(frozen=True)
class Installation:
    tools: FirmwareTools | None = None
    targets: Mapping[str, Target] = field(default_factory=dict)


@dataclass(frozen=True)
class ControllerConfig:
    installation: Installation
    builds: Mapping[str, BuildRequest] = field(default_factory=dict)
    experiment: Mapping[str, object] = field(default_factory=dict)


def load_config(path: Path) -> ControllerConfig:
    config_path = path.resolve()
    if not config_path.is_file():
        raise FlatsatError(f"Config file not found: {config_path}")
    with config_path.open(encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    if not isinstance(raw, dict):
        raise FlatsatError(f"Config must be a YAML mapping: {config_path}")
    unknown = set(raw) - {
        "app_root",
        "board",
        "west",
        "openocd",
        "targets",
        "builds",
        "experiment",
    }
    if unknown:
        raise FlatsatError(f"Unknown config keys {sorted(unknown)} in {config_path}")
    base = config_path.parent
    build_data = raw.get("builds", {})
    if not isinstance(build_data, dict):
        raise FlatsatError("builds must be a mapping")
    build_data = dict(build_data)
    if "app_root" in raw or "board" in raw:
        if not {"app_root", "board"} <= raw.keys():
            raise FlatsatError("Legacy config requires both app_root and board")
        if "default" in build_data:
            raise FlatsatError("Use either legacy app_root/board or builds.default")
        build_data["default"] = {"app_root": raw["app_root"], "board": raw["board"]}
    builds = {}
    for name, spec in build_data.items():
        if not isinstance(name, str) or not isinstance(spec, dict):
            raise FlatsatError("Each build must be a named mapping")
        if (
            set(spec) - {"app_root", "board", "selections"}
            or not {"app_root", "board"} <= spec.keys()
        ):
            raise FlatsatError(
                f"Build {name}: expected app_root, board, optional selections"
            )
        app = _resolve_path(spec["app_root"], base)
        if not app.is_dir():
            raise FlatsatError(f"app_root is not a directory: {app}")
        board = spec["board"]
        selections = spec.get("selections", {})
        if (
            not isinstance(board, str)
            or not board
            or not isinstance(selections, dict)
            or any(
                not isinstance(k, str) or not isinstance(v, str)
                for k, v in selections.items()
            )
        ):
            raise FlatsatError(f"Build {name}: invalid board or selections")
        builds[name] = BuildRequest(app, board, selections)
    default = builds.get("default")
    tools = None
    west = raw.get("west")
    if west is not None or default is not None:
        west = (
            _resolve_path(west, base)
            if west is not None
            else default.app_root.parent / ".venv/bin/west"
        )
        openocd = (
            _resolve_path(raw["openocd"], base)
            if raw.get("openocd") is not None
            else Path.home() / ".local/bin/openocd"
        )
        tools = FirmwareTools(west, openocd)
    elif raw.get("openocd") is not None:
        raise FlatsatError("openocd requires west or a default build")
    target_data = raw.get(
        "targets", {"obc": {"board": default.board}} if default else {}
    )
    if not isinstance(target_data, dict):
        raise FlatsatError("targets must be a mapping")
    targets = {}
    for name, spec in target_data.items():
        if (
            not isinstance(name, str)
            or not isinstance(spec, dict)
            or set(spec) - {"board", "probe_serial"}
        ):
            raise FlatsatError("Each target accepts board and optional probe_serial")
        board = spec.get("board", default.board if default else None)
        serial = spec.get("probe_serial")
        if (
            not isinstance(board, str)
            or not board
            or (serial is not None and (not isinstance(serial, str) or not serial))
        ):
            raise FlatsatError(f"Target {name}: invalid board or probe_serial")
        targets[name] = Target(name, board, serial)
    experiment = raw.get("experiment", {})
    if not isinstance(experiment, dict):
        raise FlatsatError("experiment must be a mapping")
    return ControllerConfig(Installation(tools, targets), builds, experiment)


def _resolve_path(value: object, base: Path) -> Path:
    if not isinstance(value, (str, Path)):
        raise FlatsatError("Paths must be strings")
    path = Path(value).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()
