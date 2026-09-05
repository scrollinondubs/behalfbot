#!/usr/bin/env bash
# colima-ensure.sh - idempotent, boot-safe "make the Docker daemon answer" for
# macOS installs whose Docker runtime is Colima.
#
# Single owner of record for starting Colima on a chassis install. Nothing else
# should run `colima start`. See docs/colima-recovery.md.
#
# ---------------------------------------------------------------------------
# WHY THIS EXISTS
# ---------------------------------------------------------------------------
# An unclean shutdown (hard power-off, panic, pulled plug) leaves Lima's runtime
# files behind in ~/.colima/_lima/<instance>/:
#
#     ha.pid   vz.pid   ha.sock   ssh.sock
#
# On the next boot those pid files point at PIDs the kernel has since reissued
# to unrelated processes. Colima reads them, believes the VM is alive, and every
# subsequent command talks to a socket nobody is listening on. The install is
# then wedged in a state that no number of `colima start` retries clears.
#
# Observed on the reference Mac Mini 2026-09-05 after a hard power-off, when the
# stale pids resolved to `sirittsd` (scrollinondubs/new-jaxity#550):
#
#     colima list      ->  STATUS  Broken
#     colima status    ->  colima is not running
#     docker ps        ->  Cannot connect to the Docker daemon at
#                          unix:///Users/<user>/.colima/default/docker.sock
#     colima start     ->  errors inspecting instance: [failed to get Info from
#                          ".../ha.sock": dial unix .../ha.sock: connect:
#                          connection refused]
#     daemon.log       ->  waiting 5 secs for VM   (forever)
#
# Grep any of those strings and you should land here.
#
# `colima stop --force` removes the stale pid and socket files and flips the
# profile from Broken back to Stopped, after which a plain `colima start`
# succeeds. It is non-destructive: the VM's `disk` image, and therefore every
# Docker volume on it, is untouched.
#
# ---------------------------------------------------------------------------
# HARD SAFETY RULE - DO NOT "IMPROVE" THIS SCRIPT BY ADDING EITHER OF THESE
# ---------------------------------------------------------------------------
# This script must NEVER run `colima delete` or `colima start --reset`.
#
# Both destroy the VM's disk image, and the disk image is where every Docker
# volume lives - Postgres data, the note app's workspace, anything a container
# has persisted. On the reference install that is a 20GB image holding state
# with no other copy on the box. A recovery script that can silently delete the
# thing it is recovering is worse than no recovery script.
#
# The only two verbs allowed here are `stop --force` and `start`. If those do
# not fix it, this script exits non-zero and a human looks at it. That is the
# correct outcome.
#
# ---------------------------------------------------------------------------
# CONTRACT
# ---------------------------------------------------------------------------
# Exit 0  - Docker answers (already did, or this run made it).
# Exit 0  - Not applicable: host is not macOS, or Colima is not installed. A
#           Linux install runs Docker natively and needs nothing from this.
# Exit 0  - Another instance holds the lock. Not an error: the other run is
#           doing exactly this work, and two concurrent recoveries fighting
#           over the same pid files is the failure mode the lock prevents.
# Exit 1  - Colima could not be brought up. The log names what was tried.
#
# Safe to run at boot, on a timer, and by hand at the same time.
#
# ---------------------------------------------------------------------------
# ENV
# ---------------------------------------------------------------------------
#   COLIMA_PROFILE                 profile name. Default: default
#   COLIMA_BIN                     path to colima. Default: resolved from PATH,
#                                  then the stable Homebrew opt/ symlinks.
#   DOCKER_BIN                     path to docker. Same resolution.
#   CUSTOMER_HOME                  per-customer state root, used for the log
#                                  path. Default: $HOME/.behalfbot
#   COLIMA_ENSURE_LOG              override the log file outright.
#   COLIMA_ENSURE_DOCKER_TIMEOUT   seconds to wait for `docker ps` to answer
#                                  after a start. Default: 180
#   COLIMA_ENSURE_POLL             seconds between docker probes. Default: 3
#   COLIMA_ENSURE_LOCK_DIR         lock directory. Default:
#                                  ${TMPDIR:-/tmp}/colima-ensure.<profile>.lock
#   COLIMA_ENSURE_LOCK_MAX_AGE     seconds after which a held lock is treated
#                                  as abandoned. Default: 900
#   COLIMA_ENSURE_START_ARGS       extra args passed to `colima start` ONLY when
#                                  the profile does not exist yet (first-ever
#                                  creation), e.g. "--cpus 4 --memory 6 --disk 30".
#                                  Never applied to an existing profile: sizing
#                                  policy belongs to whoever created the VM, not
#                                  to a recovery script.
#   COLIMA_ENSURE_OS               override the detected OS. Test-only.
#   COLIMA_ENSURE_PREFIX_ROOT      prefix applied to the hardcoded Homebrew
#                                  fallback paths. Test-only: point it at an
#                                  empty directory to simulate a host with no
#                                  Colima installed.
#
# Usage:
#   bash chassis/scripts/colima-ensure.sh

