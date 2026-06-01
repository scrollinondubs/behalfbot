#!/usr/bin/env bash
# gather-docker-prune.sh — heartbeat gather for the docker-prune routine.
#
# Fires the docker-prune action directly from the gather step. This is the
# "do the work in the gather, count=0 = clean run, count>0 = something needs
# Claude" pattern used elsewhere (e.g. gather-pg-backup, gather-strava-ingest).
# Pruning is non-destructive against running services: `docker builder prune`
# only removes unreferenced build cache layers, and `docker image prune -af`
# only removes images currently NOT used by any container.
#
# Why in-container rather than host launchd:
# Per Sean's 2026-05-25 directive: "any interaction with the [docker daemon]
# that can run in the container should be there. Dating is the only thing
# that should run external to the container." This script + the matching
# heartbeat entry move what was previously a host-resident launchd job
# (com.<assistant>.docker-prune-weekly) into the chassis container dispatcher path
# so future installers get it by default without per-host plist plumbing.
#
# The chassis container needs Docker CLI installed (provided by the
# Dockerfile) AND a bind-mount of /var/run/docker.sock (provided by
# docker-compose.yml). Security trade-off is documented inline in both.
#
# Gather contract:
#   {"count": 0, "reclaimed_bytes": <int>, "status": "ok", ...}
#     → no Claude alert needed
#   {"count": 1, "status": "docker_unreachable" | "prune_failed", ...}
#     → fires the alert prompt
#
# Schedule: weekly Sunday 03:00 local. See HEARTBEATS.md entry.

set -uo pipefail

CHECKED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)

emit_failure() {
    local tag="$1" detail="$2"
    local escaped
    escaped=$(printf '%s' "$detail" | sed 's/"/\\"/g')
    printf '{"count": 1, "status": "%s", "detail": "%s", "checked_at": "%s"}\n' \
        "$tag" "$escaped" "$CHECKED_AT"
}

# 1. Sanity — can we reach the docker daemon at all?
if ! docker info >/dev/null 2>&1; then
    emit_failure "docker_unreachable" \
        "\`docker info\` failed inside the chassis container. The /var/run/docker.sock bind-mount may be missing or permission-broken."
    exit 0
fi

# 2. Capture before/after sizes for the log + telemetry.
BEFORE=$(docker system df --format '{{.Type}}={{.Size}}' 2>/dev/null | tr '\n' ';')

# 3. Run builder prune — unreferenced cache layers. Most cuttable.
if ! BUILDER_OUT=$(docker builder prune -f 2>&1); then
    emit_failure "prune_failed" "docker builder prune failed: $(printf '%s' "$BUILDER_OUT" | tail -1 | head -c 200)"
    exit 0
fi

# 4. Image prune — drop tagged-but-unused images.
if ! IMAGE_OUT=$(docker image prune -af 2>&1); then
    emit_failure "prune_failed" "docker image prune failed: $(printf '%s' "$IMAGE_OUT" | tail -1 | head -c 200)"
    exit 0
fi

# 5. Extract reclaimed bytes from each step (best-effort — format varies).
extract_reclaim() {
    # Both `docker builder prune` and `docker image prune` print a final
    # "Total reclaimed space: <human-readable>" line on success.
    printf '%s' "$1" | awk -F': ' '/Total reclaimed space/ {print $2; exit}'
}
BUILDER_RECLAIMED=$(extract_reclaim "$BUILDER_OUT")
IMAGE_RECLAIMED=$(extract_reclaim "$IMAGE_OUT")

AFTER=$(docker system df --format '{{.Type}}={{.Size}}' 2>/dev/null | tr '\n' ';')

printf '{"count": 0, "status": "ok", "checked_at": "%s", "builder_reclaimed": "%s", "image_reclaimed": "%s", "before": "%s", "after": "%s"}\n' \
    "$CHECKED_AT" "${BUILDER_RECLAIMED:-0B}" "${IMAGE_RECLAIMED:-0B}" "$BEFORE" "$AFTER"
