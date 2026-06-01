#!/usr/bin/env bash
# encrypted-s3-upload.sh - Generic tar + age + S3 upload utility.
#
# Takes a staging directory, tarballs its contents, age-encrypts with the
# configured recipient public key, uploads to S3 under nightly/<date>/, and
# writes a sibling manifest.json with the encrypted-object metadata.
#
# Composable: any chassis script (or customer wrapper) that has already
# assembled a staging directory can call this utility to finish the upload.
# Keeps the encryption + manifest + idempotency logic in one place instead
# of duplicating across siyuan-backup, vaultwarden-backup, pg-backup,
# customer-bundle-backup, etc.
#
# Usage:
#   chassis/scripts/encrypted-s3-upload.sh <staging-dir>
#
# Required env (read from caller's environment; caller is responsible for
# having sourced $CHASSIS_HOME/.env or otherwise populated these):
#   AWS_BACKUP_WRITE_ACCESS_KEY_ID     - dedicated backup-writer IAM key
#   AWS_BACKUP_WRITE_SECRET_ACCESS_KEY
#   S3_BACKUP_BUCKET                   - target bucket name
#   S3_BACKUP_REGION                   - target region
#   BACKUP_AGE_RECIPIENT               - age public key ("age1...")
#
# Optional env:
#   BACKUP_S3_PREFIX  - default "nightly". Lets customers route to a
#                       dedicated path within the bucket.
#   BACKUP_NAME       - default "chassis-backup". Embedded in the tarball
#                       filename + S3 object key.
#
# Output (to stdout, single JSON line):
#   {"count": 0, "issues": [], "s3_key": "...", "size_bytes": N,
#    "sha256": "..."}
# On failure:
#   {"count": 1, "issues": ["..."]}
#
# The JSON contract matches the chassis heartbeat gather-script convention,
# so this utility can be invoked directly as a gather command if a customer
# wants per-staging-dir upload tracking.
#
# Security notes:
# - The tarball is encrypted with `age` using a PUBLIC key recipient. Only
#   the holder of the matching private key can decrypt. The private key
#   MUST NEVER touch the chassis host - keep it in a password manager or
#   YubiKey, restored only at DR time.
# - The S3 manifest object is plaintext but contains zero secret material
#   (date, object key, size, sha256, age recipient, hostname). Safe to
#   list publicly if the bucket ever leaks.
# - aws-cli credentials are injected per-command via `AWS_ACCESS_KEY_ID=...
#   AWS_SECRET_ACCESS_KEY=...` env-prefix syntax so they don't survive in
#   the shell environment beyond each cp invocation.

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo '{"count": 1, "issues": ["usage: encrypted-s3-upload.sh <staging-dir>"]}'
    exit 0
fi

STAGING_DIR="$1"

fail() {
    local msg="$1"
    printf '{"count": 1, "issues": ["%s"]}\n' "${msg//\"/\\\"}"
    exit 0
}

if [[ ! -d "$STAGING_DIR" ]]; then
    fail "staging dir does not exist: $STAGING_DIR"
fi

if [[ -z "$(ls -A "$STAGING_DIR")" ]]; then
    fail "staging dir is empty: $STAGING_DIR"
fi

# Validate required env. Explicit per-var check so the failure message
# names every missing one in a single line (faster triage than serial
# zsh `:?` errors).
missing=()
for var in AWS_BACKUP_WRITE_ACCESS_KEY_ID AWS_BACKUP_WRITE_SECRET_ACCESS_KEY S3_BACKUP_BUCKET S3_BACKUP_REGION BACKUP_AGE_RECIPIENT; do
    eval "value=\${$var:-}"
    if [[ -z "$value" ]]; then
        missing+=("$var")
    fi
