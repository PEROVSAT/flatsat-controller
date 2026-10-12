#!/bin/bash
set -Eeuo pipefail
APP_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)

# Shared functions are also loaded by update-workspace.sh.
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

workspace_paths() {
    [[ $(basename "$APP_DIR") == flatsat-controller ]] || die "Keep the controller checkout named flatsat-controller."
    local parent
    parent=$(dirname "$APP_DIR")
    if [[ $(basename "$parent") == flatsat-workspace ]]; then
        WORKSPACE_DIR=$parent
    else
        WORKSPACE_DIR=$parent/flatsat-workspace
    fi
    STATE_DIR=$WORKSPACE_DIR/.flatsat
    WORKSPACE_PYTHON=$WORKSPACE_DIR/.venv/bin/python
}

refuse_existing_setup() {
    if [[ -e "$STATE_DIR/owner" || -e "$WORKSPACE_DIR/.west" ]]; then
        die "Workspace initialization already happened. Run $WORKSPACE_DIR/flatsat-controller/update-workspace.sh."
    fi
    if [[ $(dirname "$APP_DIR") != "$WORKSPACE_DIR" && -e "$WORKSPACE_DIR" ]]; then
        die "Destination already exists: $WORKSPACE_DIR. Setup will not merge into it."
    fi
    local ancestor
    ancestor=$(dirname "$APP_DIR")
    while [[ "$ancestor" != / ]]; do
        [[ ! -e "$ancestor/.west" ]] || die "The checkout is already inside another west workspace: $ancestor."
        ancestor=$(dirname "$ancestor")
    done
    [[ -d "$APP_DIR/.git" && ! -L "$APP_DIR/.git" ]] || die "Setup needs an ordinary Git clone, not a linked worktree."
    [[ ! -e "$APP_DIR/.venv" ]] || die "This checkout contains a .venv with absolute paths; relocate it yourself before setup."
}

require_owned_workspace() {
    [[ $(dirname "$APP_DIR") == "$WORKSPACE_DIR" && -f "$STATE_DIR/owner" ]] || die "No initialized FlatSat workspace here. Run ./setup-workspace.sh first."
    [[ $(cat "$STATE_DIR/owner") == "$APP_DIR" ]] || die "Workspace ownership differs from this checkout. Restore the original workspace location."
}

host_dependency_help() {
    if [[ $(uname -s) == Darwin ]]; then
        cat >&2 <<'EOF'
Install host prerequisites with Homebrew:
  brew install git cmake ninja gperf dtc python@3.12 wget xz libmagic
Install/start Docker Desktop and Apple Command Line Tools (xcode-select --install).
EOF
    else
        cat >&2 <<'EOF'
Install host prerequisites on Ubuntu 26.04:
  sudo apt update
  sudo apt install git cmake ninja-build gperf device-tree-compiler python3-dev python3-venv make gcc g++ xz-utils file wget libmagic1
Install Docker Engine using Docker's Ubuntu instructions; ensure your user can run docker info.
EOF
    fi
}

check_host() {
    local os arch tool missing=0
    os=$(uname -s)
    arch=$(uname -m)
    case "$os/$arch" in
        Darwin/arm64) ;;
        Linux/x86_64)
            [[ -f /etc/os-release ]] || die "Cannot identify the Linux distribution."
            # shellcheck disable=SC1091
            source /etc/os-release
            [[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 26.04 ]] || die "Supported Linux host: Ubuntu 26.04 x86-64."
            ;;
        *) die "Supported hosts: macOS Apple Silicon and Ubuntu 26.04 x86-64 ($os/$arch detected)." ;;
    esac
    HOST_PYTHON=${FLATSAT_PYTHON:-}
    if [[ -z "$HOST_PYTHON" ]]; then
        if command -v python3.12 >/dev/null 2>&1; then
            HOST_PYTHON=$(command -v python3.12)
        else
            HOST_PYTHON=$(command -v python3 || true)
        fi
    fi
    for tool in git cmake ninja gperf dtc make cc c++ tar xz file wget docker; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            printf 'Missing tool: %s\n' "$tool" >&2
            missing=1
        fi
    done
    if [[ -z "$HOST_PYTHON" ]] || ! "$HOST_PYTHON" -c 'import sys; assert sys.version_info >= (3, 12)' 2>/dev/null; then
        echo "Python >=3.12 is required. Select it with FLATSAT_PYTHON=/path/to/python." >&2
        missing=1
    fi
    if [[ $missing == 1 ]]; then
        host_dependency_help
        die "Install the missing prerequisites, then retry."
    fi
    workspace_python "$HOST_PYTHON" host-check || {
        host_dependency_help
        die "Host tool version checks failed."
    }
    docker info >/dev/null 2>&1 || die "Docker is not running or accessible. Start Docker and retry."
}

