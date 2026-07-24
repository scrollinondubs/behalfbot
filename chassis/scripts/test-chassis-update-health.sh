#!/bin/bash
# test-chassis-update-health.sh - Unit tests for the chassis-update healthcheck
# primitives in _chassis-update-health.sh.
#
# The bug these lock down
# =======================
# chassis-update.sh's post-update healthcheck could not fail. It looked for the
# container with `docker ps --filter label=com.behalfbot.chassis` (a label
# nothing set) and read `/chassis/VERSION` (a path the image does not have - the
# Dockerfile copies to /app/chassis). Both misses fell through to a host-disk
# check that compared the VERSION file the subtree pull had just written against
# the upstream value it came from, so it passed unconditionally. The rollback it
# was supposed to trigger therefore never ran.
#
# These tests stub `docker` on PATH, so no daemon, no image and no network are
# needed. Chassis test infra deliberately avoids both.
#
# Exit codes:
#   0 - all scenarios passed
#   1 - one or more scenarios failed
#   2 - test harness itself broke

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="${SCRIPT_DIR}/_chassis-update-health.sh"

if [[ ! -f "$LIB" ]]; then
    echo "test-chassis-update-health: lib not found at $LIB" >&2
    exit 2
fi
# shellcheck source=chassis/scripts/_chassis-update-health.sh
source "$LIB"

fail=0
pass=0

check() {
    local name="$1" expected="$2" actual="$3"
    if [[ "$expected" == "$actual" ]]; then
        pass=$((pass + 1))
    else
        echo "FAIL [$name] expected '$expected', got '$actual'"
        fail=$((fail + 1))
    fi
}

