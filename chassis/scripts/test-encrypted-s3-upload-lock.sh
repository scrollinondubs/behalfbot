#!/usr/bin/env bash
# Tests BACKUP_OBJECT_LOCK_DAYS in encrypted-s3-upload.sh against stub `aws`
# and `age` binaries. No network, no AWS account.
#
#   bash chassis/scripts/test-encrypted-s3-upload-lock.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
UPLOADER="$HERE/encrypted-s3-upload.sh"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT

mkdir -p "$T/bin" "$T/stage"
echo hello > "$T/stage/file.txt"

# Stub aws: logs every call; put-object-retention fails when AWS_STUB_LOCK_FAIL=1.
cat > "$T/bin/aws" <<'EOF'
#!/usr/bin/env bash
echo "$*" >> "$AWS_STUB_LOG"
if [[ "$1 $2" == "s3api put-object-retention" && "${AWS_STUB_LOCK_FAIL:-0}" == "1" ]]; then exit 254; fi
exit 0
EOF
# Stub age: copies input to --output.
cat > "$T/bin/age" <<'EOF'
#!/usr/bin/env bash
out=""; in=""
while [[ $# -gt 0 ]]; do case "$1" in --output) out="$2"; shift 2;; --recipient) shift 2;; --encrypt) shift;; *) in="$1"; shift;; esac; done
cp "$in" "$out"
EOF
chmod +x "$T/bin/aws" "$T/bin/age"

export PATH="$T/bin:$PATH"
export AWS_BACKUP_WRITE_ACCESS_KEY_ID=test AWS_BACKUP_WRITE_SECRET_ACCESS_KEY=test
export S3_BACKUP_BUCKET=bucket S3_BACKUP_REGION=eu-west-1 BACKUP_AGE_RECIPIENT=age1test
export AWS_STUB_LOG="$T/aws.log"

fails=0
check() { if eval "$2"; then echo "ok   - $1"; else echo "FAIL - $1"; fails=$((fails + 1)); fi; }

run() { : > "$AWS_STUB_LOG"; OUT="$(bash "$UPLOADER" "$T/stage" 2>"$T/err")"; }

# 1. Unset: no retention calls, count 0.
unset BACKUP_OBJECT_LOCK_DAYS
run
check "unset: no put-object-retention calls" '! grep -q put-object-retention "$AWS_STUB_LOG"'
check "unset: count 0" '[[ "$OUT" == *"\"count\": 0"* ]]'

# 2. Set: tarball and manifest both locked, COMPLIANCE, date about N days out.
export BACKUP_OBJECT_LOCK_DAYS=60
run
check "set: two retention calls (tarball + manifest)" '[[ $(grep -c put-object-retention "$AWS_STUB_LOG") == 2 ]]'
check "set: tarball key locked" 'grep put-object-retention "$AWS_STUB_LOG" | grep -q "\.tar\.gz\.age"'
check "set: manifest key locked" 'grep put-object-retention "$AWS_STUB_LOG" | grep -q "manifest\.json"'
check "set: COMPLIANCE mode" 'grep put-object-retention "$AWS_STUB_LOG" | head -1 | grep -q "Mode=COMPLIANCE"'
want="$(date -u -d '+60 days' +%Y-%m-%d 2>/dev/null || date -u -v+60d +%Y-%m-%d)"
check "set: retain-until is today + 60 days" 'grep put-object-retention "$AWS_STUB_LOG" | head -1 | grep -q "RetainUntilDate=${want}T"'
check "set: count 0" '[[ "$OUT" == *"\"count\": 0"* ]]'

# 3. Lock failure is loud: count 1, names the key, upload key still reported.
export AWS_STUB_LOCK_FAIL=1
run
check "lock fails: count 1" '[[ "$OUT" == *"\"count\": 1"* ]]'
check "lock fails: issue names Object Lock" '[[ "$OUT" == *"Object Lock retention failed on 2 object(s)"* ]]'
check "lock fails: s3_key still reported" '[[ "$OUT" == *"\"s3_key\": \"nightly/"* ]]'
check "lock fails: warn on stderr" 'grep -q "warn: Object Lock retention" "$T/err"'
unset AWS_STUB_LOCK_FAIL

# 4. Garbage value is refused before any upload.
export BACKUP_OBJECT_LOCK_DAYS=abc
run
check "invalid: count 1 with message" '[[ "$OUT" == *"BACKUP_OBJECT_LOCK_DAYS must be a positive whole number"* ]]'
check "invalid: nothing uploaded" '! grep -q "s3 cp" "$AWS_STUB_LOG"'

echo
if (( fails > 0 )); then echo "$fails failed"; exit 1; fi
echo "all passed"
