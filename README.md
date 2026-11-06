# FlatSat controller

Python controller and experiment runner for the PEROVSAT FlatSat/HITL workspace.
The controller is installed in the workspace's shared `.venv`. Run commands from
the workspace root so they find `bench.yml` and write run records in the expected
place.

## Initialize and check a bench

Initialize a local bench file from the workspace root, then edit it for the
connected equipment with these commands.

```sh
flatsat config init
flatsat config check
```

`config init` creates only `bench.yml` and refuses to overwrite an existing file.
The starter template leaves device identities blank. `config check` validates
configured tool and app paths, UART devices, and probe identities. Blank optional
fields are skipped, so a passing check does not mean every experiment has its
required inputs. Experiment preflight reports missing required values. The check
does not build or flash firmware. Paths in `bench.yml` are relative to the
workspace root. The file is local configuration and should not be committed.

For flashing, connect a CMSIS-DAP debug probe and install an RP2350-capable
OpenOCD separately. It defaults to `~/.local/bin/openocd`. Set
`tools.openocd` to use a different path. Set `flight.app_root` for OBC builds.
`flight.west` can override the OBC `west` executable. IMU settings are
`devices.imu.probe_serial`, `uart`, `address`, and `baud`. OBC settings include
`devices.obc.probe_serial`. The Pico 2 board is implicit, and device names are
`imu` and `obc`.

## Run an experiment

```sh
flatsat run utc-sync
flatsat run imu-update
```

The CLI works from either the workspace root or `flatsat-controller/`. Each
command loads the workspace-root `bench.yml`. Experiment parameters live with
the Python implementation. Operator details are in the [UTC sync](experiments/utc_sync/experiment.md)
and [IMU update](experiments/imu_update/experiment.md) notes. Runs are
timestamped under each package's `runs/` directory. Inspect the summary and
captured artifacts there.

`utc-sync` uses the IMU and needs no OBC. Before flashing, `imu-update` checks
the required bench configuration and tool paths for the IMU and OBC. It then
checks emulator readiness, UTC synchronization reported over the emulator UART,
a baseline sensor read, and the sample's completed 14-byte MPU6050 readback.
The readback must match the configured sample and preserve clean transaction
evidence. The receive window starts with the stimulus and is capped at 20
seconds.

For `imu-update`, connect OBC I²C0 GP4/SDA and GP5/SCL to the emulator's matching
pins, with common ground and shared external 2.2 kΩ pull-ups to 3.3 V. The
emulator responds at I²C address `0x68`. Ensure the OBC firmware uses this bus
and address. Stop other processes using the IMU UART before a run.

## Workspace setup and updates

Target hosts are macOS Apple Silicon and Ubuntu 26.04 x86-64. Install Python
3.12+, CMake 3.28+, Ninja, Git, gperf, dtc 1.4.6+, make, tar/xz, file, wget,
and Docker. Start Docker and ensure it is accessible to your user. On macOS,
install Docker Desktop and Apple Command Line Tools as well.

Install the macOS packages with Homebrew.

```sh
brew install git cmake ninja gperf dtc python@3.12 wget xz libmagic
```

Install these Ubuntu packages. Install and start Docker Engine separately.

```sh
sudo apt update
sudo apt install git cmake ninja-build gperf device-tree-compiler \
  python3-dev python3-venv make gcc g++ xz-utils file wget libmagic1
```

Clone this repository and initialize the surrounding workspace with these commands.

```sh
git clone git@github.com:PEROVSAT/flatsat-controller.git
cd flatsat-controller
./setup-workspace.sh
```

Setup places the checkout in a sibling `flatsat-workspace`, installs the host
virtual environment and Zephyr dependencies, prepares the Zephyr SDK, and runs a
Basilisk plugin smoke check. Manifest revisions are pinned in [west.yml](west.yml).
Git authentication for all manifest repositories must already work. Expect several
gigabytes of downloads. Use the printed `cd` command after setup. The shell that
launched it remains in its old location.

For subsequent synchronization, activate the workspace environment and run these commands.

```sh
source ../.venv/bin/activate
./update-workspace.sh --check
./update-workspace.sh
```

`--check` checks the recorded environment. The plain command synchronizes managed
repositories and dependencies to `west.yml`, but does not update this controller
checkout. Managed repositories must be clean and detached at west's last
`manifest-rev`. Save work before updating. Updates do not reset, clean, or stash
repositories. An interrupted setup/update retains downloaded files. Fix the
reported issue and rerun `update-workspace.sh`.

Run the Basilisk Makefile check from the workspace root.

```sh
make -C basilisk-simulation smoke IMAGE=perovsat-flatsat-bsk
```

Host tests use simulated probes, firmware commands, and in-memory bridge traffic
to check parsing, experiment sequencing, transaction evidence, and verdicts. They
do not require or flash physical hardware. Run them from the workspace root.

```sh
source .venv/bin/activate
pytest flatsat-controller/tests hitl-bridge/tests
```

See the experiment notes for bench-specific wiring and operation details.