done
if (( ${#missing[@]} > 0 )); then
    fail "missing required env vars: ${missing[*]}"
fi

TODAY="$(date +%Y-%m-%d)"
TS="$(date +%Y%m%dT%H%M%SZ)"
BACKUP_NAME="${BACKUP_NAME:-chassis-backup}"
BACKUP_S3_PREFIX="${BACKUP_S3_PREFIX:-nightly}"

WORK_DIR="$(mktemp -d /tmp/chassis-s3-upload.XXXXXX)"
trap 'rm -rf "$WORK_DIR"' EXIT

TARBALL="${WORK_DIR}/${BACKUP_NAME}-${TS}.tar.gz"
ENCRYPTED="${TARBALL}.age"

# Tar the staging dir's CONTENTS, not the dir itself, so restore lands a
# predictable layout. Caller is expected to have shaped the dir already
# (top-level subdirs like memory/, scheduled-tasks/, etc.).
if ! tar -czf "$TARBALL" -C "$STAGING_DIR" . 2>/dev/null; then
    fail "tar failed"
fi

if [[ ! -s "$TARBALL" ]]; then
    fail "tar produced empty output"
fi

if ! age --encrypt --recipient "$BACKUP_AGE_RECIPIENT" --output "$ENCRYPTED" "$TARBALL" 2>/dev/null; then
    fail "age encrypt failed"
fi

if [[ ! -s "$ENCRYPTED" ]]; then
    fail "age produced empty output"
fi

# stat -c (GNU) / stat -f (BSD/macOS) split. BSD %z = size; GNU %s = size.
ENC_SIZE=$(stat -c%s "$ENCRYPTED" 2>/dev/null || stat -f%z "$ENCRYPTED")
SHA256=$(shasum -a 256 "$ENCRYPTED" 2>/dev/null | awk '{print $1}')
if [[ -z "$SHA256" ]]; then
    # Linux fallback when shasum isn't installed.
    SHA256=$(sha256sum "$ENCRYPTED" 2>/dev/null | awk '{print $1}')
fi

S3_KEY="${BACKUP_S3_PREFIX}/${TODAY}/${BACKUP_NAME}-${TS}.tar.gz.age"
S3_MANIFEST_KEY="${BACKUP_S3_PREFIX}/${TODAY}/manifest.json"

# Upload the encrypted tarball.
if ! AWS_ACCESS_KEY_ID="$AWS_BACKUP_WRITE_ACCESS_KEY_ID" \
     AWS_SECRET_ACCESS_KEY="$AWS_BACKUP_WRITE_SECRET_ACCESS_KEY" \
     AWS_DEFAULT_REGION="$S3_BACKUP_REGION" \
     aws s3 cp "$ENCRYPTED" "s3://${S3_BACKUP_BUCKET}/${S3_KEY}" --no-progress >/dev/null 2>&1; then
    fail "s3 cp encrypted-tarball failed"
fi

# Write + upload sibling manifest. Plaintext is OK - manifest contains
# zero secret material.
MANIFEST_FILE="${WORK_DIR}/manifest.json"
cat > "$MANIFEST_FILE" <<EOF
{
  "date": "${TODAY}",
  "timestamp_utc": "${TS}",
  "object_key": "${S3_KEY}",
  "encrypted_size_bytes": ${ENC_SIZE},
  "encrypted_sha256": "${SHA256}",
  "age_recipient": "${BACKUP_AGE_RECIPIENT}",
  "hostname": "$(hostname -s 2>/dev/null || hostname)",
  "backup_name": "${BACKUP_NAME}"
}
EOF

if ! AWS_ACCESS_KEY_ID="$AWS_BACKUP_WRITE_ACCESS_KEY_ID" \
     AWS_SECRET_ACCESS_KEY="$AWS_BACKUP_WRITE_SECRET_ACCESS_KEY" \
     AWS_DEFAULT_REGION="$S3_BACKUP_REGION" \
     aws s3 cp "$MANIFEST_FILE" "s3://${S3_BACKUP_BUCKET}/${S3_MANIFEST_KEY}" --no-progress >/dev/null 2>&1; then
    fail "s3 cp manifest failed (tarball already uploaded successfully)"
fi

printf '{"count": 0, "issues": [], "s3_key": "%s", "size_bytes": %s, "sha256": "%s"}\n' \
    "$S3_KEY" "$ENC_SIZE" "$SHA256"
