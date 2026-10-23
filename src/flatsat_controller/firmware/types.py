from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Mapping
from types import MappingProxyType


@dataclass(frozen=True)
class FirmwareTools:
    west: Path
    openocd: Path = field(default_factory=lambda: Path.home() / ".local/bin/openocd")


@dataclass(frozen=True)
class Target:
    name: str
    board: str
    probe_serial: str | None = None


@dataclass(frozen=True)
class BuildRequest:
    app_root: Path
    board: str
    selections: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "selections", MappingProxyType(dict(self.selections)))


@dataclass(frozen=True)
class FirmwareArtifact:
    image_path: Path
    build_dir: Path
    board: str
    selections: Mapping[str, str]
    config_path: Path
    app_root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "selections", MappingProxyType(dict(self.selections)))
