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
#      `ops`), which resolves the webhook URL out of the install's .env.
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

    local poster="${CHASSIS_ALERT_LIB_DIR}/post-to-channel.sh"
    [[ -x "$poster" ]] || return 1

    # post-to-channel.sh hard-requires CHASSIS_HOME (it sources .env from
    # there for the webhook URL). A launchd job has neither var set, so
    # resolve one rather than letting the poster die on `:?`.
    CHASSIS_HOME="${CHASSIS_HOME:-${CUSTOMER_HOME:-${HOME}/.behalfbot}}" \
        "$poster" "${CHASSIS_ALERT_CHANNEL:-ops}" "$msg"
}
