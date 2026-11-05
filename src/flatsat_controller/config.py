from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Mapping
import yaml
from flatsat_controller.types import FlatsatError
from flatsat_controller.firmware import FirmwareTools, Target, BuildRequest, validate_probe_serial
from flatsat_controller.simulation import SimulationLinkConfig
from flatsat_controller.firmware.types import PICO2_BOARD
from hitl_bridge.bridge import BridgeConfig, load_config as load_bridge_config


@dataclass(frozen=True)
class Installation:
    tools: FirmwareTools | None = None
    targets: Mapping[str, Target] = field(default_factory=dict)


@dataclass(frozen=True)
class ControllerConfig:
    installation: Installation
    builds: Mapping[str, BuildRequest] = field(default_factory=dict)
    experiment: Mapping[str, object] = field(default_factory=dict)
    simulation_link: SimulationLinkConfig | None = None
    bridge_config: BridgeConfig | None = None


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
        "simulation_link",
        "bridge_config",
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
            set(spec) - {"app_root", "board", "selections", "kind", "west"}
            or "app_root" not in spec
        ):
            raise FlatsatError(
                f"Build {name}: expected app_root, optional board, kind, west, selections"
            )
        app = _resolve_path(spec["app_root"], base)
        if not app.is_dir():
            raise FlatsatError(f"app_root is not a directory: {app}")
        board = spec.get("board", PICO2_BOARD)
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
        kind = spec.get("kind", "dbuild")
        if not isinstance(kind, str) or kind not in {"build", "dbuild"}:
            raise FlatsatError(f"Build {name}: kind must be build or dbuild")
        if board == "rpi_pico2":
            board = PICO2_BOARD
        if board != PICO2_BOARD:
            raise FlatsatError(f"Build {name}: only Pico 2 board {PICO2_BOARD} is supported")
        build_west = (
            _resolve_path(spec["west"], base) if spec.get("west") is not None else None
        )
        builds[name] = BuildRequest(app, board, selections, kind, build_west)
    default = builds.get("default")
    tools = None
    west = raw.get("west")
    fallback_west = _resolve_path(west, base) if west is not None else None
    if fallback_west is None and default is not None:
        fallback_west = default.west or default.app_root.parent / ".venv/bin/west"
    if builds and fallback_west is None and any(request.west is None for request in builds.values()):
        raise FlatsatError(
            "Each named build needs west when no top-level west or builds.default is configured"
        )
    if fallback_west is not None or builds:
        openocd = (
            _resolve_path(raw["openocd"], base)
            if raw.get("openocd") is not None
            else Path.home() / ".local/bin/openocd"
        )
        tools = FirmwareTools(fallback_west, openocd)
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
        board = spec.get("board", default.board if default else PICO2_BOARD)
        serial = spec.get("probe_serial")
        if (
            not isinstance(board, str)
            or not board
            or (serial is not None and not isinstance(serial, str))
        ):
            raise FlatsatError(f"Target {name}: invalid board or probe_serial")
        validate_probe_serial(serial)
        if board == "rpi_pico2":
            board = PICO2_BOARD
        if board != PICO2_BOARD:
            raise FlatsatError(f"Target {name}: only Pico 2 board {PICO2_BOARD} is supported")
        targets[name] = Target(name, board, serial)
    if len(targets) > 1:
        serials = [target.probe_serial for target in targets.values()]
        if any(serial is None for serial in serials) or len(set(serials)) != len(serials):
            raise FlatsatError("Multiple targets require distinct explicit probe_serial values")
    experiment = raw.get("experiment", {})
    if not isinstance(experiment, dict):
        raise FlatsatError("experiment must be a mapping")
    link_data = raw.get("simulation_link")
    link = None
    if link_data is not None:
        if not isinstance(link_data, dict):
            raise FlatsatError("simulation_link must be a mapping")
        try:
            link = SimulationLinkConfig.from_mapping(link_data)
        except ValueError as exc:
            raise FlatsatError(f"Invalid simulation_link: {exc}") from exc
    bridge_data = raw.get("bridge_config")
    bridge = None
    if bridge_data is not None:
        bridge_path = _resolve_path(bridge_data, base)
        try:
            bridge = load_bridge_config(bridge_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise FlatsatError(f"Invalid bridge_config: {exc}") from exc
    return ControllerConfig(Installation(tools, targets), builds, experiment, link, bridge)


def _resolve_path(value: object, base: Path) -> Path:
    if not isinstance(value, (str, Path)):
        raise FlatsatError("Paths must be strings")
    path = Path(value).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()
