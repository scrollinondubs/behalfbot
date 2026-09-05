#!/usr/bin/env bash
# gather-credential-expiry.sh - warn BEFORE a credential expires, on the
# surfaces the operator can still be reached on.
#
# The failure this exists for (new-jaxity#550)
# ============================================
# Between 2026-09-01 and 2026-09-05 the reference install's operator was
# off-grid and four credentials expired within days of each other. Every
# channel the assistant had for saying so depended on one of them:
#
#   - the Tailscale node key hit its 180-day default expiry on ~09-01. The
#     ssh hostname resolves to the Tailscale IP, so that killed ssh AND VNC
#     in one move;
#   - the macOS Keychain entry `Claude Code-credentials` lost
#     `claudeAiOauth.accessToken` on 2026-09-01 at ~10:06Z, which kills the
#     HOST interactive Claude session (the container was unaffected - it
#     reads `~/.claude/.credentials.json` and refreshes off the refresh
#     token);
#   - an iCloud trust token expired 09-04.
#
# Nothing warned. `sync-claude-oauth-bridge.sh` had in fact DETECTED the
# Keychain loss and logged the same WARN 288 times over four days into a
# file nobody reads. A monitor that fires into a log is not a monitor.
#
# This gather is the warning. It is free - no model call, no paid API, no
# network beyond a local `tailscale status` - and it alerts at T-7d and
# T-1d before an expiry, immediately on anything already expired or
# missing, and stays quiet in between.
#
# WHERE THIS CAN SEE WHAT (read before trusting it)
# =================================================
# Two of the built-in checks are HOST-only:
#
#   | check                     | host | chassis container  |
#   |---------------------------|------|--------------------|
#   | tailscale-self / -peer-*  | yes  | no (no tailscaled) |
#   | claude-keychain           | yes  | no (no Keychain)   |
#   | claude-credentials-file   | yes  | yes (bind mount)   |
#   | claude-token-refresh      | yes  | yes                |
#   | drop-in checks            | both | both               |
#
# The dispatcher runs INSIDE the container, so the heartbeat registration
# alone covers only the bottom three rows. On a containerized install the
# host-side runner `credential-expiry-alert.sh` is the load-bearing half -
# it runs this same script from a LaunchAgent and posts straight to the ops
# webhook with NO model call, which also means it still works when the
# expired credential is Claude's own. See
# `chassis/docs/heartbeats/credential-expiry.md`.
#
# Running in both places is safe: suppression state is per-check, not
# per-run, so a run that cannot see the Keychain leaves that check's record
# alone rather than fighting the other runner over a whole-run fingerprint.
#
# REPEAT SUPPRESSION - why the fingerprint holds no day count
# ==========================================================
# Each check resolves to a STAGE, one of `ok` / `t7` / `t1` / `warn` /
# `expired` / `missing` / `unknown`. The stage is what gets recorded and
# compared, never `days_remaining`. A fingerprint carrying a volatile
# counter never matches its own previous value, so the cooldown never
# applies and the alert fires every single tick - a bug this codebase has
# shipped before. `days_remaining` is emitted in the payload for the prompt
# to read and is deliberately excluded from both the state record and
# `alert_signature`.
#
# A check fires when:
#   - its stage changed since the last recorded alert, so T-7 fires once,
#     then silence, then T-1 fires once, OR
#   - its stage is `expired` / `missing` / `unknown` and the last alert is
#     older than CHASSIS_CREDENTIAL_REALERT_DAYS (default 3). Those states
#     do not self-resolve, and an unanswered notification is not a
#     dismissal.
# `t7` / `t1` / `warn` never re-nag on the cooldown: a 3-day re-nag on t7
# would fire again at T-4, which is exactly the daily nagging this design
# rules out.
# A check returning to `ok` clears its record, so a later re-degrade fires
# again from scratch.
#
# Configuration
# =============
#   CHASSIS_CREDENTIAL_CHECKS          Comma list of built-in check groups to
#                                      run. Default `tailscale,claude`. Set
#                                      to `claude` on a host with no tailnet,
#                                      or to the empty string to run drop-ins
#                                      only.
#   CHASSIS_CREDENTIAL_CHECKS_DIR      Drop-in directory. Default
#                                      $CUSTOMER_HOME/scheduled-tasks/credential-checks.d
#   CHASSIS_CREDENTIAL_STATE           State file. Default
#                                      $CUSTOMER_HOME/scheduled-tasks/credential-expiry-state.json
#   CHASSIS_CREDENTIAL_WARN_DAYS       First threshold, default 7
#   CHASSIS_CREDENTIAL_URGENT_DAYS     Second threshold, default 1
#   CHASSIS_CREDENTIAL_REALERT_DAYS    Re-nag window for the states that do
#                                      not self-resolve, default 3
#   CHASSIS_CREDENTIAL_TAILSCALE_BIN   Explicit tailscale binary path
#   CHASSIS_CREDENTIAL_TAILSCALE_PEERS Comma list of peer DNS labels or
#                                      hostnames to check. Default: every
#                                      peer that has a key expiry.
#   CHASSIS_CREDENTIAL_KEYCHAIN_SERVICE  Default `Claude Code-credentials`
#   KEYCHAIN_ACCOUNT                   Keychain account, default $USER (the
#                                      same var sync-claude-oauth-bridge.sh reads)
#   CLAUDE_CREDENTIALS_FILE            Default $HOME/.claude/.credentials.json
#   CHASSIS_CREDENTIAL_REFRESH_GRACE_HOURS  How long the Claude access token
#                                      may sit expired before that counts as a
#                                      dead refresh loop. Default 24.
#
# Extending it: drop-in checks
# ============================
# Any executable file in the drop-in directory is run and its stdout parsed
# as either a JSON array of check records or one record per line. A record
# is:
#
#   {"id": "icloud-trust-token",
#    "label": "iCloud trust token",
#    "expires_at": 1789200000,          # epoch s, epoch ms, or ISO8601
#    "detail": "renewed by scripts/icloud-login.sh"}
#
# or, when the script would rather decide for itself:
#
#   {"id": "icloud-trust-token", "state": "expired",
#    "detail": "session file older than 60 days"}
#
# Valid `state` values are ok / warn / expired / missing / unknown; anything
# else is clamped to `unknown`. A drop-in that exits non-zero or emits
# unparseable output is itself reported as `unknown` rather than silently
# dropped - a credential check that cannot run is a credential nobody is
# watching. Keep drop-ins fast; this gather runs on every heartbeat tick.
#
# Gather JSON contract
# ====================
#   {"count": N, "issues": [...], "checked": M, "status": "...",
#    "alert_signature": "...", "ts_utc": "...",
#    "credentials": [...], "unhealthy": [...], "skipped": [...]}
#
# `count` is the number of checks FIRING this run, not the number unhealthy:
# a known-expired credential already alerted about inside the cooldown
# reports count 0 with status `suppressed` and still lists itself under
# `unhealthy`.
#
# Emits NO secrets. Token presence, epochs, hostnames and states only - the
# dispatcher logs gather stdout verbatim.
#
# Always exits 0. The dispatcher treats a non-zero gather exit as count=0,
# so a check that exited non-zero on failure would go silent exactly when it
# matters.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/_env.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/_env.sh" 2>/dev/null || true
fi
: "${CUSTOMER_HOME:=${CHASSIS_HOME:-${HOME}/.behalfbot}}"