set -uo pipefail

PROFILE="${COLIMA_PROFILE:-default}"
DOCKER_TIMEOUT="${COLIMA_ENSURE_DOCKER_TIMEOUT:-180}"
POLL_SECONDS="${COLIMA_ENSURE_POLL:-3}"
LOCK_DIR="${COLIMA_ENSURE_LOCK_DIR:-${TMPDIR:-/tmp}/colima-ensure.${PROFILE}.lock}"
LOCK_MAX_AGE="${COLIMA_ENSURE_LOCK_MAX_AGE:-900}"
HOST_OS="${COLIMA_ENSURE_OS:-$(uname -s)}"

CUSTOMER_HOME="${CUSTOMER_HOME:-$HOME/.behalfbot}"
LOG_FILE="${COLIMA_ENSURE_LOG:-$CUSTOMER_HOME/logs/scheduled/colima-ensure.log}"
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >> "$LOG_FILE" 2>/dev/null
    printf '%s\n' "$*"
}

log_block() {
    # Indent a captured command's output so it cannot be mistaken for this
    # script's own lines when someone greps the log.
    local line
    while IFS= read -r line; do
        log "  | $line"
    done <<< "$1"
}

# Resolve a binary from PATH first, then from the stable Homebrew opt/ symlinks.
# launchd hands jobs a minimal PATH, and Cellar version paths rotate on every
# `brew upgrade`, so opt/ is the only path worth hardcoding.
resolve_bin() {
    local name="$1" override="$2" candidate
    if [[ -n "$override" ]]; then
        printf '%s' "$override"
        return 0
    fi
    if candidate="$(command -v "$name" 2>/dev/null)"; then
        printf '%s' "$candidate"
        return 0
    fi
    local root="${COLIMA_ENSURE_PREFIX_ROOT:-}"
    for candidate in "$root/opt/homebrew/opt/$name/bin/$name" \
                     "$root/usr/local/opt/$name/bin/$name" \
                     "$root/opt/homebrew/bin/$name" \
                     "$root/usr/local/bin/$name"; do
        if [[ -x "$candidate" ]]; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    return 1
}

# ---------------------------------------------------------------------------
# Applicability
# ---------------------------------------------------------------------------
if [[ "$HOST_OS" != "Darwin" ]]; then
    log "not applicable: host is $HOST_OS, not Darwin. Colima is macOS only; Linux installs run Docker natively."
    exit 0
fi

if ! COLIMA_BIN="$(resolve_bin colima "${COLIMA_BIN:-}")"; then
    log "not applicable: colima not found on PATH or in the Homebrew opt/ prefixes. Nothing to start."
    exit 0
fi
if ! DOCKER_BIN="$(resolve_bin docker "${DOCKER_BIN:-}")"; then
    log "ERROR: colima is installed at $COLIMA_BIN but docker is not. Cannot verify the daemon answers, so refusing to report success."
    exit 1
fi

# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------
# `mkdir` is atomic on every filesystem the chassis runs on, and macOS ships no
# flock(1). Two concurrent recoveries would race on the same pid and socket
# files, which is the class of problem this script exists to clean up. The
# reference install's operator had to boot out three watchdogs by hand to stop
# them racing the manual recovery; this is that, automated.
#
# Staleness is decided by AGE as well as by whether the recorded pid is alive.
# `kill -0 <pid>` on a recycled pid returns success for a completely unrelated
# process - that is literally the bug in the postmortem, where a stale pid file
# resolved to `sirittsd`. The pid check is a fast path; the age check is what
# makes it correct.
#
# Teardown is `rm -f` on the one known file plus `rmdir`, never a recursive
# delete. `rmdir` refuses to remove a non-empty directory, so a mis-set
# COLIMA_ENSURE_LOCK_DIR can fail loudly but cannot wipe a tree.
LOCK_HELD=false

release_lock() {
    if [[ "$LOCK_HELD" == "true" ]]; then
        rm -f "$LOCK_DIR/pid" 2>/dev/null || true
        rmdir "$LOCK_DIR" 2>/dev/null || true
        LOCK_HELD=false
    fi
}

lock_age_seconds() {
    local now mtime=""
    now="$(date +%s)"

    # BSD stat (macOS) and GNU stat (Linux, where the CI suite runs) spell this
    # differently, and the two spellings are NOT safely chainable inside one
    # command substitution. GNU `stat -f %m <dir>` does not error out: -f means
    # "filesystem status" there, `%m` is read as another FILE operand, and stat
    # prints a whole filesystem block for <dir> on stdout before exiting 1 for
    # the missing `%m`. Chained with `||` in a single `$(...)`, both commands'
    # stdout concatenates and the result is a multi-line block with an epoch
    # glued on the end. That parsed as age 0, every stale lock read as fresh,
    # and the recycled-pid branch below became unreachable on Linux.
    #
    # So: one substitution per assignment, and validate that what came back is
    # actually a number before trusting it.
    mtime="$(stat -c %Y "$LOCK_DIR" 2>/dev/null)" || mtime=""
    if [[ ! "$mtime" =~ ^[0-9]+$ ]]; then
        mtime="$(stat -f %m "$LOCK_DIR" 2>/dev/null)" || mtime=""
    fi
    if [[ ! "$mtime" =~ ^[0-9]+$ ]]; then
        # Age is unknowable. Report 0 (fresh), which makes the caller respect
        # the lock. Standing down for one 300s tick is cheap; breaking a lock a
        # live recovery is holding is not.
        printf '0'
        return 0
    fi
    printf '%s' "$(( now - mtime ))"
}

take_lock() {
    if mkdir "$LOCK_DIR" 2>/dev/null; then
        printf '%s' "$$" > "$LOCK_DIR/pid" 2>/dev/null || true
        LOCK_HELD=true
        trap release_lock EXIT
        return 0
    fi
    return 1
}

acquire_lock() {
    take_lock && return 0

    local holder age
    holder="$(cat "$LOCK_DIR/pid" 2>/dev/null || printf '')"
    age="$(lock_age_seconds)"

    [[ "$age" =~ ^[0-9]+$ ]] || age=0
    if [[ "$age" -lt "$LOCK_MAX_AGE" ]] && [[ -n "$holder" ]] && kill -0 "$holder" 2>/dev/null; then
        log "another colima-ensure run holds $LOCK_DIR (pid $holder, age ${age}s). Standing down; it is doing this work."
        return 1
    fi

    log "breaking abandoned lock $LOCK_DIR (pid '${holder:-none}', age ${age}s, max ${LOCK_MAX_AGE}s)"
    rm -f "$LOCK_DIR/pid" 2>/dev/null || true
    rmdir "$LOCK_DIR" 2>/dev/null || true
    take_lock && return 0

    log "lost the race to retake $LOCK_DIR. Standing down."
    return 1
}

# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------

