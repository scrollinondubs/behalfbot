#!/usr/bin/env bash
# gather-launchd-watchdog-health.sh - verify critical LaunchAgents are loaded.
#
# Returns JSON with a count of missing critical agents and the list of
# missing Labels. The threshold-condition fires when count > 0, prompting
# the installer's agent to investigate and re-bootstrap.
#
# Background: an installer's persistent watchdogs / daemons can silently
# unload from `launchctl` after a session cycle, reboot, or stale
# subprocess teardown. There's no native signal — the agent is just gone.
# This gather catches that class of failure by polling
# `launchctl print gui/$UID/<label>` for each agent in a configured list.
#
# Configuration:
#   CHASSIS_WATCHDOG_CRITICAL_AGENTS  Comma- or newline-separated list of
#                                     LaunchAgent labels to monitor. If
#                                     unset or empty, this gather emits
#                                     count=0 and no-ops — installers
#                                     opt in by populating the list.
#
#                                     Example:
#                                     export CHASSIS_WATCHDOG_CRITICAL_AGENTS="\
#                                     com.acme.heartbeat-reconciler,\
#                                     com.acme.briefing-server"
#
# This script is macOS-only. Linux/systemd installs need a parallel
# `gather-systemd-watchdog-health.sh` that polls
# `systemctl --user is-active <unit>` instead.
#
# V1 reference: <v1-reference-install> `scripts/gather-launchd-watchdog-health.sh`.

set -euo pipefail

# Normalize agent list: accept comma OR newline separation, strip blanks.
RAW="${CHASSIS_WATCHDOG_CRITICAL_AGENTS:-}"
if [[ -z "${RAW// }" ]]; then
  printf '{"count": 0, "missing": []}\n'
  exit 0
fi

# Replace commas with newlines, then iterate.
AGENTS=()
while IFS= read -r line; do
  line="${line## }"
  line="${line%% }"
  [[ -n "$line" ]] && AGENTS+=("$line")
done <<< "${RAW//,/$'\n'}"

if (( ${#AGENTS[@]} == 0 )); then
  printf '{"count": 0, "missing": []}\n'
  exit 0
fi

UID_VAL="$(id -u)"
MISSING=()

for agent in "${AGENTS[@]}"; do
  if ! launchctl print "gui/${UID_VAL}/${agent}" >/dev/null 2>&1; then
    MISSING+=("$agent")
  fi
done

COUNT=${#MISSING[@]}

if (( COUNT > 0 )); then
  printf '{"count": %d, "missing": [%s]}\n' \
    "$COUNT" \
    "$(printf '"%s",' "${MISSING[@]}" | sed 's/,$//')"
else
  printf '{"count": 0, "missing": []}\n'
fi
