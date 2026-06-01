#!/usr/bin/env bash
# backup-to-s3.sh - Nightly chassis-standard backup to S3.
#
# Assembles a staging dir from the canonical chassis state set, then hands
# off to chassis/scripts/encrypted-s3-upload.sh for the tar + age + S3 leg.
#
# Heartbeat-compatible gather script. Emits the standard JSON contract on
# stdout: count=0 silent success, count>0 triggers Claude alert via the
# dispatcher's notify-on-failure path.
#
# What gets bundled (canonical chassis set):
#   - $CHASSIS_HOME/.env                  -> env.txt   (renamed; secrets, encrypted)
#   - $CHASSIS_HOME/memory/               -> memory/   (knowledge graph + auto-memory)
#   - $CHASSIS_HOME/data/                 -> data/     (runtime state, sqlite, etc.)
#   - $CHASSIS_HOME/scheduled-tasks/{heartbeat-state,conservation-mode,triaged-issues}.json
#                                         -> scheduled-tasks/
#   - $CHASSIS_HOME/backups/{postgres,siyuan,vaultwarden,n8n,turso}/<today>
#                                         -> sibling-backups/  (today only)
#   - git archive HEAD                    -> repo/$BACKUP_NAME-HEAD.tar.gz
#                                            (DR-self-sufficiency if GitHub gone)
#   - MANIFEST.json                       -> top-level
#
# Customer extension - drop a file at
# $CHASSIS_HOME/scheduled-tasks/backup-extras.sh and we'll source it after
# the canonical bundle is assembled. The hook receives $STAGING as an env
# var and can copy additional artifacts into it. Keeps install-specific
# bundle shapes (e.g. V1 install's BFL photos, plugin-specific data dirs)
# layered on top of the canonical set without forking this script.
#
# Required env (read from $CHASSIS_HOME/.env; encrypted-s3-upload.sh also
# validates):
#   AWS_BACKUP_WRITE_ACCESS_KEY_ID
#   AWS_BACKUP_WRITE_SECRET_ACCESS_KEY
#   S3_BACKUP_BUCKET
#   S3_BACKUP_REGION
#   BACKUP_AGE_RECIPIENT
#
# Optional env:
#   BACKUP_NAME              - default derived from chassis.config.yaml
#                              installer_name (lowercased), falling back to
#                              "chassis-backup"
#   BACKUP_INCLUDE_REPO      - default "true". Set "false" to skip the
#                              git-archive HEAD step (useful on installs
#                              without a tracked git tree).
#   BACKUP_SIBLINGS          - default "postgres siyuan vaultwarden n8n turso".
#                              Space-separated list of subdirs under
#                              $CHASSIS_HOME/backups/ to bundle today's
#                              <date>.* file from. Customers can extend.
#
# Related:
#   chassis/scripts/encrypted-s3-upload.sh  - the tar+age+s3 utility
#   chassis/scripts/pg-backup.sh            - sibling that produces postgres input
#   docs/disaster-recovery.md               - restore runbook

set -euo pipefail

: "${CHASSIS_HOME:?CHASSIS_HOME must be set (chassis dispatcher exports it)}"

fail() {
    local msg="$1"
    printf '{"count": 1, "issues": ["%s"]}\n' "${msg//\"/\\\"}"
    exit 0
}

# Source .env. Required env vars validated downstream by encrypted-s3-upload.sh.
if [[ -f "${CHASSIS_HOME}/.env" ]]; then
    set -a
    # shellcheck disable=SC1090,SC1091
    source "${CHASSIS_HOME}/.env" 2>/dev/null || true
    set +a
fi

TODAY="$(date +%Y-%m-%d)"
TS="$(date +%Y%m%dT%H%M%SZ)"
TMPDIR="$(mktemp -d /tmp/chassis-backup.XXXXXX)"
STAGING="${TMPDIR}/staging"
mkdir -p "$STAGING"
trap 'rm -rf "$TMPDIR"' EXIT

# Canonical bundle.
if [[ -f "${CHASSIS_HOME}/.env" ]]; then
    cp "${CHASSIS_HOME}/.env" "${STAGING}/env.txt"
fi

