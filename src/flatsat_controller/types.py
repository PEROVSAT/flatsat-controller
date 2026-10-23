from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


class FlatsatError(Exception):
    """Expected operational failure in the FlatSat controller."""


@dataclass(frozen=True)
class Verdict:
    outcome: Literal["pass", "fail", "invalid"]
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExperimentResult:
    name: str
    status: Literal["completed", "failed", "interrupted"]
    verdict: Literal["pass", "fail", "invalid"] | None
    reasons: tuple[str, ...]
    run_dir: Path
    cleanup_errors: tuple[str, ...]
