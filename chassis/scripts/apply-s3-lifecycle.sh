#!/usr/bin/env bash
# apply-s3-lifecycle.sh - Apply the GFS backup-retention lifecycle policy to the
# S3 backup bucket.
#
# Pairs with the BACKUP_GFS_RETENTION fanout in encrypted-s3-upload.sh. The
# fanout creates monthly/quarterly/yearly copies; this script installs the
# lifecycle rules that tier them into Glacier Deep Archive and expire them on a
# grandfather-father-son schedule. See docs/backup-retention.md for the full
# scheme.
#
# This is a FULL REPLACE of the bucket's lifecycle configuration. It includes
# the nightly-expire rule (30-day daily window) so that existing behavior is
# preserved, plus the three long-tail rules.
#
# Credentials: needs s3:PutLifecycleConfiguration + s3:GetLifecycleConfiguration
# on the bucket. The chassis backup-writer key (AWS_BACKUP_WRITE_*) is scoped to
# PutObject only and will NOT work here. Provide an admin credential via the
# ambient AWS environment or AWS_PROFILE, e.g.:
#
#   AWS_PROFILE=my-admin bash chassis/scripts/apply-s3-lifecycle.sh
#
# Reads S3_BACKUP_BUCKET + S3_BACKUP_REGION from $CHASSIS_HOME/.env (or the
# environment). Idempotent - safe to re-run.

set -euo pipefail

CHASSIS_HOME="${CHASSIS_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

if [[ -f "${CHASSIS_HOME}/.env" ]]; then
    set -a
    # shellcheck disable=SC1090,SC1091
    source "${CHASSIS_HOME}/.env" 2>/dev/null || true
    set +a
fi

: "${S3_BACKUP_BUCKET:?set S3_BACKUP_BUCKET in .env or env}"
: "${S3_BACKUP_REGION:?set S3_BACKUP_REGION in .env or env}"

POLICY_FILE="$(mktemp /tmp/s3-lifecycle-gfs.XXXXXX.json)"
trap 'rm -f "$POLICY_FILE"' EXIT

cat > "$POLICY_FILE" <<'JSON'
{
  "Rules": [
    {
      "ID": "nightly-expire",
      "Status": "Enabled",
      "Filter": { "Prefix": "nightly/" },
      "Expiration": { "Days": 30 },
      "NoncurrentVersionExpiration": { "NoncurrentDays": 30 }
    },
    {
      "ID": "monthly-12mo-deep-archive",
      "Status": "Enabled",
      "Filter": { "Prefix": "monthly/" },
      "Transitions": [ { "Days": 1, "StorageClass": "DEEP_ARCHIVE" } ],
      "Expiration": { "Days": 365 }
    },
    {
      "ID": "quarterly-3yr-deep-archive",
      "Status": "Enabled",
      "Filter": { "Prefix": "quarterly/" },
      "Transitions": [ { "Days": 1, "StorageClass": "DEEP_ARCHIVE" } ],
      "Expiration": { "Days": 1095 }
    },
    {
      "ID": "yearly-forever-deep-archive",
      "Status": "Enabled",
      "Filter": { "Prefix": "yearly/" },
      "Transitions": [ { "Days": 1, "StorageClass": "DEEP_ARCHIVE" } ]
    }
  ]
}
JSON

echo "Applying GFS lifecycle policy to s3://${S3_BACKUP_BUCKET} (${S3_BACKUP_REGION})..."
echo "This REPLACES the bucket's entire lifecycle configuration."

if ! aws s3api put-bucket-lifecycle-configuration \
        --bucket "$S3_BACKUP_BUCKET" \
        --region "$S3_BACKUP_REGION" \
        --lifecycle-configuration "file://${POLICY_FILE}"; then
    echo "ERROR: put-bucket-lifecycle-configuration failed." >&2
    echo "The backup-writer key (AWS_BACKUP_WRITE_*) cannot do this - it is" >&2
    echo "PutObject-scoped. Re-run with an admin credential (AWS_PROFILE=...)" >&2
    echo "or apply the rules via the AWS Console (see docs/backup-retention.md)." >&2
    exit 1
fi

echo "Applied. Reading back current configuration:"
aws s3api get-bucket-lifecycle-configuration \
    --bucket "$S3_BACKUP_BUCKET" \
    --region "$S3_BACKUP_REGION"

echo "Done."
