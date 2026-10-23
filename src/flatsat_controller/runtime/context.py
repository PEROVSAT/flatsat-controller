from __future__ import annotations
import json
from dataclasses import fields, is_dataclass
from collections.abc import Callable, Mapping
import sys
import time
from pathlib import Path
from typing import Any, Protocol
from flatsat_controller.types import FlatsatError


class Executor(Protocol):
    def run(
        self, argv: list[str], *, cwd: Path, env: dict[str, str], log_path: Path
    ) -> int: ...


class ProcessOwner(Executor, Protocol):
    """Execution and cleanup needed by a run, including test doubles."""

    def close(self) -> list[str]: ...


class Recording(Protocol):
    def allocate_operation(self, label: str) -> Path: ...
    def record(self, record: dict[str, Any]) -> None: ...


class RunContext:
    def __init__(self, run_dir: Path, processes: ProcessOwner) -> None:
        self.run_dir = run_dir
        self.processes = processes
        self._operation = 0

    def allocate_operation(self, label: str) -> Path:
        self._operation += 1
        path = self.run_dir / "operations" / f"{self._operation:04d}-{label}"
        path.mkdir(parents=True, exist_ok=False)
        return path

    def record(self, record: dict[str, Any]) -> None:
        with (self.run_dir / "actions.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps({"monotonic_ns": time.monotonic_ns(), **record}) + "\n"
            )

    def write_metadata(self, name: str, value: object) -> None:
        (self.run_dir / f"{name}.json").write_text(
            json.dumps(_json_value(value), indent=2) + "\n", encoding="utf-8"
        )

    def close(self) -> list[str]:
        return self.processes.close()


class Operator:
    def __init__(
        self,
        recording: Recording,
        *,
        auto_confirm: bool = False,
        input_fn: Callable[[str], str] | None = None,
    ) -> None:
        self._recording = recording
        self._auto_confirm = auto_confirm
        self._input_fn = input_fn

    def confirm(self, message: str) -> None:
        self._recording.record({"action": "prompt", "message": message})
        if self._auto_confirm:
            return
        if self._input_fn is not None:
            self._input_fn(message)
            return
        if not sys.stdin.isatty():
            raise FlatsatError("Prompt requires a TTY or pass --yes to auto-confirm")
        input(message)


def _json_value(value: object) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value