check_status() {
    local name="$1" expected="$2" actual="$3"
    if [[ "$expected" == "$actual" ]]; then
        pass=$((pass + 1))
    else
        echo "FAIL [$name] expected exit $expected, got $actual"
        fail=$((fail + 1))
    fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
STUB_BIN="${TMP}/bin"
mkdir -p "$STUB_BIN"
ORIGINAL_PATH="$PATH"

# Install a `docker` stub. $1 is a shell body; it receives the real docker
# argv and is responsible for the whole response.
stub_docker() {
    { printf '#!/bin/bash\n'; printf '%s\n' "$1"; } > "${STUB_BIN}/docker"
    chmod +x "${STUB_BIN}/docker"
    PATH="${STUB_BIN}:${ORIGINAL_PATH}"
}

# PATH is narrowed to the stub dir alone, not prepended, so a real docker on
# the developer's machine cannot satisfy the `command -v docker` probe. The
# functions under test use only bash builtins, so nothing else is needed.
no_docker() {
    rm -f "${STUB_BIN}/docker"
    PATH="${STUB_BIN}"
}

# A compose dir is just a directory holding a docker-compose.yml.
COMPOSE_DIR="${TMP}/install"
mkdir -p "$COMPOSE_DIR"
echo "services: {}" > "${COMPOSE_DIR}/docker-compose.yml"
BARE_DIR="${TMP}/hostmode"
mkdir -p "$BARE_DIR"

# ---------------------------------------------------------------------------
# chassis_healthcheck_mode
# ---------------------------------------------------------------------------
stub_docker 'exit 0'
check "mode/compose-present-is-container" "container" "$(chassis_healthcheck_mode "$COMPOSE_DIR")"
check "mode/no-compose-is-host" "host" "$(chassis_healthcheck_mode "$BARE_DIR")"
check "mode/empty-dir-arg-is-host" "host" "$(chassis_healthcheck_mode "")"

no_docker
check "mode/no-docker-binary-is-host" "host" "$(chassis_healthcheck_mode "$COMPOSE_DIR")"
PATH="${STUB_BIN}:${ORIGINAL_PATH}"

# ---------------------------------------------------------------------------
# chassis_find_container
# ---------------------------------------------------------------------------
# Compose is authoritative when the project file is visible.
stub_docker '
if [[ "$1" == "compose" ]]; then echo "behalfbot-from-compose"; exit 0; fi
if [[ "$1" == "ps" ]]; then echo "behalfbot-from-ps"; exit 0; fi
exit 1'
check "find/compose-wins" "behalfbot-from-compose" "$(chassis_find_container "$COMPOSE_DIR")"

# The regression that mattered: the label filter matches nothing on every real
# install, because nothing set com.behalfbot.chassis. Discovery must still find
# the container via the container_name probe.
stub_docker '
if [[ "$1" == "compose" ]]; then exit 1; fi
if [[ "$1" == "ps" ]]; then
    for a in "$@"; do
        [[ "$a" == "label=com.behalfbot.chassis" ]] && exit 0   # matches nothing
    done
    echo "behalfbot"; exit 0
fi
exit 1'
check "find/falls-back-past-unset-label" "behalfbot" "$(chassis_find_container "$COMPOSE_DIR")"

# Nothing running anywhere: empty string, never a guess.
stub_docker 'exit 0'
check "find/nothing-running-is-empty" "" "$(chassis_find_container "$COMPOSE_DIR")"

# ---------------------------------------------------------------------------
# chassis_container_version
# ---------------------------------------------------------------------------
# The resolved-root symlink is the most truthful probe: when the entrypoint's
# resolve-chassis-root.sh picked the live mounted tree, the baked ENV (which a
# docker exec printenv still reports on pre-fix images) is a LIE about what is
# running. The symlink read must win over the printenv path.
stub_docker '
if [[ "$1" == "exec" ]]; then
    shift; container="$1"; shift
    if [[ "$1" == "cat" && "$2" == "/app/customer/state/chassis-root/VERSION" ]]; then
        echo "0.3.0"; exit 0
    fi
    if [[ "$1" == "printenv" ]]; then echo "/app/chassis"; exit 0; fi
    if [[ "$1" == "cat" ]]; then
        [[ "$2" == "/app/chassis/VERSION" ]] && { echo "0.2.0"; exit 0; }
        exit 1
    fi
fi
exit 1'
check "version/resolved-symlink-wins-over-baked-env" "0.3.0" "$(chassis_container_version behalfbot)"

# The image puts the tree at /app/chassis and exports CHASSIS_ROOT. Reading the
# old hardcoded /chassis/VERSION must NOT be what happens. (Pre-fix images have
# no chassis-root symlink, so that probe misses and the env path answers.)
stub_docker '
if [[ "$1" == "exec" ]]; then
    shift; container="$1"; shift
    if [[ "$1" == "printenv" ]]; then echo "/app/chassis"; exit 0; fi
    if [[ "$1" == "cat" ]]; then
        [[ "$2" == "/app/chassis/VERSION" ]] && { echo "0.2.0"; exit 0; }
        exit 1
    fi
fi
exit 1'
check "version/reads-chassis-root-path" "0.2.0" "$(chassis_container_version behalfbot)"

# CHASSIS_ROOT unset in an older image: fall back to the documented default,
# which is still /app/chassis and still not /chassis.
stub_docker '
if [[ "$1" == "exec" ]]; then
    shift; container="$1"; shift
    if [[ "$1" == "printenv" ]]; then exit 1; fi
    if [[ "$1" == "cat" ]]; then
        [[ "$2" == "/app/chassis/VERSION" ]] && { echo "0.1.1"; exit 0; }
        exit 1
    fi
fi
exit 1'
check "version/default-root-when-env-unset" "0.1.1" "$(chassis_container_version behalfbot)"

# Whitespace and trailing newline are stripped, as the version compare is ==.
stub_docker '
if [[ "$1" == "exec" ]]; then
    shift; shift
    if [[ "$1" == "printenv" ]]; then echo "/app/chassis"; exit 0; fi
    if [[ "$1" == "cat" ]]; then printf "  0.2.0 \n"; exit 0; fi
fi
exit 1'
check "version/strips-whitespace" "0.2.0" "$(chassis_container_version behalfbot)"

# THE core honesty property: an unreadable VERSION must report failure, not an
# empty string a caller might compare loosely, and never a silent success. This
# is the exact shape of the original bug - the read failed on every install.
stub_docker '
if [[ "$1" == "exec" ]]; then
    shift; shift
    if [[ "$1" == "printenv" ]]; then echo "/app/chassis"; exit 0; fi
    if [[ "$1" == "cat" ]]; then echo "cat: no such file" >&2; exit 1; fi
fi
exit 1'
out=$(chassis_container_version behalfbot); status=$?
check_status "version/unreadable-returns-nonzero" 1 "$status"
check "version/unreadable-prints-nothing" "" "$out"

# No container name to ask: same contract.
out=$(chassis_container_version ""); status=$?
check_status "version/empty-container-returns-nonzero" 1 "$status"

# ---------------------------------------------------------------------------
# chassis_container_image
# ---------------------------------------------------------------------------
stub_docker '
if [[ "$1" == "inspect" ]]; then echo "sha256:deadbeef"; exit 0; fi
exit 1'
check "image/reads-inspect" "sha256:deadbeef" "$(chassis_container_image behalfbot)"

out=$(chassis_container_image ""); status=$?
check_status "image/empty-container-returns-nonzero" 1 "$status"

PATH="$ORIGINAL_PATH"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo "test-chassis-update-health: $pass passed, $fail failed"
if [[ $fail -gt 0 ]]; then
    exit 1
fi
exit 0
