#!/bin/bash
# gather-service-health.sh - HTTPS endpoint health probe for chassis installs.
#
# Heartbeat-compatible gather script. Reads a list of name+url pairs from
# `$CHASSIS_HOME/scheduled-tasks/service-endpoints.json` and probes each
# endpoint with `curl -sf --max-time TIMEOUT_S`. Aggregates failures into
# the standard gather JSON contract.
#
# Replaces the SSH-into-host docker-ps pattern from the V1 reference
# install (<v1-reference-install>#605 follow-up). Reasons HTTP > SSH for
# health checks:
#   - No private key bind-mount into the container (no credential
#     blast-radius expansion).
#   - Service-level signal: HTTP 200 means the service is actually
#     serving, not just "host is reachable".
#   - Portable across installer infrastructures (any installer with
#     HTTPS-exposed services can use this; SSH pattern only works when
#     the installer has SSH-accessible boxes).
#   - Generic chassis image stays lean (no openssh-client layer).
#
# Endpoints file format (`scheduled-tasks/service-endpoints.json`):
#   {
#     "services": [
#       {"name": "n8n",         "url": "https://n8n.example.com/healthz"},
#       {"name": "siyuan",      "url": "https://siyuan.example.com/api/system/version"},
#       {"name": "vaultwarden", "url": "https://vault.example.com/alive"}
#     ],
#     "timeout_seconds": 10,
#     "expected_status_codes": [200, 204],
#     "min_consecutive_failures": 2
#   }
#
# `timeout_seconds`, `expected_status_codes`, and `min_consecutive_failures`
# are all optional with sensible defaults (10s, [200], 1 respectively).
# Each service URL is probed with `curl -sf` so any 4xx/5xx counts as a
# failure; if you need broader acceptance, override `expected_status_codes`.
#
# `min_consecutive_failures` adds hysteresis: a service must fail the probe
# this many ticks in a row before it contributes to the gather `count` /
# `issues` array. Default 1 = fire on first failure (legacy behavior). Set
# to 2 (or higher) to suppress transient blips that self-recover by the
# next tick. State is persisted per-service in `service-health-state.json`
# alongside the endpoints file; a single successful probe resets the
# counter. Probes still run every tick — only the *reporting* is gated.
#
# The 10s default is intentionally generous: cloudflared-tunneled and
# other reverse-proxied endpoints can take >5s under brief network
# jitter, and a noisy false-positive alert (followed by self-recovery
# on the next tick) is worse than waiting an extra few seconds.
# Installers with strict latency targets can lower it per-config.
#
# Output (gather JSON contract):
#   {
#     "count": N,                            # reportable failing services
#     "issues": ["n8n_unreachable_503", ...],
#     "checked": N_total,
#     "ts_utc": "2026-05-21T13:00:00Z"
#   }

set -euo pipefail

: "${CHASSIS_HOME:?CHASSIS_HOME must be set (chassis dispatcher exports it)}"

ENDPOINTS_FILE="${CHASSIS_HOME}/scheduled-tasks/service-endpoints.json"
STATE_FILE="${CHASSIS_HOME}/scheduled-tasks/service-health-state.json"

if [[ ! -f "$ENDPOINTS_FILE" ]]; then
    # Heartbeat configured to use this script but no endpoints file present.
    # Treat as a config error and emit count=1 so the customer notices.
    echo '{"count": 1, "issues": ["service-endpoints.json not found"]}'
    exit 0
fi

TIMEOUT_S=$(jq -r '.timeout_seconds // 10' "$ENDPOINTS_FILE")
ACCEPTED_CODES=$(jq -r '.expected_status_codes // [200] | map(tostring) | join(",")' "$ENDPOINTS_FILE")
MIN_CONSECUTIVE=$(jq -r '.min_consecutive_failures // 1' "$ENDPOINTS_FILE")

# Initialize state file on first run so jq reads/writes don't fail.
if [[ ! -f "$STATE_FILE" ]]; then
    echo '{}' > "$STATE_FILE"
fi

issues=()
checked=0
# Per-tick state updates accumulate in a jq filter applied once at the end
# (avoids N temp-file rewrites of the state file inside the loop).
state_updates=()

while IFS=$'\t' read -r name url; do
    [[ -z "$name" || -z "$url" ]] && continue
    checked=$((checked + 1))

    # Probe with curl. Capture HTTP status code separately so we can
    # report which code came back, not just "failed".
    #
    # NOTE: curl's `-w '%{http_code}'` already writes `000` (no trailing
    # newline) to stdout on connection failure, AND exits non-zero. An
    # earlier `|| echo "000"` fallback inside the $() ran on that same
    # non-zero exit and appended another `000\n`, producing http_code
    # values like `000000` (six zeros). That broke the `== "000"` check
    # below, so timeouts fell through to the elif branch and emitted
    # garbage issue codes like `siyuan_http_000000`. The fallback is now
    # outside the $() and only applies if curl somehow wrote nothing.
    http_code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time "$TIMEOUT_S" "$url" 2>/dev/null) || true
    http_code="${http_code:-000}"

    # Classify probe result and build the candidate issue label.
    issue=""
    if [[ "$http_code" == "000" ]]; then
        issue="${name}_unreachable"
    elif ! echo ",$ACCEPTED_CODES," | grep -q ",$http_code,"; then
        issue="${name}_http_${http_code}"
    fi

    if [[ -z "$issue" ]]; then
        # Success — reset the consecutive-failure counter.
        state_updates+=(".\"${name}\" = {\"consecutive_failures\": 0, \"last_issue\": null}")
    else
        # Failure — bump counter; only surface to gather output once the
        # counter meets the configured min_consecutive_failures threshold.
        prior=$(jq -r --arg n "$name" '.[$n].consecutive_failures // 0' "$STATE_FILE")
        # Defensive parse: state file could have been hand-edited.
        if ! [[ "$prior" =~ ^[0-9]+$ ]]; then
            prior=0
        fi
        new_count=$((prior + 1))
        state_updates+=(".\"${name}\" = {\"consecutive_failures\": ${new_count}, \"last_issue\": \"${issue}\"}")

        if [[ $new_count -ge $MIN_CONSECUTIVE ]]; then
            issues+=("$issue")
        fi
    fi
done < <(jq -r '.services[]? | [.name, .url] | @tsv' "$ENDPOINTS_FILE")

# Apply all state updates atomically.
if [[ ${#state_updates[@]} -gt 0 ]]; then
    filter=$(printf '%s | ' "${state_updates[@]}")
    filter="${filter% | }"
    tmp="${STATE_FILE}.tmp"
    jq "$filter" "$STATE_FILE" > "$tmp" && mv "$tmp" "$STATE_FILE"
fi

count=${#issues[@]}
ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)

if [[ $count -eq 0 ]]; then
    issues_json="[]"
else
    issues_json=$(printf '%s\n' "${issues[@]}" | jq -R . | jq -s .)
fi

jq -n \
    --argjson count "$count" \
    --argjson issues "$issues_json" \
    --argjson checked "$checked" \
    --arg ts "$ts" \
    '{count: $count, issues: $issues, checked: $checked, ts_utc: $ts}'
