# FlatSat controller

Workspace entry point for the PEROVSAT FlatSat/HITL controller, Basilisk
simulation, and emulated devices.

## Controller

Use the controller API directly from Python. The configuration in
[`configs/pico2_bench.yml`](configs/pico2_bench.yml) preserves the configured
build paths and Pico probe serials.

```sh
source ../.venv/bin/activate
pip install -e .
python - <<'PYCODE'
from pathlib import Path

from flatsat_controller.assembly import assemble_bench
from flatsat_controller.config import load_config
from flatsat_controller.runtime import ProcessGroup, RunContext, create_run_dir

config = load_config(Path("configs/pico2_bench.yml"))
run_dir = create_run_dir(Path("runs"), "manual")
runtime = RunContext(run_dir, ProcessGroup())
try:
    bench = assemble_bench(runtime, config)
    artifact = bench.firmware.build(config.builds["emulator"])
    bench.firmware.flash(bench.targets["imu0"], artifact)
finally:
    cleanup_errors = runtime.close()
    if cleanup_errors:
        raise RuntimeError("; ".join(cleanup_errors))
PYCODE
```

`bench` exposes firmware, named targets, operator prompts, and the simulation
link. Use `dataclasses.replace` to customize a build request, and
`bench.firmware.build_and_flash(target, request)` when both operations should run
together. With `simulation_link` configured,
`bench.simulation_link.observe(...)` subscribes to Bridge frames and records
them to the run directory.

Experiments have a separate command. The existing public mock experiment selects
`CONSOLE=logs` and flashes using the configured OpenOCD executable.

```sh
flatsat run flash-public-mock --config configs/pico2_public_mock.yml
```

`bridge-utc` builds and flashes the emulator to the `imu0` board resolved from
the bridge inventory, then checks for a clock correction and two consecutive
UTC reports within tolerance. The inventory contains the board's explicit SWD
probe serial and its simulation UART; no OBC target is needed. Update the
example paths and identities for the bench, stop any other program using the IMU
UART, then run:

```sh
flatsat run bridge-utc --config configs/bridge_utc.yml
```

By default the check allows 20 seconds for two consecutive accepted UTC reports
within 10 ms, including a correction from the bridge. The run directory includes
accepted exchange evidence in `utc_evidence.json` and bridge output in
`bridge-utc.log`. Timeout and tolerance can be adjusted in the config's
`experiment` mapping.

Experiment artifacts land under `runs/`. Use `--runs-root` to choose a different
location. Each build and flash has a numbered directory under `operations/`
containing its logs and command metadata. Experiments also record their
parameters and result.

Run host tests with `python -m unittest discover -s tests -v`.

## Design

- [FlatSat specification](FlatSat.md): purpose and initial acceptance criteria.
- [System architecture](ARCHITECTURE.md): components and interfaces.
- [Controller design](CONTROLLER_DESIGN.md): Python bench API, experiments, and implementation tasks.

## First setup

Target hosts: macOS Apple Silicon and Ubuntu 26.04 x86-64. Install Python >=3.12,
CMake >=3.28, Ninja, Git, gperf, dtc >=1.4.6, make, tar/xz, file, wget, and Docker.
Docker must be running and accessible to your user. Setup prints prerequisite
guidance; it does not install system packages automatically.

macOS (also install Docker Desktop and Apple Command Line Tools):

```sh
brew install git cmake ninja gperf dtc python@3.12 wget xz libmagic
```

Ubuntu 26.04 (install/start Docker Engine separately):

```sh
sudo apt update
sudo apt install git cmake ninja-build gperf device-tree-compiler \
  python3-dev python3-venv make gcc g++ xz-utils file wget libmagic1
```

Then clone and run:

```sh
git clone git@github.com:PEROVSAT/flatsat-controller.git
cd flatsat-controller
./setup-workspace.sh
```

Setup creates `../flatsat-workspace`, moves this clone inside it, and downloads
dependencies into that workspace. If the clone already has a parent named
`flatsat-workspace`, that parent is used. An existing sibling destination is
rejected to avoid mixing workspaces. The checkout must be an ordinary clone named
`flatsat-controller`, with no controller-local `.venv` to invalidate by moving it.

The shell you launched setup from retains its previous working directory. After
setup, use the printed `cd` command for the new location. Expect several gigabytes
of downloads for Zephyr, its SDK, and the Basilisk Docker image.

The host `.venv` contains Zephyr tools and controller/bridge Python dependencies.
Basilisk uses its own Docker image and container venv, built through
its existing Makefile. Setup runs `make smoke` to compile/import the plugins;
it does not enter a container shell or run an experiment.

Dependencies are pinned in [west.yml](west.yml). The MPU6050 emulator and HITL
bridge are included as workspace projects. Git authentication for all manifest repositories must
already work; setup does not store credentials.

## Daily use and updates

From the relocated controller checkout:

```sh
source ../.venv/bin/activate
./update-workspace.sh --check
```

Setup is for initialization only and refuses repeat runs. To synchronize to the
current controller manifest and refresh dependencies:

```sh
./update-workspace.sh
```

The two root scripts contain the workspace functionality. Update loads shared
functions from setup without invoking its initialization entry point.

Updating does not pull the controller itself. Fetch/review controller changes
first if desired. Managed repositories must be clean and detached at west's last
`manifest-rev`; updates stop for branches, local changes, or extra commits. Save
your work before detaching. No automatic reset, clean, or stash is performed.

