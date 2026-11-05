from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Mapping
from types import MappingProxyType

PICO2_BOARD = "rpi_pico2/rp2350a/m33"


@dataclass(frozen=True)
class FirmwareTools:
    west: Path | None
    openocd: Path = field(default_factory=lambda: Path.home() / ".local/bin/openocd")


@dataclass(frozen=True)
class Target:
    name: str
    board: str
    probe_serial: str | None = None

    def __post_init__(self) -> None:
        if self.board == "rpi_pico2":
            object.__setattr__(self, "board", PICO2_BOARD)


@dataclass(frozen=True)
class BuildRequest:
    app_root: Path
    board: str = PICO2_BOARD
    selections: Mapping[str, str] = field(default_factory=dict)
    kind: str = "dbuild"
    west: Path | None = None

    def __post_init__(self) -> None:
        if self.board == "rpi_pico2":
            object.__setattr__(self, "board", PICO2_BOARD)
        object.__setattr__(self, "selections", MappingProxyType(dict(self.selections)))
        if not isinstance(self.kind, str) or self.kind not in {"build", "dbuild"}:
            raise ValueError("kind must be 'build' or 'dbuild'")


@dataclass(frozen=True)
class FirmwareArtifact:
    image_path: Path
    build_dir: Path
    board: str
    selections: Mapping[str, str]
    config_path: Path | None
    app_root: Path
    kind: str = "dbuild"
    west: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "selections", MappingProxyType(dict(self.selections)))
