# FlatSat controller

Workspace entry point for the PEROVSAT FlatSat/HITL controller, Basilisk
simulation, and emulated devices.

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

The host `.venv` contains Zephyr tools and eventual controller/daemon Python
dependencies. Basilisk uses its own Docker image and container venv, built through
its existing Makefile. Setup runs `make smoke` to compile/import the plugins;
it does not enter a container shell or run an experiment.

Dependencies are pinned in [west.yml](west.yml). The MPU6050 repository is cloned
but currently has no application code. The daemon will be added once its
repository is available. Git authentication for all manifest repositories must
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

Activate the host venv and work from the workspace root. Until the emulator has
an application, verify the toolchain with Zephyr blinky:

```sh
west build -b rpi_pico2/rp2350a/m33 -d build/pico2-smoke \
  zephyr/samples/basic/blinky
```

Hold BOOTSEL while connecting USB, then use the UF2 runner:

```sh
west flash -d build/pico2-smoke -r uf2
```

Alternatively copy `build/pico2-smoke/zephyr/zephyr.uf2` onto the mounted RP2350
BOOTSEL drive. Once the emulator application exists at its repository root:

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
