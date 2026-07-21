#!/bin/bash
# _compose-verify.sh - verify that a merged compose config is what the docker
# engine is actually running.
#
# Sourced, never executed. Used by chassis-update.sh (post-update verification)
# and chassis-migrations/v0.3.0.sh (post-repair verification). Exercised by
# test-compose-verify.sh against a real scratch compose project.
#
# Why this exists (behalfbot#100)
# ===============================
# chassis-update.sh used to bring the stack back with bare `docker compose
# up -d`, silently dropping the per-install override - published ports, image
# pins, scaled-to-0 services. Its healthcheck polled the chassis container,
# which stays healthy over the internal compose network and never touches a
# published port, so every check that existed reported success on a broken
# install. Container-healthy is not install-healthy.
#
# These helpers check the things the override actually changes, against the
# MERGED config (`docker compose config --format json` over the chassis file
# plus the override):
#
#   1. every fixed host port the merged config declares is actually published
#      by a running container of that service
#   2. every service the merged config scales to 0 has NO running container
#   3. (separately callable) the chassis container's compose config_files
#      label includes the override file - the engine's own record that the
#      stack was built with the override layered in
#
# Contract: verification functions print human-readable "VERIFY-FAIL: ..."
# lines to stdout and return 0 only when the running state matches the config.
# They never mutate anything. jq is required - chassis-update.sh already
# depends on it.
#
# Containers are located via compose's own labels
# (com.docker.compose.project + com.docker.compose.service) rather than by
# replaying -f flags, so the same functions work whether the stack was brought
# up through compose.sh or bare compose. The project name is read from the
# merged config's top-level `name:` field, which compose resolves the same way
# `up` does (-p flag beats the yaml `name:` field beats the directory name).

# Print "service|target|published|host_ip|protocol" lines for every fixed
# published port in a merged config JSON document ($1 = path). Services scaled
# to 0 are skipped (nothing should be running, let alone publishing). Port
# entries without a fixed published port are skipped - ephemeral host ports
# are allocated by the engine and cannot be asserted.
compose_expected_ports() {
    local config_json="$1"
    jq -r '
        .services | to_entries[]
        | select(((.value.deploy.replicas // 1) | tonumber) > 0)
        | .key as $svc
        | (.value.ports // [])[]
        | select(.published != null and .published != "")
        | [$svc, (.target | tostring), (.published | tostring),
           (.host_ip // ""), (.protocol // "tcp")]
        | join("|")
    ' "$config_json"
}

# Print the name of every service the merged config scales to zero.
compose_zero_replica_services() {
    local config_json="$1"
    jq -r '
        .services | to_entries[]
        | select(((.value.deploy.replicas // 1) | tonumber) == 0)
        | .key
    ' "$config_json"
}

# Running container ids for a compose (project, service) pair.
compose_service_containers() {
    local project="$1" service="$2"
    docker ps -q \
        --filter "label=com.docker.compose.project=${project}" \
        --filter "label=com.docker.compose.service=${service}" 2>/dev/null
}

# Verify the running state against a merged config JSON document ($1 = path).
# Returns 0 when every declared published port is bound and every scaled-to-0
# service is down; otherwise prints VERIFY-FAIL lines and returns 1.
compose_verify_running_config() {
    local config_json="$1"
    local project fail=0 svc target published host_ip proto cids cid bound ok

    project=$(jq -r '.name // empty' "$config_json")
    if [[ -z "$project" ]]; then
        echo "VERIFY-FAIL: merged compose config has no project name; cannot locate containers"
        return 1
    fi

    while IFS='|' read -r svc target published host_ip proto; do
        [[ -z "$svc" ]] && continue
        cids=$(compose_service_containers "$project" "$svc")
        if [[ -z "$cids" ]]; then
            echo "VERIFY-FAIL: service '$svc' declares published port ${published}->${target}/${proto} but has no running container (project '$project')"
            fail=1
            continue
        fi
        ok=0
        for cid in $cids; do
            bound=$(docker port "$cid" "${target}/${proto}" 2>/dev/null)
            if [[ -n "$host_ip" && "$host_ip" != "0.0.0.0" ]]; then
                grep -q "^${host_ip}:${published}\$" <<<"$bound" && ok=1
            else
                grep -q ":${published}\$" <<<"$bound" && ok=1
            fi
        done
        if [[ $ok -eq 0 ]]; then
            echo "VERIFY-FAIL: service '$svc' should publish ${host_ip:+${host_ip}:}${published}->${target}/${proto} but no running container of it does"
            fail=1
        fi
    done < <(compose_expected_ports "$config_json")

    while read -r svc; do
        [[ -z "$svc" ]] && continue
        cids=$(compose_service_containers "$project" "$svc")
        if [[ -n "$cids" ]]; then
            echo "VERIFY-FAIL: service '$svc' is scaled to 0 in the merged config but is running: $(tr '\n' ' ' <<<"$cids")"
            fail=1
        fi
    done < <(compose_zero_replica_services "$config_json")

    return $fail
}

# Check that a service's container carries the override file in its
# com.docker.compose.project.config_files label - the engine's own record of
# which -f files built the stack the container belongs to.
#
# $1 = project name, $2 = override file path, $3 = service name (default
# "chassis"). Returns 0 when present, 1 when the label exists without the
# override (the #100 failure shape), 2 when there is no running container to
# inspect (caller decides what that means - the updater's version healthcheck
# has already established the container is up by the time this runs).
#
# Both the literal path and its physical (symlink-resolved) form are accepted:
# compose records the -f argument as given, and CUSTOMER_HOME is commonly
# reached through a symlink.
compose_override_in_config_files() {
    local project="$1" override="$2" service="${3:-chassis}"
    local cid label real

    cid=$(compose_service_containers "$project" "$service" | head -1)
    [[ -z "$cid" ]] && return 2

    label=$(docker inspect "$cid" \
        --format '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' 2>/dev/null)
    real="$(cd "$(dirname "$override")" 2>/dev/null && pwd -P)/$(basename "$override")"

    case ",${label}," in
        *",${override},"* | *",${real},"*) return 0 ;;
    esac
    return 1
}