check_environment_variables() {
    if [[ -n ${ZEPHYR_BASE:-} ]]; then
        [[ -d "$ZEPHYR_BASE" ]] || die "ZEPHYR_BASE points to a missing directory; unset it."
        [[ $(cd "$ZEPHYR_BASE" && pwd -P) == "$WORKSPACE_DIR/zephyr" ]] || die "ZEPHYR_BASE selects another checkout. Unset it before running this script."
    fi
    [[ -z ${ZEPHYR_TOOLCHAIN_VARIANT:-} || $ZEPHYR_TOOLCHAIN_VARIANT == zephyr ]] || die "Unset ZEPHYR_TOOLCHAIN_VARIANT or select zephyr for this workspace."
    [[ -z ${VIRTUAL_ENV:-} || $VIRTUAL_ENV == "$WORKSPACE_DIR/.venv" ]] || die "Deactivate the other Python environment before setup/update."
}

wrap_checkout() {
    if [[ $(dirname "$APP_DIR") != "$WORKSPACE_DIR" ]]; then
        mkdir "$WORKSPACE_DIR"
        mv "$APP_DIR" "$WORKSPACE_DIR/flatsat-controller"
        APP_DIR=$WORKSPACE_DIR/flatsat-controller
    fi
    cd "$WORKSPACE_DIR" || die "Cannot enter workspace: $WORKSPACE_DIR."
    printf 'FlatSat workspace: %s\nController moved/located at: %s\n' "$WORKSPACE_DIR" "$APP_DIR"
}

acquire_lock() {
    mkdir -p "$STATE_DIR"
    mkdir "$STATE_DIR/lock" 2>/dev/null || die "A workspace operation is active. If it crashed, remove $STATE_DIR/lock after checking it has stopped."
    printf '%s\n' "$$" > "$STATE_DIR/lock/pid"
    cd "$WORKSPACE_DIR" || die "Cannot enter workspace: $WORKSPACE_DIR."
}

release_lock() {
    rm -f "$STATE_DIR/lock/pid"
    rmdir "$STATE_DIR/lock" 2>/dev/null || true
}

report_failure() {
    echo "Workspace operation failed; files have been retained." >&2
    printf 'Fix the reported error, then run: %s/update-workspace.sh\n' "$APP_DIR" >&2
}

prepare_python() {
    if [[ ! -e "$WORKSPACE_DIR/.venv" ]]; then
        "$HOST_PYTHON" -m venv "$WORKSPACE_DIR/.venv"
    fi
    [[ -x "$WORKSPACE_PYTHON" ]] || die "Workspace venv is incomplete. Repair .venv, then run update-workspace.sh."
    "$WORKSPACE_PYTHON" -c 'import sys; assert sys.version_info >= (3, 12); assert sys.prefix != sys.base_prefix'
    export PATH="$WORKSPACE_DIR/.venv/bin:$PATH"
    "$WORKSPACE_PYTHON" -m pip install "west==1.5.0"
}

initialize_west() {
    if [[ ! -e "$WORKSPACE_DIR/.west" ]]; then
        "$WORKSPACE_PYTHON" -m west init -l "$APP_DIR"
    fi
    workspace_python "$WORKSPACE_PYTHON" ownership "$WORKSPACE_DIR"
}

prepare_sdk() {
    local sdk_version sdk_dir
    sdk_version=$(cat "$WORKSPACE_DIR/zephyr/SDK_VERSION")
    if [[ -n ${ZEPHYR_SDK_INSTALL_DIR:-} ]]; then
        sdk_dir=$ZEPHYR_SDK_INSTALL_DIR
    elif [[ -f "$STATE_DIR/sdk-dir" ]]; then
        sdk_dir=$(cat "$STATE_DIR/sdk-dir")
    else
        sdk_dir=$HOME/zephyr-sdk-$sdk_version
    fi
    if [[ ! -e "$sdk_dir" ]]; then
        "$WORKSPACE_PYTHON" -m west sdk install --version "$sdk_version" --install-dir "$sdk_dir" --gnu-toolchains arm-zephyr-eabi
    fi
    workspace_python "$WORKSPACE_PYTHON" sdk-check "$WORKSPACE_DIR" "$sdk_dir"
    (cd "$sdk_dir" && ./setup.sh -c)
    printf '%s\n' "$sdk_dir" > "$STATE_DIR/sdk-dir"
}

