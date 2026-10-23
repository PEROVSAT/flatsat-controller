# FlatSat controller implementation

The current controller builds and flashes firmware and records manual operator
confirmations. Experiments are synchronous Python functions with explicit
registration in `experiments/__init__.py`. Bridge, observations, radio decoding,
and Basilisk integration remain future work; no placeholder capabilities exist.

## Boundaries

`Bench` is a typed collection: `firmware`, `targets`, and `operator`. It owns no
workflow, resource lifecycle, subprocess execution, or experiment policy. A bench
can contain only operator capability; firmware and targets are optional.

`runtime/` owns run folders, numbered operation folders, action recording,
parameter snapshots, prompts, subprocess lifetime, and final results. It has no
knowledge of Zephyr, dbuild, OpenOCD, applications, or radio. The public
`runner.run_experiment` accepts experiment parameters and a bench factory. It
creates a runtime, invokes the factory and experiment inside the cleanup scope,
then writes the result, preserving cleanup errors alongside the original error.

`firmware/` owns BuildRequest, FirmwareArtifact, tools and logical target types,
dbuild generation and commands, OpenOCD flashing, firmware failure diagnostics,
and per-build source/configuration metadata. Firmware receives an executor and
recording interface directly; it never receives Bench or an experiment. Public
mock selection policy is an explicit preset in `firmware/presets.py`.

`assembly.py` is the single installation composition point. It wires runtime
services into capabilities, records installation metadata, and returns Bench.
`config.py` loads installation settings, named build requests, and an unparsed
experiment parameter mapping. Each explicitly registered experiment supplies its
own parameter parser and typed parameters; new experiments do not change the
shared config loader. An operator-only experiment needs neither app nor tools.

## Experiment API

```python
def run(bench, parameters):
    artifact = bench.firmware.build(parameters.build)
    bench.operator.confirm("Check the wiring, then continue.")
    bench.firmware.flash(bench.targets[parameters.target], artifact)
```

Build requests identify their own app directory, board, and immutable selection
snapshot. Repeated builds, including builds from different applications, receive
independent numbered operation directories. Flash uses the artifact's application
as its working directory and checks target/artifact board compatibility.

Logical targets identify a board and optionally a probe serial. Explicit serials
use OpenOCD `adapter serial` before initialization via west's `--cmd-pre-init`.
Serials accept letters, digits, `_`, `.`, `:`, and `-`; other values are rejected
before a flashing subprocess starts. Omitting serial preserves the existing
single-probe runner default. A target cannot ensure physical wiring is correct.

## Configuration

One YAML file still carries distinct installation, build, and experiment data:

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
    board: rpi_pico2/rp2350a/m33
experiment:
  build: nominal
  target: obc
  flash_mode: pico2
  console_mode: logs
```

Paths resolve relative to the YAML file. Existing top-level `app_root`, `board`,
`west`, and `openocd` config remains accepted: it becomes build `default` and,
when targets are absent, target `obc`. Missing west for that default build uses
the existing flight checkout's `.venv/bin/west` convention. Empty configuration
`{}` is valid for experiments requiring only operator capability.

Public-mock parameters select a named build and target, then choose device modes
through the explicit preset. Generic build execution never imposes that policy.
The example experiment retains its historical CONSOLE=logs and FLASH=pico2.

## Evidence and cleanup

A run contains `installation.json`, `parameters.json`, `actions.jsonl`, and
`result.json`. Each build operation contains `dbuild.yml`, `build.json`,
`build.log`, and its build output; flash operations contain `flash.log`. Build
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
