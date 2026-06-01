#!/usr/bin/env bash
# check-backup-posture.sh - Verify the S3 backup security posture is intact.
#
# Walks the AWS bucket + IAM config and confirms the chassis backup-bucket
# invariants are still in place. Drift in any of these silently weakens
# DR resilience without anyone noticing - this script is the nightly
# "are we still secure" check, paired with chassis/scripts/backup-to-s3.sh.
#
# Invariants verified:
#   1. Bucket versioning ON                  (immutability foundation)
#   2. Lifecycle has noncurrent-version expiration (versioning needs pairing
#      with cleanup or storage grows unboundedly)
#   3. Object Lock enabled with COMPLIANCE mode + retention >= LOCK_MIN_DAYS
#      (optional - treated as "not yet enabled" not "drift" when disabled;
#      flagged only if it WAS on and is now off)
#   4. WRITE user policy contains required action (s3:PutObject) and LACKS
#      forbidden actions (Get, Delete, DeleteVersion, PutBucketVersioning,
#      PutLifecycleConfiguration, PutBucketPolicy, s3:*).
#   5. AWS_BACKUP_READ_* not in $CHASSIS_HOME/.env (post-host-hygiene check;
#      read creds should only be hydrated at restore time, not nightly)
#
# Output:
#   - JSON to stdout (gather contract); count=0 = no drift, count>0 = drift
#   - Optional human-readable summary to stderr (--verbose)
#   - Exit 0 if the gather completed (regardless of drift); exit 2 on script
#     failure. The drift signal travels through the JSON `count` field per
#     the heartbeat gather contract — encoding it in the exit code as well
#     made the dispatcher suppress the JSON entirely and report
#     "gather_failed" instead of firing the drift alert. See
#     <v1-reference-install>#698.
#
# Auth:
#   Uses AWS_BACKUP_AUDIT_* creds (audit-only IAM user with read-only S3 +
#   limited iam:GetUserPolicy / iam:ListUserPolicies perms). Falls back to
#   AWS_BACKUP_READ_* with a warning. Never accepts WRITE creds here -
#   audit context should be deliberately blast-radius-limited.
#
# Required env (read from $CHASSIS_HOME/.env):
#   S3_BACKUP_BUCKET           - bucket to audit
#   S3_BACKUP_REGION           - bucket region
#   AWS_BACKUP_AUDIT_ACCESS_KEY_ID / _SECRET_ACCESS_KEY
#     (or AWS_BACKUP_READ_* as fallback)
#
# Optional env:
#   WRITE_USER_NAME            - IAM user name to audit (default
#                                "chassis-backup-write")
#   LOCK_MIN_DAYS              - Object-Lock retention floor (default 30)
#
# Usage:
#   bash chassis/scripts/check-backup-posture.sh
#   bash chassis/scripts/check-backup-posture.sh --verbose
#   bash chassis/scripts/check-backup-posture.sh --no-iam   # when audit user
#                                                            # lacks iam perms

set -uo pipefail

: "${CHASSIS_HOME:?CHASSIS_HOME must be set (chassis dispatcher exports it)}"

LOCK_MIN_DAYS="${LOCK_MIN_DAYS:-30}"
WRITE_USER_NAME="${WRITE_USER_NAME:-chassis-backup-write}"

VERBOSE=0
SKIP_IAM=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --verbose) VERBOSE=1; shift ;;
        --no-iam)  SKIP_IAM=1; shift ;;
        *) echo "Unknown arg: $1" >&2; exit 2 ;;
    esac
done

# Load .env.
if [[ -f "${CHASSIS_HOME}/.env" ]]; then
    set -a
    # shellcheck disable=SC1090,SC1091
    source "${CHASSIS_HOME}/.env" 2>/dev/null
    set +a
fi

: "${S3_BACKUP_BUCKET:?S3_BACKUP_BUCKET must be set}"
: "${S3_BACKUP_REGION:?S3_BACKUP_REGION must be set}"

# Audit creds preferred; fall back to read with a warning.
AUDIT_KEY_ID=""
AUDIT_SECRET=""
AUDIT_VIA="audit"
if [[ -n "${AWS_BACKUP_AUDIT_ACCESS_KEY_ID:-}" && -n "${AWS_BACKUP_AUDIT_SECRET_ACCESS_KEY:-}" ]]; then
    AUDIT_KEY_ID="$AWS_BACKUP_AUDIT_ACCESS_KEY_ID"
    AUDIT_SECRET="$AWS_BACKUP_AUDIT_SECRET_ACCESS_KEY"
elif [[ -n "${AWS_BACKUP_READ_ACCESS_KEY_ID:-}" && -n "${AWS_BACKUP_READ_SECRET_ACCESS_KEY:-}" ]]; then
    AUDIT_KEY_ID="$AWS_BACKUP_READ_ACCESS_KEY_ID"
    AUDIT_SECRET="$AWS_BACKUP_READ_SECRET_ACCESS_KEY"
    AUDIT_VIA="read-fallback"
    [[ $VERBOSE -eq 1 ]] && echo "warn: using READ creds (no AUDIT creds set); provision a dedicated audit IAM user" >&2
