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
#     "expected_status_codes": [200, 204]
#   }
#
# `timeout_seconds` and `expected_status_codes` are both optional with
# sensible defaults (10s, [200] respectively). Each service URL is
# probed with `curl -sf` so any 4xx/5xx counts as a failure; if you
# need broader acceptance, override `expected_status_codes`.
#
# The 10s default is intentionally generous: cloudflared-tunneled and
# other reverse-proxied endpoints can take >5s under brief network
# jitter, and a noisy false-positive alert (followed by self-recovery
# on the next tick) is worse than waiting an extra few seconds.
# Installers with strict latency targets can lower it per-config.
#
# Output (gather JSON contract):
#   {
#     "count": N,                            # failing services count
#     "issues": ["n8n_unreachable_503", ...],
#     "checked": N_total,
#     "ts_utc": "2026-05-21T13:00:00Z"
#   }

set -euo pipefail

: "${CHASSIS_HOME:?CHASSIS_HOME must be set (chassis dispatcher exports it)}"

ENDPOINTS_FILE="${CHASSIS_HOME}/scheduled-tasks/service-endpoints.json"

if [[ ! -f "$ENDPOINTS_FILE" ]]; then
    # Heartbeat configured to use this script but no endpoints file present.
    # Treat as a config error and emit count=1 so the customer notices.
    echo '{"count": 1, "issues": ["service-endpoints.json not found"]}'
    exit 0
fi

TIMEOUT_S=$(jq -r '.timeout_seconds // 10' "$ENDPOINTS_FILE")
ACCEPTED_CODES=$(jq -r '.expected_status_codes // [200] | map(tostring) | join(",")' "$ENDPOINTS_FILE")

issues=()
checked=0

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

    # Match against accepted codes list. 000 = connection failure / timeout.
    if [[ "$http_code" == "000" ]]; then
        issues+=("${name}_unreachable")
    elif ! echo ",$ACCEPTED_CODES," | grep -q ",$http_code,"; then
        issues+=("${name}_http_${http_code}")
    fi
done < <(jq -r '.services[]? | [.name, .url] | @tsv' "$ENDPOINTS_FILE")

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
