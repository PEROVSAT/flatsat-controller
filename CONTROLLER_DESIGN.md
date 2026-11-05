# FlatSat controller implementation

Experiments are synchronous Python functions with explicit registration in
`experiments/__init__.py`. Manual workflows assemble the same bench API directly
in Python and manage a `RunContext` for operation records and cleanup.

## Boundaries

`Bench` exposes firmware, named targets, operator prompts, and the simulation
link. Assembly provides the capabilities configured for the installation.

`runtime/` owns run folders, numbered operation folders, action recording,
parameter snapshots, prompts, subprocess lifetime, and final results. It has no
knowledge of Zephyr, dbuild, OpenOCD, applications, or radio. The public
`runner.run_experiment` accepts experiment parameters and a bench factory. It
creates a runtime, invokes the factory and experiment inside the cleanup scope,
then writes the result, preserving cleanup errors alongside the original error.

`firmware/` owns build requests, artifacts, and targets. It builds with `west
build` or `west dbuild` and flashes through the Pico 2 OpenOCD runner. It
records command, source, and configuration metadata through runtime interfaces.
Public mock selection policy lives in `firmware/presets.py`.

`assembly.py` is the single installation composition point. It wires runtime
services into capabilities, records installation metadata, and returns Bench.
`config.py` loads installation settings, named build requests, and an unparsed
experiment parameter mapping. Each explicitly registered experiment supplies its
own parameter parser and typed parameters; new experiments do not change the
shared config loader. An operator-only experiment needs neither app nor tools.

## Firmware API

```python
def run(bench, parameters):
    artifact = bench.firmware.build(parameters.build)
    bench.operator.confirm("Check the wiring, then continue.")
    bench.firmware.flash(bench.targets[parameters.target], artifact)
```

Build requests specify an app directory and a build kind. A dbuild request can
override the app's device selections. Repeated builds receive independent
numbered operation directories. Flash programs the existing artifact using its
recorded app directory and `west` executable. Use
`firmware.build_and_flash(target, request)` when both steps should run together.

Logical target names identify physical Pico 2 boards by the serial numbers of
their debug probes. West passes `adapter serial` to OpenOCD before initialization
via `--cmd-pre-init`. Multiple targets require distinct explicit serials.
Omitting a serial preserves the existing single-probe runner default.

## Configuration

One YAML file carries installation settings, named builds, and optional
experiment parameters.

```yaml
west: /flight/.venv/bin/west
openocd: /home/operator/.local/bin/openocd
targets:
  obc:
    board: rpi_pico2/rp2350a/m33
    probe_serial: E661410403392A28
builds:
  nominal:
    app_root: /flight/perovsat-app
    kind: dbuild
    selections:
      CONSOLE: logs
experiment:
  build: nominal
  target: obc
  flash_mode: pico2
  console_mode: logs
```

Paths resolve relative to the YAML file. Existing top-level `app_root`, `board`,
`west`, and `openocd` config remains accepted. It becomes build `default` and,
when targets are absent, target `obc`. Missing west for that default build uses
the existing flight checkout's `.venv/bin/west` convention. Empty configuration
`{}` is valid for experiments requiring only operator capability.

Use `kind: build` for a plain Zephyr app. A build can specify its own `west`
path when the bench uses apps from different workspaces. Named builds need
that path when the config has neither a top-level `west` nor a default build.

Public-mock parameters select a named build and target, then choose device modes
through the explicit preset. Generic build execution never imposes that policy.
The example experiment retains its historical CONSOLE=logs and FLASH=pico2.

## Programmatic manual workflows

Load a bench config, allocate a run directory, create a `RunContext` with a
`ProcessGroup`, and pass both to `assemble_bench`. Use `try/finally` to call
`runtime.close()` so child processes and logs are cleaned up. The API supports
build, flash, operator prompts, and `SimulationLink.observe`; no registered
experiment is required for manual bring-up.

## Evidence and cleanup

A run contains `installation.json`, `parameters.json`, `actions.jsonl`, and
`result.json`. Each build operation contains `build.json`, `build.log`, and its
build output. Dbuild operations also capture `dbuild.yml`. Flash operations
contain `flash.json` and `flash.log`. Build
metadata includes app, board, selections, command, and git revision/dirty status.
Actions refer to operation paths and flash artifact/target identity. Interrupted
or failed command launches are recorded as errors before propagation.

ProcessGroup tracks processes launched by this run and closes their logs. Normal
completion, experiment failure, assembly failure, and KeyboardInterrupt all reach
cleanup. Each owned child gets a cleanup attempt even when another child times
out or raises an operating-system error, and its log is closed in all cases.
Cleanup failures make an otherwise completed execution failed, appear in the
CLI, and produce a nonzero exit. Existing failure/interruption status and the
experiment verdict/reasons are retained separately from cleanup errors. Cleanup
does not reset firmware or power settings. Execution status
(completed/failed/interrupted) is separate from a pass/fail/invalid verdict; a
manual bring-up run can complete with no verdict.

Future capabilities should accept these small runtime dependencies directly and
be wired in assembly. Implement their actual behavior before adding them to
Bench. No plugin registry, dependency injection framework, or fixed experiment
lifecycle is needed.
