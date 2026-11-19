#!/usr/bin/env bash
# _alert.sh - one place for "tell the operator something, without a model".
#
# Sourced, not executed. Exposes a single function, `chassis_alert`, used by
# the paths that must still work when the assistant itself cannot run: a
# credential expiry, a dead oauth bridge, anything where firing `claude -p`
# would depend on the very thing that is broken.
#
# Delivery resolution, in order:
#   1. $CHASSIS_ALERT_CMD, when set. The message is appended as one argv
#      element, so `CHASSIS_ALERT_CMD="/path/post-to-slack.sh ops"` works,
#      and so does pointing it at post-to-telegram.sh or a bare curl wrapper.
#   2. `post-to-channel.sh $CHASSIS_ALERT_CHANNEL` (default channel key
#      `ops`), which resolves a webhook URL out of the install's .env.
#   3. `discord-post.sh` with a bot token and a channel ID, resolved from
#      $CHASSIS_ALERT_CHANNEL_ID, then DISCORD_ALERTS_CHANNEL_ID,
#      DISCORD_OPS_CHANNEL_ID, DISCORD_PRIMARY_CHANNEL_ID - the convention
#      chassis.config.yaml and the shipped prompts already use.
#
# CHASSIS_ALERT_TRANSPORT (environment, else the install's .env) picks the
# chat app for steps 2 and 3. Unset or `discord` is exactly the chain above.
#   matrix  matrix-post.py to CHASSIS_MATRIX_ALERTS_ROOM (then _OPS_, _DM_),
#           falling back to the Discord chain when Matrix fails.
#   both    Matrix and the Discord chain, both always attempted; success when
#           either delivered, so an edge-triggered caller does not repost the
#           leg that worked every tick while the other is down.
# $CHASSIS_ALERT_CMD still overrides all of it.
#
# Step 3 is not redundant. An install can perfectly well have a bot token and
# channel IDs and NO webhook URL at all - the reference install is exactly
# that shape, which is also why `alert_ops()` in the dispatcher has been
# dropping its own alerts there with `WARN: no ops webhook set`. A helper that
# stopped at step 2 would be one more monitor firing into a log.
#
# Returns 0 when the message went out, non-zero otherwise. Callers must treat
# a failure as non-fatal - an alert that cannot be delivered is not a reason
# to break the thing that was trying to send it.
#
# Deliberately no state, no dedupe, no formatting opinions. Repeat
# suppression belongs to the caller, which is the only layer that knows what
# "the same alert" means.

# Resolved at source time: `$0` is the caller, and BASH_SOURCE[0] is this file.
CHASSIS_ALERT_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export CHASSIS_ALERT_LIB_DIR

# .env.baked holds literals; the raw .env may carry a Vaultwarden hydration
# block that only works on the host. Same preference as the entrypoint.
_chassis_alert_envfile() {
    if [[ -f "$1/.env.baked" ]]; then
        printf '%s' "$1/.env.baked"
    else
        printf '%s' "$1/.env"
    fi
}

_chassis_alert_transport() {
    local home="$1" t="${CHASSIS_ALERT_TRANSPORT:-}" envfile
    envfile="$(_chassis_alert_envfile "$home")"
    if [[ -z "$t" && -f "$envfile" ]]; then
        t="$(
            set +eu
            # shellcheck source=/dev/null
            . "$envfile" >/dev/null 2>&1
            printf '%s' "${CHASSIS_ALERT_TRANSPORT:-}"
        )"
    fi
    t="$(printf '%s' "${t:-discord}" | tr '[:upper:]' '[:lower:]')"
    case "$t" in
        matrix|both) printf '%s' "$t" ;;
        *) printf 'discord' ;;
    esac
}

_chassis_alert_matrix() {
    local home="$1" msg="$2" dir="$CHASSIS_ALERT_LIB_DIR" envfile
    [[ -f "${dir}/matrix-post.py" ]] || return 1
    envfile="$(_chassis_alert_envfile "$home")"
    # Message on stdin: a message that starts with `@` would otherwise be read
    # as a path.
    (
        set +eu
        # shellcheck source=/dev/null
        [[ -f "$envfile" ]] && . "$envfile" >/dev/null 2>&1
        export CHASSIS_MATRIX_HOMESERVER CHASSIS_MATRIX_TOKEN_FILE CHASSIS_MATRIX_ALERTS_ROOM \
            CHASSIS_MATRIX_OPS_ROOM CHASSIS_MATRIX_DM_ROOM
        export CUSTOMER_HOME="${CUSTOMER_HOME:-$home}"
        printf '%s' "$msg" | python3 "${dir}/matrix-post.py" "${CHASSIS_ALERT_MATRIX_ROOM:-alerts}" - >/dev/null 2>&1
    )
}

_chassis_alert_discord() {
    local home="$1" msg="$2" dir="$CHASSIS_ALERT_LIB_DIR"

    if [[ -x "${dir}/post-to-channel.sh" ]] \
        && CHASSIS_HOME="$home" "${dir}/post-to-channel.sh" \
            "${CHASSIS_ALERT_CHANNEL:-ops}" "$msg" >/dev/null 2>&1; then
        return 0
    fi

    [[ -x "${dir}/discord-post.sh" ]] || return 1

    # Subshell: sourcing the install's .env must not clobber the caller's
    # environment, and a caller under `set -euo pipefail` must not die on a
    # line in a file it does not control.
    (
        set +eu
        # shellcheck source=/dev/null
        [[ -f "${home}/.env" ]] && . "${home}/.env"
        chan="${CHASSIS_ALERT_CHANNEL_ID:-${DISCORD_ALERTS_CHANNEL_ID:-${DISCORD_OPS_CHANNEL_ID:-${DISCORD_PRIMARY_CHANNEL_ID:-}}}}"
        [[ -n "${DISCORD_BOT_TOKEN:-}" && -n "$chan" ]] || exit 1
        export DISCORD_BOT_TOKEN
        "${dir}/discord-post.sh" "$chan" "$msg" >/dev/null 2>&1
    )
}

chassis_alert() {
    local msg="${1:-}"
    [[ -z "$msg" ]] && return 1

    if [[ -n "${CHASSIS_ALERT_CMD:-}" ]]; then
        # Word-split on purpose: the var carries a command plus its fixed
        # arguments, and the message is the final argv element.
        # shellcheck disable=SC2086
        ${CHASSIS_ALERT_CMD} "$msg"
        return $?
    fi

    # post-to-channel.sh hard-requires CHASSIS_HOME (it sources .env from
    # there for the webhook URL). A launchd job has neither var set, so
    # resolve one rather than letting the poster die on `:?`.
    local home="${CHASSIS_HOME:-${CUSTOMER_HOME:-${HOME}/.behalfbot}}"
    local m=1 d=1

    case "$(_chassis_alert_transport "$home")" in
        matrix)
            _chassis_alert_matrix "$home" "$msg" && return 0
            _chassis_alert_discord "$home" "$msg"
            ;;
        both)
            _chassis_alert_matrix "$home" "$msg" && m=0
            _chassis_alert_discord "$home" "$msg" && d=0
            [[ "$m" -eq 0 || "$d" -eq 0 ]]
            ;;
        *)
            _chassis_alert_discord "$home" "$msg"
            ;;
    esac
}