if [[ -d "${CHASSIS_HOME}/memory" ]]; then
    cp -R "${CHASSIS_HOME}/memory" "${STAGING}/memory"
fi

if [[ -d "${CHASSIS_HOME}/data" ]]; then
    cp -R "${CHASSIS_HOME}/data" "${STAGING}/data"
fi

mkdir -p "${STAGING}/scheduled-tasks"
for f in heartbeat-state.json conservation-mode.json triaged-issues.json; do
    [[ -f "${CHASSIS_HOME}/scheduled-tasks/$f" ]] && \
        cp "${CHASSIS_HOME}/scheduled-tasks/$f" "${STAGING}/scheduled-tasks/" || true
done

# Today's sibling-heartbeat backup outputs. Stale-by-one-day is better than
# nothing, so missing files are noted but don't fail the run.
mkdir -p "${STAGING}/sibling-backups"
for subdir in ${BACKUP_SIBLINGS:-postgres siyuan vaultwarden n8n turso}; do
    src_dir="${CHASSIS_HOME}/backups/${subdir}"
    [[ -d "$src_dir" ]] || continue
    # Find today's file regardless of extension.
    today_file=$(find "$src_dir" -maxdepth 1 -name "${TODAY}.*" -type f 2>/dev/null | head -1)
    if [[ -n "$today_file" ]]; then
        cp "$today_file" "${STAGING}/sibling-backups/${subdir}-${TODAY}.$(basename "$today_file" | sed "s|^${TODAY}.||")"
    fi
done

# Repo snapshot - makes the backup self-sufficient if GitHub is unavailable
# during DR. git archive HEAD captures tracked files at the current commit;
# untracked working-tree changes (drafts, editor garbage, .env) are excluded.
BACKUP_NAME="${BACKUP_NAME:-chassis-backup}"
if [[ "${BACKUP_INCLUDE_REPO:-true}" == "true" ]] && [[ -d "${CHASSIS_HOME}/.git" ]]; then
    mkdir -p "${STAGING}/repo"
    REPO_SHA="$(git -C "${CHASSIS_HOME}" rev-parse HEAD 2>/dev/null || echo "unknown")"
    if [[ "$REPO_SHA" != "unknown" ]]; then
        git -C "${CHASSIS_HOME}" archive --format=tar.gz HEAD > "${STAGING}/repo/${BACKUP_NAME}-HEAD.tar.gz" 2>/dev/null || true
        echo "${REPO_SHA}" > "${STAGING}/repo/HEAD-sha.txt"
    fi
fi

# Customer extension hook (optional). Runs AFTER canonical bundle assembly
# so a customer can add install-specific paths (BFL photo archive, plugin
# data dirs, etc.) into the staging dir. Receives STAGING as an env var.
EXTRAS_HOOK="${CHASSIS_HOME}/scheduled-tasks/backup-extras.sh"
if [[ -f "$EXTRAS_HOOK" ]]; then
    # shellcheck disable=SC1090
    STAGING="$STAGING" source "$EXTRAS_HOOK" || true
fi

# Top-level MANIFEST.json. Contents-list helps DR scripts and humans see
# what's inside without decrypting first.
MANIFEST="${STAGING}/MANIFEST.json"
cat > "$MANIFEST" <<EOF
{
  "backup_date": "${TODAY}",
  "timestamp_utc": "${TS}",
  "chassis_home": "${CHASSIS_HOME}",
  "hostname": "$(hostname -s 2>/dev/null || hostname)",
  "backup_name": "${BACKUP_NAME}",
  "age_recipient": "${BACKUP_AGE_RECIPIENT:-unset}",
  "contents": $(find "${STAGING}" -type f -not -name 'MANIFEST.json' 2>/dev/null | sed "s|${STAGING}/||" | jq -R . | jq -s . 2>/dev/null || echo '[]')
}
EOF

# Hand off to the generic uploader. It validates the AWS/age env, tars +
# encrypts + uploads, and emits its own gather-contract JSON line.
BACKUP_NAME="$BACKUP_NAME" "${CHASSIS_ROOT:-/app/chassis}/scripts/encrypted-s3-upload.sh" "$STAGING"