# The only success signal that means anything. `colima status` reporting
# "running" while the socket refuses connections is exactly the state that
# wedged the reference install, so status is never trusted on its own.
docker_answers() {
    "$DOCKER_BIN" ps --quiet >/dev/null 2>&1
}

wait_for_docker() {
    local waited=0 step="$POLL_SECONDS"
    # Floor the step at 1s. A zero step would advance `waited` by nothing and
    # spin this loop forever against any timeout above zero.
    (( step < 1 )) && step=1
    while true; do
        if docker_answers; then
            return 0
        fi
        if (( waited >= DOCKER_TIMEOUT )); then
            return 1
        fi
        sleep "$step"
        waited=$(( waited + step ))
    done
}

# Prints one of: Running | Stopped | Broken | absent | unknown
profile_status() {
    local json line status
    if ! json="$("$COLIMA_BIN" list --json 2>/dev/null)"; then
        printf 'unknown'
        return 0
    fi
    # One JSON object per line, one per profile. Parsed with grep and sed rather
    # than jq because jq is not guaranteed present on a freshly imaged Mac, and
    # this script has to work on the boot where everything else is broken.
    line="$(printf '%s\n' "$json" | grep -F "\"name\":\"${PROFILE}\"" | head -1)"
    if [[ -z "$line" ]]; then
        printf 'absent'
        return 0
    fi
    status="$(printf '%s' "$line" | sed -n 's/.*"status":"\([^"]*\)".*/\1/p' | head -1)"
    printf '%s' "${status:-unknown}"
}

start_colima() {
    local out rc
    local -a extra=()
    if [[ "$(profile_status)" == "absent" && -n "${COLIMA_ENSURE_START_ARGS:-}" ]]; then
        # Word-split deliberately: this is a flag string, not a path.
        # shellcheck disable=SC2206
        extra=(${COLIMA_ENSURE_START_ARGS})
        log "profile '$PROFILE' does not exist yet; creating it with: ${extra[*]}"
    fi
    out="$("$COLIMA_BIN" start --profile "$PROFILE" ${extra[@]+"${extra[@]}"} 2>&1)"
    rc=$?
    log "colima start --profile $PROFILE exited $rc"
    [[ -n "$out" ]] && log_block "$out"
    return $rc
}

force_stop() {
    local out rc
    # NON-DESTRUCTIVE. Removes ha.pid / vz.pid / ha.sock / ssh.sock and flips a
    # Broken profile to Stopped. Does NOT touch the disk image or any volume.
    out="$("$COLIMA_BIN" stop --force --profile "$PROFILE" 2>&1)"
    rc=$?
    log "colima stop --force --profile $PROFILE exited $rc"
    [[ -n "$out" ]] && log_block "$out"
    return $rc
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
acquire_lock || exit 0

if docker_answers; then
    log "docker already answering (profile '$PROFILE' status $(profile_status)). Nothing to do."
    exit 0
fi

STATUS="$(profile_status)"
log "docker is not answering. Profile '$PROFILE' status: $STATUS"

if [[ "$STATUS" == "Broken" ]]; then
    # Skip the first `colima start` entirely. Against a Broken profile it dies
    # on the stale ha.sock every time, and each attempt costs a minute of boot.
    log "status is Broken - stale lima runtime files from an unclean shutdown. Going straight to the forced stop."
else
    if start_colima && wait_for_docker; then
        log "recovered: docker answering after a plain start."
        exit 0
    fi
    log "plain start did not produce a working docker daemon. Falling back to the forced stop."
fi

force_stop

if start_colima && wait_for_docker; then
    log "recovered: docker answering after stop --force + start."
    exit 0
fi

log "FAILED: colima could not be brought up for profile '$PROFILE' after stop --force + start."
log "Next step is a human. Do NOT run 'colima delete' or 'colima start --reset' to clear this - both destroy the VM disk image and every docker volume on it."
log "Look at ~/.colima/${PROFILE}/daemon/daemon.log and ~/.colima/_lima/colima*/ha.stderr.log. Background: docs/colima-recovery.md"
exit 1