refresh_workspace() {
    workspace_python "$WORKSPACE_PYTHON" guard "$WORKSPACE_DIR"
    "$WORKSPACE_PYTHON" -m west update
    "$WORKSPACE_PYTHON" -m west packages pip --install
    local component
    for component in "$APP_DIR" "$WORKSPACE_DIR/flatsat-daemon"; do
        if [[ -f "$component/pyproject.toml" || -f "$component/setup.py" ]]; then
            "$WORKSPACE_PYTHON" -m pip install -e "$component"
        elif [[ -f "$component/requirements.txt" ]]; then
            "$WORKSPACE_PYTHON" -m pip install -r "$component/requirements.txt"
        fi
    done
    "$WORKSPACE_PYTHON" -m pip check
    "$WORKSPACE_PYTHON" -m west zephyr-export
    prepare_sdk
    # Its Makefile builds the image if missing or the Dockerfile changed.
    make -C "$WORKSPACE_DIR/basilisk-simulation" smoke IMAGE=perovsat-flatsat-bsk
    workspace_python "$WORKSPACE_PYTHON" snapshot "$WORKSPACE_DIR"
    printf 'complete\n' > "$STATE_DIR/status"
    printf '\nFlatSat setup/update complete. In your terminal, run:\n'
    printf '  cd %q\n  source ../.venv/bin/activate\n' "$APP_DIR"
    echo "Build the Pico 2 smoke sample from the workspace root:"
    echo "  west build -b rpi_pico2/rp2350a/m33 -d build/pico2-smoke zephyr/samples/basic/blinky"
    echo "The MPU6050 repository is cloned; it is buildable once its application is added."
}