TS_NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
NOW=$(date -u +%s)

WARN_DAYS="${CHASSIS_CREDENTIAL_WARN_DAYS:-7}"
URGENT_DAYS="${CHASSIS_CREDENTIAL_URGENT_DAYS:-1}"
REALERT_DAYS="${CHASSIS_CREDENTIAL_REALERT_DAYS:-3}"
REFRESH_GRACE_HOURS="${CHASSIS_CREDENTIAL_REFRESH_GRACE_HOURS:-24}"
ENABLED="${CHASSIS_CREDENTIAL_CHECKS-tailscale,claude}"
DROPIN_DIR="${CHASSIS_CREDENTIAL_CHECKS_DIR:-${CUSTOMER_HOME}/scheduled-tasks/credential-checks.d}"
STATE_FILE="${CHASSIS_CREDENTIAL_STATE:-${CUSTOMER_HOME}/scheduled-tasks/credential-expiry-state.json}"
KEYCHAIN_SERVICE="${CHASSIS_CREDENTIAL_KEYCHAIN_SERVICE:-Claude Code-credentials}"
KEYCHAIN_ACCOUNT="${KEYCHAIN_ACCOUNT:-${USER:-$(id -un 2>/dev/null || echo unknown)}}"
CRED_FILE="${CLAUDE_CREDENTIALS_FILE:-${HOME}/.claude/.credentials.json}"