else
    echo "ERROR: neither AWS_BACKUP_AUDIT_* nor AWS_BACKUP_READ_* set in env" >&2
    exit 2
fi

aws_audit() {
    AWS_ACCESS_KEY_ID="$AUDIT_KEY_ID" \
    AWS_SECRET_ACCESS_KEY="$AUDIT_SECRET" \
    AWS_DEFAULT_REGION="$S3_BACKUP_REGION" \
    aws "$@"
}

FINDINGS=()
DRIFT=0

note_finding() {
    local key="$1"
    local val="$2"
    FINDINGS+=("$(printf '"%s": %s' "$key" "$val")")
}

flag_drift() {
    DRIFT=1
    local msg="$1"
    [[ $VERBOSE -eq 1 ]] && echo "drift: $msg" >&2
    FINDINGS+=("$(printf '"drift_%d": "%s"' "${#FINDINGS[@]}" "$msg")")
}

# 1. Versioning.
VERS_RAW=$(aws_audit s3api get-bucket-versioning --bucket "$S3_BACKUP_BUCKET" 2>/dev/null || echo '{}')
VERS_STATUS=$(echo "$VERS_RAW" | jq -r '.Status // "Unknown"')
note_finding "versioning" "\"$VERS_STATUS\""
if [[ "$VERS_STATUS" != "Enabled" ]]; then
    flag_drift "bucket versioning is '$VERS_STATUS', expected 'Enabled'"
fi

# 2. Lifecycle - require a noncurrent-version expiration rule.
LIFECYCLE_RAW=$(aws_audit s3api get-bucket-lifecycle-configuration --bucket "$S3_BACKUP_BUCKET" 2>/dev/null || echo '{}')
HAS_NONCURRENT=$(echo "$LIFECYCLE_RAW" | jq '[.Rules[]? | select(.NoncurrentVersionExpiration != null)] | length' 2>/dev/null || echo 0)
note_finding "lifecycle_noncurrent_rule_count" "$HAS_NONCURRENT"
if [[ "$HAS_NONCURRENT" -lt 1 ]]; then
    flag_drift "no NoncurrentVersionExpiration rule on bucket lifecycle (versioning needs paired cleanup)"
fi

# 3. Object Lock - optional. Report state; flag drift only if it WAS enabled
# and is now off, or if enabled but mode/days don't meet the floor.
OL_RAW=$(aws_audit s3api get-object-lock-configuration --bucket "$S3_BACKUP_BUCKET" 2>/dev/null || echo '{}')
OL_ENABLED=$(echo "$OL_RAW" | jq -r '.ObjectLockConfiguration.ObjectLockEnabled // "Disabled"')
OL_MODE=$(echo "$OL_RAW" | jq -r '.ObjectLockConfiguration.Rule.DefaultRetention.Mode // "none"')
OL_DAYS=$(echo "$OL_RAW" | jq -r '.ObjectLockConfiguration.Rule.DefaultRetention.Days // 0')
note_finding "object_lock_enabled" "\"$OL_ENABLED\""
note_finding "object_lock_mode" "\"$OL_MODE\""
note_finding "object_lock_days" "$OL_DAYS"
if [[ "$OL_ENABLED" == "Enabled" ]]; then
    if [[ "$OL_MODE" != "COMPLIANCE" ]]; then
        flag_drift "Object Lock mode is '$OL_MODE', expected 'COMPLIANCE'"
    fi
    if [[ "$OL_DAYS" -lt "$LOCK_MIN_DAYS" ]]; then
        flag_drift "Object Lock retention is $OL_DAYS days, expected >= $LOCK_MIN_DAYS"
    fi
fi

# 4. WRITE IAM policy.
#
# Failure modes from the audit user side:
#   - Missing iam:ListUserPolicies → list returns empty → we can't tell whether
#     the policy is missing or perms are degraded.
#   - Missing iam:GetUserPolicy → list works (we see policy names), but the
#     policy document fetch returns AccessDenied. Treating an empty action
#     list as "WRITE policy missing s3:PutObject" produces false-positive
#     CRITICAL drift every night when the real backups are landing fine. We
#     surface "degraded audit perms" instead and let the operator restore
#     the grant.
AUDIT_PERMS_DEGRADED=0
if [[ $SKIP_IAM -eq 0 ]]; then
    LIST_OUT=$(aws_audit iam list-user-policies --user-name "$WRITE_USER_NAME" 2>&1)
    LIST_RC=$?
    POLICY_NAMES=""
    if [[ $LIST_RC -eq 0 ]]; then
        POLICY_NAMES=$(echo "$LIST_OUT" | jq -r '.PolicyNames[]?' 2>/dev/null || true)
    elif echo "$LIST_OUT" | grep -qE 'AccessDenied|not authorized'; then
        AUDIT_PERMS_DEGRADED=1
        flag_drift "audit user lacks 'iam:ListUserPolicies' on '$WRITE_USER_NAME' (degraded-audit; restore grant per #349)"
    fi

    if [[ $AUDIT_PERMS_DEGRADED -eq 0 && -z "$POLICY_NAMES" ]]; then
        flag_drift "no inline policies on user '$WRITE_USER_NAME'"
    elif [[ -n "$POLICY_NAMES" ]]; then
        ALL_ACTIONS=""
        GET_PERMS_DEGRADED=0
        while IFS= read -r pname; do
            [[ -z "$pname" ]] && continue
            PDOC=$(aws_audit iam get-user-policy --user-name "$WRITE_USER_NAME" --policy-name "$pname" 2>&1)
            if echo "$PDOC" | grep -qE 'AccessDenied|not authorized'; then
                GET_PERMS_DEGRADED=1
                continue
            fi
            ACTIONS=$(echo "$PDOC" | jq -r '.PolicyDocument.Statement[]?.Action | if type=="array" then .[] else . end' 2>/dev/null || true)
            ALL_ACTIONS="$ALL_ACTIONS"$'\n'"$ACTIONS"
        done <<< "$POLICY_NAMES"

        if [[ $GET_PERMS_DEGRADED -eq 1 ]]; then
            AUDIT_PERMS_DEGRADED=1
            flag_drift "audit user lacks 'iam:GetUserPolicy' on '$WRITE_USER_NAME' (degraded-audit; cannot verify policy actions — restore grant per #349)"
        else
            REQUIRED=("s3:PutObject")
            for action in "${REQUIRED[@]}"; do
                if ! grep -qF "$action" <<< "$ALL_ACTIONS"; then
                    flag_drift "WRITE policy missing required action '$action'"
                fi
            done

            FORBIDDEN=("s3:GetObject" "s3:DeleteObject" "s3:DeleteObjectVersion" "s3:PutBucketVersioning" "s3:PutLifecycleConfiguration" "s3:PutBucketPolicy" "s3:*")
            for action in "${FORBIDDEN[@]}"; do
                if grep -qF "$action" <<< "$ALL_ACTIONS"; then
                    flag_drift "WRITE policy contains forbidden action '$action'"
                fi
            done
        fi

        note_finding "write_iam_policy_names" "$(printf '%s' "$POLICY_NAMES" | jq -R . | jq -s .)"
    fi
fi
note_finding "audit_perms_degraded" "$([[ $AUDIT_PERMS_DEGRADED -eq 1 ]] && echo true || echo false)"

# 5. READ creds in .env - hygiene check. Read creds should only ever be
# hydrated at restore time, not sitting in nightly runtime env.
READ_IN_ENV=false
if [[ -f "${CHASSIS_HOME}/.env" ]] && grep -qE '^AWS_BACKUP_READ_(ACCESS_KEY_ID|SECRET_ACCESS_KEY)=' "${CHASSIS_HOME}/.env"; then
    READ_IN_ENV=true
    # Soft drift - not all installs have completed the hygiene migration yet.
    # Customer scripts can re-enable as hard drift via the local-health-hooks.sh
    # extension pattern when their migration completes.
fi
note_finding "read_creds_in_env" "$READ_IN_ENV"

# Compose JSON output. `count` matches the dispatcher's threshold gather
# convention; `drift` is the human-friendly bool.
DRIFT_COUNT=0
for f in "${FINDINGS[@]}"; do
    [[ "$f" == \"drift_* ]] && DRIFT_COUNT=$((DRIFT_COUNT+1))
done
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
JOINED_FINDINGS=$(printf '%s,' "${FINDINGS[@]}")
JOINED_FINDINGS="${JOINED_FINDINGS%,}"
DRIFT_BOOL=$([[ $DRIFT -eq 0 ]] && echo "false" || echo "true")

cat <<JSON
{
  "count": $DRIFT_COUNT,
  "drift": $DRIFT_BOOL,
  "ts_utc": "$TS",
  "bucket": "$S3_BACKUP_BUCKET",
  "region": "$S3_BACKUP_REGION",
  "audit_via": "$AUDIT_VIA",
  $JOINED_FINDINGS
}
JSON

if [[ $VERBOSE -eq 1 ]]; then
    echo "" >&2
    echo "=== Backup posture summary ===" >&2
    echo "  bucket: $S3_BACKUP_BUCKET" >&2
    echo "  versioning: $VERS_STATUS" >&2
    echo "  lifecycle noncurrent rules: $HAS_NONCURRENT" >&2
    echo "  object lock: $OL_ENABLED ($OL_MODE / $OL_DAYS days)" >&2
    echo "  read creds in .env: $READ_IN_ENV" >&2
    echo "  drift: $DRIFT_BOOL" >&2
fi

exit 0
