from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypeVar

from flatsat_controller.runtime.context import ProcessOwner, RunContext
from flatsat_controller.runtime.processes import ProcessGroup
from flatsat_controller.types import ExperimentResult, FlatsatError, Verdict


BenchT = TypeVar("BenchT")
ParametersT = TypeVar("ParametersT")


def run_experiment(
    name: str,
    func: Callable[[BenchT, ParametersT], Verdict | None],
    parameters: ParametersT,
    *,
    runs_root: Path,
    processes: ProcessOwner | None = None,
    bench_factory: Callable[[RunContext], BenchT],
) -> ExperimentResult:
    run_dir = _allocate_run_dir(runs_root, name)
    run_dir.mkdir(parents=True, exist_ok=False)

    group = processes if processes is not None else ProcessGroup()
    runtime = RunContext(run_dir, group)

    status: Literal["completed", "failed", "interrupted"] = "completed"
    verdict: Literal["pass", "fail", "invalid"] | None = None
    reasons: tuple[str, ...] = ()
    cleanup_errors: tuple[str, ...] = ()

    try:
        runtime.write_metadata("parameters", parameters)
        bench = bench_factory(runtime)
        outcome = func(bench, parameters)
        if isinstance(outcome, Verdict):
            verdict = outcome.outcome
            reasons = outcome.reasons
        elif outcome is not None:
            status = "failed"
            reasons = (f"experiment returned unexpected value: {outcome!r}",)
    except KeyboardInterrupt:
        status = "interrupted"
    except FlatsatError as exc:
        # Execution failure (build/flash error), not a test FAIL verdict.
        status = "failed"
        reasons = (str(exc),)
    except Exception as exc:
        status = "failed"
        reasons = (str(exc),)
    finally:
        try:
            cleanup_errors = tuple(runtime.close())
        except Exception as exc:
            cleanup_errors = (str(exc),)

    if cleanup_errors and status == "completed":
        status = "failed"

    result = ExperimentResult(
        name=name,
        status=status,
        verdict=verdict,
        reasons=reasons,
        run_dir=run_dir,
        cleanup_errors=cleanup_errors,
    )
    _write_result(result)
    return result


def _allocate_run_dir(runs_root: Path, name: str) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = runs_root / f"{ts}-{name}"
    if not base.exists():
        return base
    for i in range(1, 10000):
        candidate = runs_root / f"{ts}-{name}-{i}"
        if not candidate.exists():
            return candidate
    raise FlatsatError(f"Could not allocate run directory under {runs_root}")


def _write_result(result: ExperimentResult) -> None:
    payload = {
        "name": result.name,
        "status": result.status,
        "verdict": result.verdict,
        "reasons": list(result.reasons),
        "run_dir": str(result.run_dir),
        "cleanup_errors": list(result.cleanup_errors),
    }
    with (result.run_dir / "result.json").open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