An interrupted setup/update retains downloaded files. Fix the reported error and
run `update-workspace.sh` from the new controller location. If the host venv is
corrupt, move it aside manually so update can recreate it. A process lock lives
in `../.flatsat/lock`; remove it only after confirming no workspace operation is
running. Ownership records contain absolute paths: moving an initialized
workspace/venv afterward is not supported.

Completion and resolved revisions are recorded in
`../.flatsat/setup-complete.json`; failed updates remove the completion record.
The environment checker validates the recorded manifest, revisions, SDK, and
host Python dependencies. Docker plugin readiness is checked by `make smoke`.

If another Zephyr workspace is active, deactivate its venv and unset
`ZEPHYR_BASE`. Select a host interpreter with `FLATSAT_PYTHON=/path/to/python`.
The SDK defaults to `~/zephyr-sdk-<Zephyr SDK_VERSION>`. To use another location,
set `ZEPHYR_SDK_INSTALL_DIR` to an SDK of that exact version with the ARM toolchain.
Existing incompatible/incomplete SDKs are reported and retained.

## Basilisk

From the workspace root:

```sh
make -C basilisk-simulation smoke IMAGE=perovsat-flatsat-bsk
make -C basilisk-simulation shell IMAGE=perovsat-flatsat-bsk
make -C basilisk-simulation run IMAGE=perovsat-flatsat-bsk \
  SCRIPT=experiments/detumble/run.py
```

Use the FlatSat image tag consistently. Its Makefile rebuilds when the Dockerfile
changes or the image is missing. Dependencies inside the image remain owned by
the Basilisk repository; source commit pins are not a full package/image lock.

## Pico 2 build and flash

Activate the host venv and work from the workspace root. To verify the toolchain
independently of the emulator firmware, build Zephyr blinky:

```sh
west build -b rpi_pico2/rp2350a/m33 -d build/pico2-smoke \
  zephyr/samples/basic/blinky
```

Hold BOOTSEL while connecting USB, then use the UF2 runner:

```sh
west flash -d build/pico2-smoke -r uf2
```

Alternatively copy `build/pico2-smoke/zephyr/zephyr.uf2` onto the mounted RP2350
BOOTSEL drive. To build the emulator application from its repository root:

```sh
west build -b rpi_pico2/rp2350a/m33 -d build/mpu6050 emulated-devices/mpu6050
west flash -d build/mpu6050 -r uf2
```

With the debug probe attached, use an RP2350-capable OpenOCD installation. The
pinned Zephyr board documentation calls for Raspberry Pi's OpenOCD fork; do not
assume a generic packaged OpenOCD has the required target support.

```sh
west flash -d build/mpu6050 -r openocd --openocd /path/to/openocd
west debug -d build/mpu6050 -r openocd --openocd /path/to/openocd
```

The board defaults to a CMSIS-DAP adapter. Other probes may need
`-DRPI_PICO_DEBUG_ADAPTER=<adapter>` when configuring the build. OpenOCD is not
required for setup or UF2.

## Script validation

Check shell syntax without running setup:

```sh
bash -n setup-workspace.sh update-workspace.sh
```

The bootstrap west version is pinned directly in `setup-workspace.sh`.

Full host installation, Docker builds, and physical flashing require their
respective prerequisites. See [SETUP_PLAN.md](SETUP_PLAN.md) for the implementation
scope and remaining daemon/probe inputs.

# MPU6050 simulation A/B check

The registered `mpu6050-ab` experiment sends two SI-unit samples through the
bridge, polls device status, then checks complete 14-byte MPU6050 reads logged
by the normal OBC. It compares the returned bus bytes and sample sequence with
the configured accel and gyro ranges. A mismatch, capture loss, flagged or
truncated transaction, or timeout invalidates the run. This is a sensor-bus
readback check and does not provide a flight telemetry verdict.

Start the bridge with the emulator on its serial link. For example, save this
as the bridge config and replace the serial port with the emulator's port:

```yaml
listen: 127.0.0.1:4242
devices:
  - name: imu0
    address: 1
    port: /dev/cu.usbmodem102 # replace with the emulator's serial port
```

The OBC is not a bridge device; it connects electrically to the emulator's
MPU6050 I²C pins. Before running the experiment, have the OBC connected and
running its MPU6050 application, then run from the workspace:

```sh
flatsat run mpu6050-ab --config flatsat-controller/configs/mpu6050_ab.yml
```

The example expects the bridge TCP listener at `127.0.0.1:4242`, controller
source address 253, and emulator device address 1. Edit `simulation_link` for
the listener and addresses in use. Edit `experiment` to select `accel_range_g`
(2, 4, 8, or 16), `gyro_range_dps` (250, 500, 1000, or 2000), timeout, and the
two stimuli. Acceleration is m/s², angular rate is rad/s, and temperature is
°C. The timeout applies independently to adoption and each sensor read.

Wire the OBC I²C0 GP4/SDA and GP5/SCL pins to the emulator's matching pins, with
common ground and shared external 2.2 kΩ pull-ups to 3.3 V. The emulated MPU6050
responds at I²C address `0x68`. Ensure the OBC firmware is configured for this
bus and address. The experiment does not flash or configure the OBC.

Original frame payloads are captured as hex and base64 in
`simulation-frames.jsonl` in the run directory, including frames received while
status is being polled. No physical device is required for host tests. The
current workspace does not have the OBC attached; the experiment is ready to
run when the OBC and bridge are connected.
