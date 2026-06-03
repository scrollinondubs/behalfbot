#!/usr/bin/env bash
# chassis/scripts/preflight-bot-identity.sh
# =========================================
# Pre-flight check: verify the bot's outbound identity matches what's
# configured for this install BEFORE the first heartbeat fires.
#
# Why this exists (chassis#5 item 3): Toby's first morning briefing on
# Asimov posted to Discord under the chassis maintainer's bot persona
# ("Captain Hook") instead of Asimov's persona. The briefing webhook
# went out with INSTANCE_NAME unset, so post-to-channel.sh defaulted
# to "Behalf.bot" - but the underlying Discord webhook itself was a
# carry-over from a different install and DISPLAYED a stale name.
# The fix is two-part:
#   1. Ensure INSTANCE_NAME is set + matches chassis.config.yaml's
#      identity.assistant.name before the dispatcher starts.
#   2. Verify every configured webhook URL still hits the right bot
#      (or at least warn loudly when it cannot be verified).
#
# This script is a "warn early, fail informatively" pre-flight. It does
# NOT mutate webhook config - just inspects + reports. Failures here block
# bootstrap completion so the installer fixes them before the first
# heartbeat fires under the wrong identity.
#
# Inputs:
#   CUSTOMER_HOME (required)  - to read .env + chassis.config.yaml from
#   CHASSIS_HOME  (required)  - to source post-to-channel.sh paths
#
# Exit codes:
#   0  all configured webhooks pass the identity check
#   1  one or more webhooks mismatch the expected identity
#   2  config could not be loaded (missing chassis.config.yaml or .env)

set -euo pipefail

: "${CUSTOMER_HOME:?CUSTOMER_HOME must be set}"
: "${CHASSIS_HOME:?CHASSIS_HOME must be set}"

CONFIG_FILE="$CUSTOMER_HOME/chassis.config.yaml"
ENV_FILE="$CUSTOMER_HOME/.env"

# Fall back to CHASSIS_HOME locations during the transitional window when
# CUSTOMER_HOME is being separated out (legacy installs).
if [[ ! -f "$CONFIG_FILE" && -f "$CHASSIS_HOME/chassis.config.yaml" ]]; then
    CONFIG_FILE="$CHASSIS_HOME/chassis.config.yaml"
fi
if [[ ! -f "$ENV_FILE" && -f "$CHASSIS_HOME/.env" ]]; then
    ENV_FILE="$CHASSIS_HOME/.env"
fi

say() {
    printf '%s\n' "$*"
}

# Extract identity.assistant.name from chassis.config.yaml. The YAML schema
# is shallow enough that awk is sufficient - no need to drag in yq/python.
# Format expected:
#   identity:
#     ...
#     assistant:
#       name: <agent-name>
extract_assistant_name() {
    awk '
        /^identity:/ { in_identity = 1; next }
        in_identity && /^  assistant:/ { in_assistant = 1; next }
        in_identity && in_assistant && /^    name:/ {
            sub(/^    name: */, "")
            sub(/ *#.*$/, "")
            gsub(/^"|"$|^'\''|'\''$/, "")
            print
            exit
        }
        in_identity && /^[^ ]/ && !/^identity:/ { exit }
        in_assistant && /^  [^ ]/ && !/^  assistant:/ { in_assistant = 0 }
    ' "$1"
}

if [[ ! -f "$CONFIG_FILE" ]]; then
    say "ERROR: chassis.config.yaml not found at $CONFIG_FILE" >&2
    exit 2
fi

EXPECTED_NAME=$(extract_assistant_name "$CONFIG_FILE")
if [[ -z "$EXPECTED_NAME" || "$EXPECTED_NAME" == "<agent-name>" ]]; then
    say "WARN: identity.assistant.name not configured in $CONFIG_FILE" >&2
    say "  Fill in identity.assistant.name before the first heartbeat fires," >&2
    say "  otherwise outbound posts default to the chassis 'Behalf.bot' label." >&2
    exit 1
fi

# Source the env file so webhook URLs + INSTANCE_NAME are in scope.
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi

# Check INSTANCE_NAME matches assistant.name. INSTANCE_NAME is what
# post-to-channel.sh and the heartbeat dispatcher pass as the webhook
# `username` field - this is the visible sender label in Discord.
INSTANCE_OK=true
if [[ -z "${INSTANCE_NAME:-}" ]]; then
    say "FAIL: INSTANCE_NAME not set in $ENV_FILE" >&2
    say "  Expected: INSTANCE_NAME=$EXPECTED_NAME" >&2
    say "  Without this, every outbound webhook posts as 'Behalf.bot'." >&2
    INSTANCE_OK=false
elif [[ "$INSTANCE_NAME" != "$EXPECTED_NAME" ]]; then
    say "FAIL: INSTANCE_NAME mismatch (env=$INSTANCE_NAME, expected=$EXPECTED_NAME)" >&2
    say "  Update $ENV_FILE so INSTANCE_NAME matches identity.assistant.name." >&2
    INSTANCE_OK=false
else
    say "  ✓ INSTANCE_NAME=$INSTANCE_NAME matches identity.assistant.name"
fi

# Resolve each known webhook key + check that the URL is set. We can't easily
# verify what name Discord shows on the webhook server-side from a shell
# script (would need a probe POST + read-back, which is noisy on installs
# already in flight). But we CAN catch the more common failure mode where
# the webhook env var isn't set at all - which produces the silent fallback
# to chassis defaults that Toby hit.
INSTANCE_PREFIX=""
if [[ -n "${INSTANCE_NAME:-}" ]]; then
    INSTANCE_PREFIX="$(printf '%s' "$INSTANCE_NAME" | tr '[:lower:]' '[:upper:]')_"
fi

WEBHOOK_OK=true
for key in BRIEFINGS OPS LEADS SOCIAL ALERTS; do
    prefixed="${INSTANCE_PREFIX}${key}_WEBHOOK_URL"
    bare="${key}_WEBHOOK_URL"
    if [[ -n "${!prefixed:-}" ]]; then
        say "  ✓ $prefixed configured"
    elif [[ -n "${!bare:-}" ]]; then
        say "  ✓ $bare configured (no INSTANCE_NAME prefix)"
    else
        case "$key" in
            BRIEFINGS|OPS)
                say "FAIL: neither $prefixed nor $bare is set" >&2
                say "  $key webhook required for chassis briefings/ops surface." >&2
                WEBHOOK_OK=false
                ;;
            *)
                say "  (skip) $key webhook unset - optional, only needed if the matching plugin is active"
                ;;
        esac
    fi
done

if [[ "$INSTANCE_OK" == "true" && "$WEBHOOK_OK" == "true" ]]; then
    say ""
    say "✓ bot-identity pre-flight OK (instance=$INSTANCE_NAME)"
    exit 0
fi

say "" >&2
say "Bot-identity pre-flight FAILED. Fix the items above and re-run bootstrap." >&2
say "Without these, the first heartbeat will post under a wrong/default bot persona." >&2
exit 1
