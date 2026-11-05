from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

import yaml

from flatsat_controller.types import FlatsatError


def load_dbuild(app_root: Path) -> dict:
    path = app_root / "dbuild.yml"
    if not path.is_file():
        raise FlatsatError(f"dbuild.yml not found: {path}")
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise FlatsatError(f"dbuild.yml must be a mapping: {path}")
    if "devices" not in data or not isinstance(data["devices"], dict):
        raise FlatsatError(f"dbuild.yml missing 'devices' mapping: {path}")
    return data


def write_dbuild(
    source: dict,
    selections: Mapping[str, str],
    path: Path,
) -> None:
    doc = dict(source)
    doc["selections"] = dict(selections)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, sort_keys=False, default_flow_style=False)


def dbuild_argv(
    west: Path,
    *,
    board: str,
    config: Path,
    build_dir: Path,
) -> list[str]:
    return [
        str(west.resolve()),
        "dbuild",
        "-b",
        board,
        "--config",
        str(config.resolve()),
        "-d",
        str(build_dir.resolve()),
        "-p",
        "always",
    ]


def build_argv(west: Path, *, board: str, app_root: Path, build_dir: Path) -> list[str]:
    return [
        str(west.resolve()), "build", "-b", board, "-d",
        str(build_dir.resolve()), str(app_root.resolve()),
    ]


def flash_argv(
    west: Path, *, build_dir: Path, openocd: Path, probe_serial: str | None = None
) -> list[str]:
    argv = [
        str(west.resolve()),
        "flash",
        "--no-rebuild",
        "-d",
        str(build_dir.resolve()),
        "-r",
        "openocd",
        "--openocd",
        str(openocd.resolve()),
    ]

    if probe_serial is not None:
        validate_probe_serial(probe_serial)
        argv.extend(["--cmd-pre-init", f"adapter serial {probe_serial}"])
    return argv


def validate_probe_serial(probe_serial: str | None) -> None:
    if probe_serial is not None and (
        not isinstance(probe_serial, str)
        or not re.fullmatch(r"[A-Za-z0-9_.:-]+", probe_serial)
    ):
        raise FlatsatError("probe_serial must contain only letters, digits, _, ., :, or -")


def flight_env(west: Path) -> dict[str, str]:
    # Child west is resolved from PATH; prepend the flight workspace venv bin.
    env = dict(os.environ)
    env.pop("ZEPHYR_BASE", None)
    west_dir = west.resolve().parent
    env["PATH"] = f"{west_dir}{os.pathsep}{env.get('PATH', '')}"
    venv_root = west_dir.parent
    if (venv_root / "pyvenv.cfg").is_file():
        env["VIRTUAL_ENV"] = str(venv_root)
    return env
