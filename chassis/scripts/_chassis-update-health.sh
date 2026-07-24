#!/bin/bash
# _chassis-update-health.sh - container discovery + VERSION probe for the
# post-update healthcheck in chassis-update.sh.
#
# Sourced, never executed. Split out of chassis-update.sh so the discovery and
# probe logic is testable without running a real update
# (see test-chassis-update-health.sh).
#
# Why this exists
# ===============
# The original healthcheck (chassis-update.sh v0.1.0 - v0.1.1) was decorative.
# Two independent defects stacked:
#
#   1. It ran `docker exec "$c" cat /chassis/VERSION`. The Dockerfile copies the
#      tree to /app/chassis (`COPY chassis/ /app/chassis/`), so that read has
#      NEVER succeeded on any image the repo has ever published. Verified by
#      building `FROM busybox` + the same COPY line: /app/chassis/VERSION reads
#      0.1.1, /chassis/VERSION does not exist.
#   2. It found the container with `docker ps --filter label=com.behalfbot.chassis`.
#      Nothing in the repo ever set that label - not the Dockerfile (it has no
#      LABEL instruction at all), not docker-compose.yml, not docker-publish.yml
#      (which stamps only org.opencontainers.image.* via metadata-action). The
#      filter matched zero containers on a live install with the chassis
#      container up and healthy.
#
# Either defect alone dropped the check through to a host-disk fallback that
# compared the VERSION file the subtree pull had just advanced against the
# upstream VERSION it was pulled from. That comparison is a tautology: it passes
# whether or not the container ever restarted. Net effect - a container stuck on
# the old image reported healthy, and the rollback in step 7 could not fire.
#
# The contract now: in container mode the container read is the ONLY evidence
# that counts. There is no disk fallback. A healthcheck that cannot read the
# container does not get to report success.

# Where the image puts the chassis tree. Matches `COPY chassis/ /app/chassis/`
# in the Dockerfile and the CHASSIS_ROOT env the compose stack exports. Used
# only when the container does not report CHASSIS_ROOT itself.
CHASSIS_CONTAINER_ROOT_DEFAULT="/app/chassis"

# Name of the running chassis container, or empty string if none is up.
#
# Three probes, most authoritative first. Compose knows the project layout, so
# it wins. The label probe is kept because docker-compose.yml now sets
# com.behalfbot.chassis - it covers installs whose compose file this script
# cannot see. The container_name probe is the last resort for hand-run
# `docker run` installs.
chassis_find_container() {
    local compose_dir="${1:-}"
    local name=""

    if [[ -n "$compose_dir" && -f "${compose_dir}/docker-compose.yml" ]]; then
        name=$(cd "$compose_dir" && docker compose ps --format '{{.Name}}' chassis 2>/dev/null | head -1)
    fi
    if [[ -z "$name" ]]; then
        name=$(docker ps --filter "label=com.behalfbot.chassis" --format '{{.Names}}' 2>/dev/null | head -1)
    fi
    if [[ -z "$name" ]]; then
        name=$(docker ps --filter "name=^behalfbot$" --format '{{.Names}}' 2>/dev/null | head -1)
    fi

    printf '%s' "$name"
}

# Print the VERSION the given container is actually running. Returns non-zero
# and prints nothing when the read fails, so callers can tell "container says
# 0.1.1" apart from "could not ask the container".
#
# Probe order, most truthful first:
#   1. /app/customer/state/chassis-root/VERSION - the symlink the entrypoint's
#      resolve-chassis-root.sh materialises at boot. It records which tree the
#      dispatcher is ACTUALLY running (live mounted clone vs baked image copy).
#      `docker exec` cannot see the entrypoint's exported CHASSIS_ROOT (exec
#      sessions get only the image/compose ENV), so the symlink is the only
#      honest cross-process channel. The symlink is dereferenced inside the
#      container, so container-internal targets resolve correctly.
#   2. printenv CHASSIS_ROOT - pre-fix images (which baked the ENV) and
#      installs that override it via compose environment / docker -e. For a
#      pre-fix image this reads the baked tree, which IS what it runs.
#   3. /app/chassis - the historical default.
chassis_container_version() {
    local container="$1"
    local root version

    [[ -z "$container" ]] && return 1

    version=$(docker exec "$container" cat /app/customer/state/chassis-root/VERSION 2>/dev/null | tr -d '[:space:]')
    if [[ -z "$version" ]]; then
        root=$(docker exec "$container" printenv CHASSIS_ROOT 2>/dev/null | tr -d '[:space:]')
        [[ -z "$root" ]] && root="$CHASSIS_CONTAINER_ROOT_DEFAULT"
        version=$(docker exec "$container" cat "${root}/VERSION" 2>/dev/null | tr -d '[:space:]')
    fi
    [[ -z "$version" ]] && return 1

    printf '%s' "$version"
}

# Which healthcheck contract applies: "container" or "host".
#
# Container mode requires proof from inside the container. Host mode (no docker
# binary, or no compose file to bring a container up with) is the legitimate
# case for a disk-based check - some installs run the dispatcher directly on the
# host. The distinction is made ONCE, up front, from facts that do not change
# mid-poll. It is deliberately not a per-iteration fallback: that is exactly how
# the old check degraded into always passing.
chassis_healthcheck_mode() {
    local compose_dir="${1:-}"

    command -v docker >/dev/null 2>&1 || { printf 'host'; return; }
    [[ -n "$compose_dir" && -f "${compose_dir}/docker-compose.yml" ]] || { printf 'host'; return; }

    printf 'container'
}

# Image ref the chassis container is currently running, for rollback pinning.
# Empty when there is no container to ask.
chassis_container_image() {
    local container="$1"
    [[ -z "$container" ]] && return 1
    docker inspect --format '{{.Image}}' "$container" 2>/dev/null | tr -d '[:space:]'
}
