from __future__ import annotations

import argparse
import sys
from pathlib import Path

from flatsat_controller.config import load_config
from flatsat_controller.assembly import assemble_bench
from flatsat_controller.runner import run_experiment
from flatsat_controller.types import FlatsatError

from experiments import EXPERIMENTS


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="flatsat")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run a registered experiment")
    run_p.add_argument("experiment", help="Experiment name")
    run_p.add_argument("--config", type=Path, required=True, help="Bench config YAML")
    run_p.add_argument(
        "--runs-root",
        type=Path,
        default=Path("runs"),
        help="Directory for run artifacts (default: runs)",
    )

    run_p.add_argument(
        "--yes",
        action="store_true",
        help="Auto-confirm operator prompts",
    )

    args = parser.parse_args(argv)

    if args.command == "run":
        _cmd_run(args)


def _cmd_run(args: argparse.Namespace) -> None:
    name = args.experiment
    if name not in EXPERIMENTS:
        known = ", ".join(sorted(EXPERIMENTS))
        print(f"Unknown experiment {name!r}; known: {known}", file=sys.stderr)
        sys.exit(2)

    try:
        config = load_config(args.config)
        experiment = EXPERIMENTS[name]
        parameters = experiment.parameters(config)
    except FlatsatError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)

    result = run_experiment(
        name,
        experiment.run,
        parameters,
        bench_factory=lambda runtime: assemble_bench(
            runtime, config, auto_confirm=args.yes
        ),
        runs_root=args.runs_root.resolve(),
    )

    print(result.run_dir)
    for error in result.cleanup_errors:
        print(f"cleanup error: {error}", file=sys.stderr)
    if result.status == "interrupted":
        print("status: interrupted", file=sys.stderr)
        sys.exit(130)
    if result.status == "failed":
        print("status: failed", file=sys.stderr)
        for reason in result.reasons:
            print(reason, file=sys.stderr)
        sys.exit(1)
    if result.verdict is not None:
        print(f"verdict: {result.verdict}")
        for reason in result.reasons:
            print(reason)
        if result.verdict in ("fail", "invalid"):
            sys.exit(1)
    sys.exit(0)