if ! command -v jq >/dev/null 2>&1; then
    # No jq means no JSON, and a bare line falls through the dispatcher's
    # line-count fallback and fires every tick. Emit the contract by hand and
    # report the broken monitor as the finding.
    printf '{"count": 1, "issues": ["gather_unavailable_no_jq"], "checked": 0, "status": "unavailable", "alert_signature": "no_jq", "credentials": [], "unhealthy": ["gather_unavailable_no_jq"], "skipped": [], "ts_utc": "%s"}\n' "$TS_NOW"
    exit 0
fi

CHECKS=()
SKIPPED=()

add_check() {
    # $1 id, $2 label, $3 expires_at (epoch seconds, or empty), $4 state (or
    # empty to derive the stage from the expiry), $5 detail
    local exp_json="null"
    [[ -n "$3" ]] && exp_json="$3"
    CHECKS+=("$(jq -nc \
        --arg id "$1" --arg label "$2" --argjson exp "$exp_json" \
        --arg st "$4" --arg detail "$5" \
        '{id: $id, label: $label, expires_at: $exp,
          state: (if $st == "" then null else $st end), detail: $detail}')")
}

add_skip() {
    SKIPPED+=("$(jq -nc --arg check "$1" --arg reason "$2" '{check: $check, reason: $reason}')")
}

check_enabled() {
    [[ ",${ENABLED}," == *",$1,"* ]]
}

# RFC3339 -> epoch seconds, via jq rather than date(1): `date -j -f` (BSD) and
# `date -d` (GNU) disagree on everything, and Go emits RFC3339Nano so the
# fractional seconds have to come off first either way.
iso_to_epoch() {
    jq -rn --arg t "$1" '($t | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601)' 2>/dev/null || true
}

