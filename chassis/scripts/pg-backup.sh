#!/usr/bin/env bash
# pg-backup.sh - Nightly Postgres backup for chassis installs.
#
# Heartbeat-compatible gather script. Outputs JSON status to stdout following
# the standard gather contract (count=0 silent success, count>0 triggers
# Claude alert per the dispatcher's notify-on-failure pattern).
#
# Two execution modes, auto-detected:
#   1. Host pg_dump + DSN: if `pg_dump` is on PATH and `CHASSIS_PG_DSN` is set,
#      runs the dump locally streaming through gzip. Fastest path.
#   2. docker compose exec: falls back to running pg_dump INSIDE the chassis
#      postgres container so the host doesn't need pg_dump installed. Output
#      stream is captured on the host side and gzipped in a single pipe so
#      the dump never lands on the container filesystem.
#
# Output format is custom (-Fc) so pg_restore can parallelize on DR.
#
# Retention: last RETENTION_DAYS daily backups + 1st-of-month forever.
#
# Required env (read from $CHASSIS_HOME/.env via the heartbeat dispatcher):
#   CHASSIS_PG_DSN    - postgresql://user:pw@host:port/db (host mode)
# Optional:
#   CHASSIS_PG_USER   - default "chassis" (container mode)
#   CHASSIS_PG_DB     - default "chassis" (container mode)
#   PG_CONTAINER  - default "behalfbot-postgres" (container mode)
#   RETENTION_DAYS - default 30
#   BACKUP_SUBDIR  - default "postgres" (relative to $CHASSIS_HOME/backups)
#
# Related:
#   docker-compose.yml - postgres service definition
#   docs/disaster-recovery.md - restore runbook

set -euo pipefail

: "${CHASSIS_HOME:?CHASSIS_HOME must be set (chassis dispatcher exports it)}"

BACKUP_DIR="${CHASSIS_HOME}/backups/${BACKUP_SUBDIR:-postgres}"
DATE=$(date +%Y-%m-%d)
DUMP_FILE="${BACKUP_DIR}/${DATE}.dump.gz"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
PG_CONTAINER="${PG_CONTAINER:-behalfbot-postgres}"

# Load env. Heartbeat dispatcher already sources .env so this is belt-and-
# suspenders; the explicit `|| true` keeps `set -e` from blowing up if a
# customer's .env has hydration logic that fails partially.
if [[ -f "${CHASSIS_HOME}/.env" ]]; then
    set -a
    # shellcheck disable=SC1090,SC1091
    source "${CHASSIS_HOME}/.env" 2>/dev/null || true
    set +a
fi

fail() {
    local msg="$1"
    # Escape double-quotes for JSON. The dispatcher reads count>0 as
    # "fire Claude" so the message string ends up in a Discord alert.
    printf '{"count": 1, "issues": ["%s"]}\n' "${msg//\"/\\\"}"
    exit 0
}

mkdir -p "$BACKUP_DIR"

# Skip if today's backup already exists + non-empty. Heartbeats can fire
# multiple times per day; idempotency keeps us from re-dumping unnecessarily.
if [[ -s "$DUMP_FILE" ]]; then
    echo '{"count": 0, "issues": []}'
    exit 0
fi

# Prefer local pg_dump if both available; container exec is the fallback.
# Container-exec is the typical path inside the chassis Docker stack because
# pg_dump isn't installed on the host by default.
if command -v pg_dump >/dev/null 2>&1 && [[ -n "${CHASSIS_PG_DSN:-}" ]]; then
    if ! pg_dump -Fc --no-owner --no-acl "${CHASSIS_PG_DSN}" 2>/dev/null | gzip > "${DUMP_FILE}.tmp"; then
        rm -f "${DUMP_FILE}.tmp"
        fail "host pg_dump failed"
    fi
else
    if ! docker exec "$PG_CONTAINER" pg_dump -Fc --no-owner --no-acl -U "${CHASSIS_PG_USER:-chassis}" "${CHASSIS_PG_DB:-chassis}" 2>/dev/null | gzip > "${DUMP_FILE}.tmp"; then
        rm -f "${DUMP_FILE}.tmp"
        fail "docker pg_dump failed - is $PG_CONTAINER running?"
    fi
fi

if [[ ! -s "${DUMP_FILE}.tmp" ]]; then
    rm -f "${DUMP_FILE}.tmp"
    fail "pg_dump produced empty output"
fi

mv "${DUMP_FILE}.tmp" "${DUMP_FILE}"

# Prune old backups - keep last N days, plus 1st-of-month forever.
# macOS `date -v-Nd` and GNU `date -d "-N days"` both supported via fallback.
prune_old_backups() {
    local cutoff
    cutoff=$(date -v-"${RETENTION_DAYS}"d +%Y-%m-%d 2>/dev/null || date -d "-${RETENTION_DAYS} days" +%Y-%m-%d)
    for backup_file in "${BACKUP_DIR}"/*.dump.gz; do
        [[ -f "$backup_file" ]] || continue
        local filename
        filename=$(basename "$backup_file" .dump.gz)
        if [[ "$filename" > "$cutoff" || "$filename" == "$cutoff" ]]; then
            continue
        fi
        local day="${filename:8:2}"
        if [[ "$day" != "01" ]]; then
            rm -f "$backup_file"
        fi
    done
}

prune_old_backups

SIZE=$(du -h "$DUMP_FILE" | cut -f1)
printf '{"count": 0, "issues": [], "size": "%s", "path": "%s"}\n' "$SIZE" "$DUMP_FILE"