workspace_python() {
    local interpreter=$1
    shift
    "$interpreter" - "$@" <<'FLATSAT_PYTHON_HELPER'
import configparser
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone


def run(*args, cwd=None, check=True):
    return subprocess.run(args, cwd=cwd, check=check, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def git(repo, *args, check=True):
    return run("git", "-C", str(repo), *args, check=check)


def ownership(root):
    app = root / "flatsat-controller"
    owner = root / ".flatsat/owner"
    if not owner.is_file() or owner.read_text().strip() != str(app.resolve()):
        raise ValueError("Workspace is not owned by this controller checkout.")
    if (root / ".west").is_symlink():
        raise ValueError("West metadata must belong to this workspace, not a symlink.")
    config = configparser.ConfigParser()
    config.read(root / ".west/config")
    if config.get("manifest", "path", fallback=None) != "flatsat-controller":
        raise ValueError("West manifest.path must identify flatsat-controller.")
    if config.get("manifest", "file", fallback="west.yml") != "west.yml":
        raise ValueError("West must use this controller's west.yml.")


def managed_repositories(root):
    """Include removed manifest entries too, so local work is never overlooked."""
    for current, dirs, files in os.walk(root, followlinks=False):
        path = Path(current)
        dirs[:] = [d for d in dirs if d not in {
            "flatsat-controller", ".west", ".flatsat", ".venv", "build",
            "__pycache__", ".cache",
        }]
        for directory in dirs:
            if (path / directory).is_symlink():
                raise ValueError(f"Workspace source path is a symlink: {path / directory}. Updates require local checkouts.")
        if ".git" in dirs or ".git" in files:
            yield path
            dirs[:] = []


def guard(root):
    ownership(root)
    for repo in managed_repositories(root):
        label = repo.relative_to(root)
        if git(repo, "status", "--porcelain", "--untracked-files=all").stdout:
            raise ValueError(f"{label} has local changes. Commit/save them before updating.")
        if git(repo, "symbolic-ref", "--quiet", "HEAD", check=False).returncode == 0:
            raise ValueError(f"{label} is on a local branch. Save work and detach at manifest-rev before updating.")
        baseline = git(repo, "rev-parse", "--verify", "refs/heads/manifest-rev", check=False)
        if baseline.returncode != 0:
            raise ValueError(f"{label} has no west manifest-rev; setup will not adopt an unrelated checkout.")
        if git(repo, "rev-parse", "HEAD").stdout != baseline.stdout:
            raise ValueError(f"{label} has commits outside manifest-rev. Save work before updating.")


def check_sdk(root, sdk):
    version = (root / "zephyr/SDK_VERSION").read_text().strip()
    version_file = sdk / "sdk_version"
    # SDK 1.x stores GNU toolchains under gnu/.
    compiler = sdk / "gnu/arm-zephyr-eabi/bin/arm-zephyr-eabi-gcc"
    if not version_file.is_file() or version_file.read_text().strip() != version:
        raise ValueError(f"SDK must be version {version}: {sdk}. Select another directory; existing files are retained.")
    if not (sdk / "setup.sh").is_file() or not compiler.is_file():
        raise ValueError(f"SDK lacks setup.sh or the ARM compiler: {sdk}. Repair it or choose another directory.")
    run(str(compiler), "--version")
    print(f"Zephyr SDK {version}: {sdk}")


def host_check():
    for tool, args, minimum in (
        ("cmake", ["--version"], (3, 28, 0)),
        ("dtc", ["--version"], (1, 4, 6)),
    ):
        output = run(tool, *args).stdout
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", output)
        if not match or tuple(map(int, match.groups())) < minimum:
            raise ValueError(f"{tool} >= {'.'.join(map(str, minimum))} is required.")


def manifest_projects(root):
    from west.manifest import Manifest
    manifest = Manifest.from_topdir(topdir=str(root))
    return [p for p in manifest.projects if p.name != "manifest" and manifest.is_active(p)]


def snapshot(root):
    ownership(root)
    projects = []
    for project in manifest_projects(root):
        repo = root / project.path
        projects.append({"name": project.name, "path": project.path,
                         "revision": project.revision,
                         "commit": git(repo, "rev-parse", "HEAD").stdout.strip()})
    app = root / "flatsat-controller"
    record = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "controller_commit": git(app, "rev-parse", "HEAD").stdout.strip(),
        "controller_dirty": bool(git(app, "status", "--porcelain").stdout),
        "manifest_sha256": hashlib.sha256((app / "west.yml").read_bytes()).hexdigest(),
        "sdk_dir": (root / ".flatsat/sdk-dir").read_text().strip(),
        "projects": projects,
        "python_packages": json.loads(run(sys.executable, "-m", "pip", "list", "--format=json").stdout),
    }
    output = root / ".flatsat/setup-complete.json"
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n")
    temporary.replace(output)


def environment_check(root):
    ownership(root)
    record_path = root / ".flatsat/setup-complete.json"
    if not record_path.is_file():
        raise ValueError("Setup/update is incomplete. Run update-workspace.sh.")
    record = json.loads(record_path.read_text())
    app = root / "flatsat-controller"
    if hashlib.sha256((app / "west.yml").read_bytes()).hexdigest() != record["manifest_sha256"]:
        raise ValueError("Manifest changed since the last setup/update. Run update-workspace.sh.")
    for project in record["projects"]:
        actual = git(root / project["path"], "rev-parse", "HEAD").stdout.strip()
        if actual != project["commit"]:
            raise ValueError(f"{project['name']} changed revision since setup/update.")
    check_sdk(root, Path(record["sdk_dir"]))
    run(sys.executable, "-m", "pip", "check")
    print(f"Workspace environment checked: {root}")
    print("Docker runtime/plugin readiness: run make smoke in basilisk-simulation.")


def main():
    action = sys.argv[1]
    if action == "host-check":
        host_check()
        return
    root = Path(sys.argv[2]).resolve()
    if action == "ownership":
        ownership(root)
    elif action == "guard":
        guard(root)
    elif action == "sdk-check":
        check_sdk(root, Path(sys.argv[3]).resolve())
    elif action == "snapshot":
        snapshot(root)
    elif action == "check":
        environment_check(root)
    else:
        raise ValueError(f"Unknown action: {action}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr.strip(), file=sys.stderr)
        sys.exit(1)
FLATSAT_PYTHON_HELPER
}

setup_main() {
    if [[ ${1:-} == --help && $# == 1 ]]; then
        echo "Usage: ./setup-workspace.sh"
        echo "Wrap this clone in flatsat-workspace and install Zephyr/Basilisk dependencies."
        echo "After initialization, use ./update-workspace.sh (also resumes failed setup)."
        exit 0
    fi
    [[ $# == 0 ]] || die "Unknown arguments. Use --help."
    workspace_paths
    refuse_existing_setup
    check_host
    check_environment_variables
    wrap_checkout
    acquire_lock
    trap release_lock EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    trap 'report_failure' ERR
    printf '%s\n' "$APP_DIR" > "$STATE_DIR/owner"
    printf 'initializing\n' > "$STATE_DIR/status"
    prepare_python
    initialize_west
    refresh_workspace
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    setup_main "$@"
fi