# --- Built-in: Tailscale -----------------------------------------------------
#
# `Self.KeyExpiry` absent or null means key expiry is DISABLED for this node,
# which is the healthy state for an always-on host and the fix the reference
# install applied after the outage. Peers are checked too: the laptop or the
# phone whose key expires is the operator's way back in.
if check_enabled tailscale; then
    TS_BIN="${CHASSIS_CREDENTIAL_TAILSCALE_BIN:-}"
    TS_REASON="tailscale not installed"
    if [[ -n "$TS_BIN" && ! -x "$TS_BIN" ]]; then
        # A configured path that is not there is a wiring mistake, and saying
        # so beats reporting the generic not-installed reason.
        TS_REASON="configured tailscale binary ${TS_BIN} is not executable"
        TS_BIN=""
    elif [[ -z "$TS_BIN" ]]; then
        if command -v tailscale >/dev/null 2>&1; then
            TS_BIN="$(command -v tailscale)"
        elif [[ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]]; then
            # The App Store build ships no CLI on PATH, and launchd's PATH
            # would not have picked it up even if it did.
            TS_BIN="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
        fi
    fi

    if [[ -z "$TS_BIN" ]]; then
        add_skip "tailscale" "$TS_REASON"
    else
        TS_JSON="$("$TS_BIN" status --json 2>/dev/null)" || TS_JSON=""
        if [[ -z "$TS_JSON" ]] || ! jq -e . >/dev/null 2>&1 <<<"$TS_JSON"; then
            add_skip "tailscale" "tailscale status unavailable (daemon not running)"
        else
            BACKEND="$(jq -r '.BackendState // "unknown"' <<<"$TS_JSON")"
            case "$BACKEND" in
                NeedsLogin)
                    # This is what an expired node key looks like from the CLI.
                    # NOT a graceful skip - it is the outage state itself.
                    add_check "tailscale-self" "this node's tailscale key" "" "expired" \
                        "tailscale BackendState is NeedsLogin - node key expired or logged out, run 'tailscale up'"
                    ;;
                Running|Starting)
                    SELF_NAME="$(jq -r '.Self.HostName // "this node"' <<<"$TS_JSON")"
                    SELF_EXPIRY="$(jq -r '.Self.KeyExpiry // empty' <<<"$TS_JSON")"
                    if [[ "$(jq -r '.Self.Expired // false' <<<"$TS_JSON")" == "true" ]]; then
                        add_check "tailscale-self" "$SELF_NAME" "" "expired" \
                            "tailscale reports this node's key as expired"
                    elif [[ -z "$SELF_EXPIRY" ]]; then
                        add_check "tailscale-self" "$SELF_NAME" "" "ok" \
                            "key expiry disabled for this node (the healthy state for an always-on host)"
                    else
                        SELF_EPOCH="$(iso_to_epoch "$SELF_EXPIRY")"
                        if [[ "$SELF_EPOCH" =~ ^[0-9]+$ ]]; then
                            add_check "tailscale-self" "$SELF_NAME" "$SELF_EPOCH" "" \
                                "tailscale node key expires ${SELF_EXPIRY}; disable key expiry in the admin console for an always-on host"
                        else
                            add_check "tailscale-self" "$SELF_NAME" "" "unknown" \
                                "could not parse Self.KeyExpiry"
                        fi
                    fi

                    PEER_RECORDS="$(jq -c \
                        --arg filter "${CHASSIS_CREDENTIAL_TAILSCALE_PEERS:-}" '
                        ($filter | ascii_downcase | split(",")
                            | map(sub("^ +"; "") | sub(" +$"; ""))
                            | map(select(length > 0))) as $want
                        | (.Peer // {}) | [.[]]
                        | map(select(.KeyExpiry != null))
                        | map(. as $p
                            | (($p.DNSName // "") | split(".")[0]) as $dns
                            | (if ($dns | length) > 0 then $dns
                               else (($p.HostName // "peer") | ascii_downcase
                                     | gsub("[^a-z0-9]+"; "-")
                                     | sub("^-+"; "") | sub("-+$"; "")) end) as $slug
                            | {id: ("tailscale-peer-" + $slug),
                               slug: $slug,
                               label: ($p.HostName // $slug),
                               expires_at: ($p.KeyExpiry | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601),
                               state: (if ($p.Expired // false) then "expired" else null end),
                               detail: ("tailscale node key for " + ($p.HostName // $slug)
                                        + " (" + ($p.OS // "unknown os") + ")")})
                        | (if ($want | length) == 0 then .
                           else map(select((($want | index(.slug)) != null)
                                        or (($want | index(.label | ascii_downcase)) != null))) end)
                        | map(del(.slug))
                        | .[]' <<<"$TS_JSON" 2>/dev/null)" || PEER_RECORDS=""
                    if [[ -n "$PEER_RECORDS" ]]; then
                        while IFS= read -r rec; do
                            [[ -z "$rec" ]] && continue
                            CHECKS+=("$rec")
                        done <<<"$PEER_RECORDS"
                    fi
                    ;;
                *)
                    add_skip "tailscale" "tailscale backend state is ${BACKEND}"
                    ;;
            esac
        fi
    fi
fi

# --- Built-in: Claude credentials -------------------------------------------
#
# Three separate checks, because the outage proved they fail independently.
# Watching only the file would have shown green through all four days.
KC_ACCESS_EXP=""
FILE_ACCESS_EXP=""

if check_enabled claude; then
    # Gate on the binary, not on `uname`: Linux skips cleanly either way, and
    # this lets the test suite stub `security` on PATH.
    if ! command -v security >/dev/null 2>&1; then
        add_skip "claude-keychain" "no macOS keychain on this host"
    else
        KC_ERR="$(mktemp)"
        if KC_JSON="$(security find-generic-password -s "$KEYCHAIN_SERVICE" -a "$KEYCHAIN_ACCOUNT" -w 2>"$KC_ERR")"; then
            KC_ACCESS="$(jq -r '.claudeAiOauth.accessToken // empty' <<<"$KC_JSON" 2>/dev/null || true)"
            if [[ -z "$KC_ACCESS" ]]; then
                # THE highest-value signal in this script. First occurrence, no
                # threshold: this is the exact state the host interactive
                # session sat in for four days.
                add_check "claude-keychain" "Claude Code keychain entry" "" "missing" \
                    "keychain entry '${KEYCHAIN_SERVICE}' exists but carries no claudeAiOauth.accessToken - host interactive 'claude' cannot authenticate until someone runs /login"
            else
                KC_REFRESH_EXP="$(jq -r '.claudeAiOauth.refreshTokenExpiresAt // empty' <<<"$KC_JSON" 2>/dev/null || true)"
                KC_ACCESS_EXP="$(jq -r '.claudeAiOauth.expiresAt // empty' <<<"$KC_JSON" 2>/dev/null || true)"
                if [[ "$KC_REFRESH_EXP" =~ ^[0-9]+$ ]]; then
                    add_check "claude-keychain" "Claude Code keychain entry" \
                        "$(( KC_REFRESH_EXP / 1000 ))" "" \
                        "claudeAiOauth.refreshToken expiry for the host interactive session"
                else
                    # Older Claude Code builds wrote no refreshTokenExpiresAt.
                    # Silent ok, not `unknown` - otherwise every install on such
                    # a build alerts about a perfectly healthy credential.
                    add_check "claude-keychain" "Claude Code keychain entry" "" "ok" \
                        "accessToken present; this build records no refreshTokenExpiresAt"
                fi
            fi
        else
            KC_MSG="$(tr -d '\n' <"$KC_ERR" 2>/dev/null)"
            if grep -qi "could not be found" "$KC_ERR" 2>/dev/null; then
                add_check "claude-keychain" "Claude Code keychain entry" "" "missing" \
                    "no keychain entry '${KEYCHAIN_SERVICE}' for account '${KEYCHAIN_ACCOUNT}' - host interactive 'claude' cannot authenticate until someone runs /login"
            elif grep -qi "interaction is not allowed" "$KC_ERR" 2>/dev/null; then
                # Not a lost token. This is the login keychain being unreachable
                # from launchd's Background session, and the fix is the launchd
                # domain rather than a re-login. See docs/launchd-domains.md.
                add_check "claude-keychain" "Claude Code keychain entry" "" "unknown" \
                    "keychain read refused: user interaction is not allowed. The caller sits in launchd's Background session (a LaunchDaemon) and cannot unlock the login keychain - see docs/launchd-domains.md"
            else
                add_check "claude-keychain" "Claude Code keychain entry" "" "unknown" \
                    "keychain read failed: ${KC_MSG:-no error text}"
            fi
        fi
        rm -f "$KC_ERR"
    fi

    if [[ ! -f "$CRED_FILE" ]]; then
        add_check "claude-credentials-file" "claude credentials file" "" "missing" \
            "${CRED_FILE} does not exist - the chassis container has no way to authenticate"
    else
        FILE_ACCESS="$(jq -r '.claudeAiOauth.accessToken // empty' "$CRED_FILE" 2>/dev/null || true)"
        if [[ -z "$FILE_ACCESS" ]]; then
            add_check "claude-credentials-file" "claude credentials file" "" "missing" \
                "${CRED_FILE} carries no claudeAiOauth.accessToken - the chassis container cannot authenticate"
        else
            FILE_REFRESH_EXP="$(jq -r '.claudeAiOauth.refreshTokenExpiresAt // empty' "$CRED_FILE" 2>/dev/null || true)"
            FILE_ACCESS_EXP="$(jq -r '.claudeAiOauth.expiresAt // empty' "$CRED_FILE" 2>/dev/null || true)"
            if [[ "$FILE_REFRESH_EXP" =~ ^[0-9]+$ ]]; then
                add_check "claude-credentials-file" "claude credentials file" \
                    "$(( FILE_REFRESH_EXP / 1000 ))" "" \
                    "claudeAiOauth.refreshToken expiry for the chassis container"
            else
                add_check "claude-credentials-file" "claude credentials file" "" "ok" \
                    "accessToken present; this build records no refreshTokenExpiresAt"
            fi
        fi
    fi

    # The access token lives about an hour and is refreshed automatically, so
    # putting the T-7 / T-1 ladder on it would alert permanently. What IS worth
    # alerting on is nothing having refreshed it for a long time: whichever
    # side refreshes writes a newer expiresAt, so the NEWEST of the two sitting
    # far in the past means every refresh path is dead.
    NEWEST_EXP=0
    if [[ "$KC_ACCESS_EXP" =~ ^[0-9]+$ ]] && (( KC_ACCESS_EXP / 1000 > NEWEST_EXP )); then
        NEWEST_EXP=$(( KC_ACCESS_EXP / 1000 ))
    fi
    if [[ "$FILE_ACCESS_EXP" =~ ^[0-9]+$ ]] && (( FILE_ACCESS_EXP / 1000 > NEWEST_EXP )); then
        NEWEST_EXP=$(( FILE_ACCESS_EXP / 1000 ))
    fi
    if (( NEWEST_EXP > 0 )); then
        GRACE=$(( REFRESH_GRACE_HOURS * 3600 ))
        STALE=$(( NOW - NEWEST_EXP ))
        if (( STALE > GRACE )); then
            add_check "claude-token-refresh" "claude access-token refresh loop" "" "expired" \
                "the newest Claude access token expired $(( STALE / 3600 ))h ago and nothing has refreshed it (grace ${REFRESH_GRACE_HOURS}h) - the oauth bridge or the container refresh path is dead"
        else
            add_check "claude-token-refresh" "claude access-token refresh loop" "" "ok" \
                "access token refreshed within the last ${REFRESH_GRACE_HOURS}h"
        fi
    fi
fi

# --- Drop-in checks ----------------------------------------------------------
if [[ -d "$DROPIN_DIR" ]]; then
    for dropin in "$DROPIN_DIR"/*; do
        [[ -f "$dropin" && -x "$dropin" ]] || continue
        BASE="$(basename "$dropin")"
        BASE="${BASE%.sh}"
        DROP_OUT="$("$dropin" 2>/dev/null)" || DROP_OUT=""
        DROP_RECS="$(jq -c 'if type == "array" then .[] else . end' <<<"$DROP_OUT" 2>/dev/null)" || DROP_RECS=""
        if [[ -z "$DROP_RECS" ]]; then
            add_check "$BASE" "$BASE" "" "unknown" \
                "credential check '${BASE}' failed or emitted no valid JSON"
            continue
        fi
        while IFS= read -r rec; do
            [[ -z "$rec" ]] && continue
            NORM="$(jq -c --arg base "$BASE" '
                . as $r
                | ($r.expires_at // $r.expiresAt // null) as $e
                | (if $e == null then null
                   elif ($e | type) == "string" then
                       ($e | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601)
                   elif ($e | type) == "number" then
                       (if $e > 100000000000 then ($e / 1000 | floor) else ($e | floor) end)
                   else null end) as $exp
                | {id: (($r.id // $base) | tostring),
                   label: (($r.label // $r.id // $base) | tostring),
                   expires_at: $exp,
                   state: (if ($r.state // null) == null then null else ($r.state | tostring) end),
                   detail: (($r.detail // "") | tostring)}' <<<"$rec" 2>/dev/null)" || NORM=""
            if [[ -z "$NORM" ]]; then
                add_check "$BASE" "$BASE" "" "unknown" \
                    "credential check '${BASE}' emitted a record this gather could not read"
            else
                CHECKS+=("$NORM")
            fi
        done <<<"$DROP_RECS"
    done
fi

# --- Grade, suppress, emit ---------------------------------------------------
if [[ ${#CHECKS[@]} -eq 0 ]]; then
    CHECKS_JSON="[]"
else
    CHECKS_JSON="$(printf '%s\n' "${CHECKS[@]}" | jq -s '.')"
fi
if [[ ${#SKIPPED[@]} -eq 0 ]]; then
    SKIPPED_JSON="[]"
else
    SKIPPED_JSON="$(printf '%s\n' "${SKIPPED[@]}" | jq -s '.')"
fi

STATE_JSON="{}"
if [[ -f "$STATE_FILE" ]]; then
    STATE_JSON="$(jq -c '.' "$STATE_FILE" 2>/dev/null)" || STATE_JSON="{}"
    [[ -z "$STATE_JSON" ]] && STATE_JSON="{}"
fi

GRADED="$(jq -n \
    --argjson checks "$CHECKS_JSON" \
    --argjson state "$STATE_JSON" \
    --argjson now "$NOW" \
    --argjson warn_days "$WARN_DAYS" \
    --argjson urgent_days "$URGENT_DAYS" \
    --argjson realert "$(( REALERT_DAYS * 86400 ))" '
    def clamp($s):
        if (["ok", "warn", "expired", "missing", "unknown"] | index($s)) != null
        then $s else "unknown" end;
    def stage($c):
        if ($c.state != null and $c.state != "ok") then clamp($c.state)
        elif ($c.state == "ok") then "ok"
        elif ($c.expires_at == null) then "ok"
        else (($c.expires_at - $now) / 86400) as $d
            | if $d <= 0 then "expired"
              elif $d <= $urgent_days then "t1"
              elif $d <= $warn_days then "t7"
              else "ok" end
        end;
    ($state.checks // {}) as $prev
    | ($checks | map(. as $c
        | . + {stage: stage($c),
               days_remaining: (if $c.expires_at == null then null
                                else (((($c.expires_at - $now) / 8640) | floor) / 10) end)})) as $graded
    | ($graded | map(select(.stage != "ok") | . as $c
        | ($prev[$c.id] // null) as $p
        | . + {fires: (
            if $p == null then true
            elif ($p.stage != $c.stage) then true
            elif (((["expired", "missing", "unknown"] | index($c.stage)) != null)
                  and (($now - ($p.alerted_at // 0)) >= $realert)) then true
            else false end)})) as $unhealthy
    | ($unhealthy | map(select(.fires))) as $firing
    | ($graded | reduce .[] as $c ($prev;
        if $c.stage == "ok" then del(.[$c.id])
        elif (($firing | map(.id) | index($c.id)) != null)
        then .[$c.id] = {stage: $c.stage, alerted_at: $now}
        else . end)) as $next
    | {payload: {
          count: ($firing | length),
          issues: ($firing | map(.id + "_" + .stage) | sort),
          checked: ($graded | length),
          status: (if ($firing | length) > 0 then "alerting"
                   elif ($unhealthy | length) > 0 then "suppressed"
                   else "ok" end),
          unhealthy: ($unhealthy | map(.id + "_" + .stage) | sort),
          credentials: ($graded | map(del(.state)))},
       signature_source: ($firing | map(.id + ":" + .stage) | sort | join("|")),
       state: {checks: $next, updated_at: $now}}')" || GRADED=""

if [[ -z "$GRADED" ]]; then
    jq -n --arg ts "$TS_NOW" --argjson skipped "$SKIPPED_JSON" \
        '{count: 1, issues: ["gather_grading_failed"], checked: 0,
          status: "unavailable", unhealthy: ["gather_grading_failed"],
          credentials: [], alert_signature: "grading_failed",
          skipped: $skipped, ts_utc: $ts}'
    exit 0
fi

SIG_SOURCE="$(jq -r '.signature_source' <<<"$GRADED")"
if [[ -z "$SIG_SOURCE" ]]; then
    SIG="no_alert"
elif command -v sha256sum >/dev/null 2>&1; then
    SIG="$(printf '%s' "$SIG_SOURCE" | sha256sum | cut -c1-16)"
else
    SIG="$(printf '%s' "$SIG_SOURCE" | shasum -a 256 | cut -c1-16)"
fi

# Atomic write: the host runner and the in-container heartbeat can both reach
# this file through the same bind mount.
mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null || true
STATE_TMP="${STATE_FILE}.$$.tmp"
if jq -c '.state' <<<"$GRADED" > "$STATE_TMP" 2>/dev/null; then
    mv "$STATE_TMP" "$STATE_FILE" 2>/dev/null || rm -f "$STATE_TMP"
else
    rm -f "$STATE_TMP"
fi

jq -n \
    --argjson payload "$(jq -c '.payload' <<<"$GRADED")" \
    --arg sig "$SIG" \
    --argjson skipped "$SKIPPED_JSON" \
    --arg ts "$TS_NOW" \
    '$payload + {alert_signature: $sig, skipped: $skipped, ts_utc: $ts}'

exit 0
