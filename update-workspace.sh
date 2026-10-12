#!/bin/bash
set -Eeuo pipefail
APP_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=setup-workspace.sh
source "$APP_DIR/setup-workspace.sh"

if [[ ${1:-} == --help && $# == 1 ]]; then
    echo "Usage: ./update-workspace.sh [--check]"
    echo "Sync to west.yml, refresh dependencies, and resume an interrupted setup."
    exit 0
fi
if [[ ${1:-} == --check && $# == 1 ]]; then
    workspace_paths
    require_owned_workspace
    check_environment_variables
    [[ -x "$WORKSPACE_PYTHON" ]] || die "Workspace Python is missing. Run update-workspace.sh."
    workspace_python "$WORKSPACE_PYTHON" check "$WORKSPACE_DIR"
    exit 0
fi
[[ $# == 0 ]] || die "Unknown arguments. Use --help."
workspace_paths
require_owned_workspace
check_host
check_environment_variables
acquire_lock
trap release_lock EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'report_failure' ERR
# Refuse source changes before removing the last completion record.
if [[ -e "$WORKSPACE_DIR/.west" ]]; then
    workspace_python "$HOST_PYTHON" guard "$WORKSPACE_DIR"
fi
printf 'updating\n' > "$STATE_DIR/status"
rm -f "$STATE_DIR/setup-complete.json"
prepare_python
initialize_west
refresh_workspace
