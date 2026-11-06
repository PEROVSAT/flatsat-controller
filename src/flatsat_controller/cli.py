from __future__ import annotations

import argparse
import sys
from pathlib import Path

from flatsat_controller.assembly import assemble_bench
from flatsat_controller.config import check_config, find_workspace, load_config
from flatsat_controller.runner import run_experiment
from flatsat_controller.types import FlatsatError

from experiments import EXPERIMENTS


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="flatsat")
    sub = parser.add_subparsers(dest="command", required=True)

    config_p = sub.add_parser("config", help="Create or check workspace bench configuration")
    config_sub = config_p.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("init", help="Create workspace-root bench.yml")
    config_sub.add_parser("check", help="Check configured tools and connected devices")

    run_p = sub.add_parser("run", help="Run a registered experiment")
    run_p.add_argument("experiment", help="Experiment name")
    run_p.add_argument("--runs-root", type=Path, default=None,
                       help="Override the experiment's default runs directory")
    run_p.add_argument("--yes", action="store_true", help="Auto-confirm operator prompts")
    args = parser.parse_args(argv)

    if args.command == "config":
        if args.config_command == "init":
            _cmd_config_init()
        else:
            _cmd_config_check()
    elif args.command == "run":
        _cmd_run(args)


_TEMPLATE = """# FlatSat workspace bench configuration
# Fill in only the hardware that is currently connected.
tools:
  # Null uses the default at ~/.local/bin/openocd.
  openocd: null

flight:
  # Example app: ../perovsat-workspace/perovsat-app
  app_root: null
  # Null uses app_root's parent .venv/bin/west.
  west: null

devices:
  imu:
    probe_serial: null
    uart: null
    address: 1
    baud: 115200
  obc:
    probe_serial: null

"""


def _config_path() -> Path:
    return find_workspace() / "bench.yml"


def _cmd_config_init() -> None:
    try:
        path = _config_path()
        with path.open("x", encoding="utf-8") as stream:
            stream.write(_TEMPLATE)
    except FileExistsError:
        print(f"Config already exists; left unchanged: {path}", file=sys.stderr)
        raise SystemExit(1)
    except (OSError, FlatsatError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
    print(path)


def _cmd_config_check() -> None:
    try:
        config = load_config(_config_path())
    except FlatsatError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
    errors = check_config(config)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        raise SystemExit(1)
    print("Bench configuration is accessible.")


def _cmd_run(args: argparse.Namespace) -> None:
    name = args.experiment
    if name not in EXPERIMENTS:
        known = ", ".join(sorted(EXPERIMENTS))
        print(f"Unknown experiment {name!r}; known: {known}", file=sys.stderr)
        raise SystemExit(2)
    try:
        workspace = find_workspace()
        config = load_config(workspace / "bench.yml")
        experiment = EXPERIMENTS[name]
        parameters = experiment.parameters(config)
        directory = experiment.directory
        runs_root = (args.runs_root if args.runs_root is not None else
                     workspace / "flatsat-controller" / "experiments" / directory / "runs")
    except FlatsatError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)

    result = run_experiment(
        name, experiment.run, parameters,
        bench_factory=lambda runtime: assemble_bench(runtime, config, auto_confirm=args.yes),
        runs_root=runs_root.resolve(),
    )
    print(result.run_dir)
    for error in result.cleanup_errors:
        print(f"cleanup error: {error}", file=sys.stderr)
    if result.status == "interrupted":
        print("status: interrupted", file=sys.stderr)
        raise SystemExit(130)
    if result.status == "failed":
        print("status: failed", file=sys.stderr)
        for reason in result.reasons:
            print(reason, file=sys.stderr)
        raise SystemExit(1)
    if result.verdict is not None:
        print(f"verdict: {result.verdict}")
        for reason in result.reasons:
            print(reason)
        if result.verdict in ("fail", "invalid"):
            raise SystemExit(1)
    raise SystemExit(0)
