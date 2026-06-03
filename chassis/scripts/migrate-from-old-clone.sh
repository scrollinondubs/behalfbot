#!/usr/bin/env bash
# chassis/scripts/migrate-from-old-clone.sh
# =========================================
# Post-rename / chassis-subtree-re-anchor migration helper. Restores customer
# state from a backup of the OLD chassis clone into the NEW clone WITHOUT
# stomping any file the chassis subtree owns.
#
# Why this exists (chassis#5 item 5): on 2026-06-01 the chassis repo was
# renamed and several installers (Toby first, Ben pending) had to re-clone
# the chassis tree. The recommended workflow was:
#   1. tar up the old clone
#   2. rm -rf the clone dir
#   3. git clone the new repo URL
#   4. restore the customer-side files from the tarball
# Step 4 was a hand-written restore loop pinned to a hard-coded allowlist:
#   .env, .env.baked, install-profile.md, chassis.config.yaml, CLAUDE.md,
#   HEARTBEATS.md, chassis-compose.override.yml, data/, state/, briefings/,
#   memory/, logs/, backups/.
# That list MISSED: scheduled-tasks/, .mcp.json, skills/, plugins/, scripts/.
# Toby's first heartbeat after the re-clone failed with
#   "scheduled-tasks/<name>.sh: no such file or directory"
# and the install was offline for 14 hours.
#
# This script uses the INVERSE approach: everything in the backup is restored
# EXCEPT what the new chassis subtree authoritatively provides. The exclude
# list mirrors the chassis tree's top-level contents, plus the git metadata
# of the new clone.
#
# Inputs:
#   --backup-dir PATH   absolute path to the old-clone backup root. Required.
#   --target-dir PATH   absolute path to the new clone root. Default: $PWD.
#   --dry-run           print every restore action without executing
#   --force             overwrite existing files in target without prompting
#                       (default: skip with a notice; --force is destructive)
#   --yes               skip the confirmation prompt before destructive ops
#
# Exit codes:
#   0  restore succeeded (or dry-run completed cleanly)
#   2  bad args / source missing / refusing to touch a non-chassis target
#   3  rsync failed
#   4  user aborted the confirmation prompt

set -euo pipefail

BACKUP_DIR=""
TARGET_DIR="${PWD}"
DRY_RUN=false
FORCE=false
YES=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --backup-dir)   BACKUP_DIR="$2"; shift 2 ;;
        --target-dir)   TARGET_DIR="$2"; shift 2 ;;
        --dry-run)      DRY_RUN=true; shift ;;
        --force)        FORCE=true; shift ;;
        --yes)          YES=true; shift ;;
        -h|--help)
            sed -n '1,60p' "$0" | sed 's/^# *//'
            exit 0 ;;
        *)
            echo "unknown arg: $1" >&2
            exit 2 ;;
    esac
done

say() { printf '%s\n' "$*"; }

if [[ -z "$BACKUP_DIR" ]]; then
    say "ERROR: --backup-dir is required" >&2
    say "Usage: $0 --backup-dir /path/to/old-clone-backup --target-dir /path/to/new-clone" >&2
    exit 2
fi

if [[ ! -d "$BACKUP_DIR" ]]; then
    say "ERROR: backup dir does not exist: $BACKUP_DIR" >&2
    exit 2
fi

if [[ ! -d "$TARGET_DIR" ]]; then
    say "ERROR: target dir does not exist: $TARGET_DIR" >&2
    exit 2
fi

# Strip trailing slashes so rsync semantics are predictable.
BACKUP_DIR="${BACKUP_DIR%/}"
TARGET_DIR="${TARGET_DIR%/}"

# Sanity check: the target must look like a chassis clone (chassis/ subdir +
# bootstrap.sh). Refusing to write into a non-chassis tree prevents an
# accidental --target-dir typo from clobbering an unrelated dir.
if [[ ! -d "$TARGET_DIR/chassis" || ! -f "$TARGET_DIR/bootstrap.sh" ]]; then
    say "ERROR: $TARGET_DIR does not look like a chassis clone" >&2
    say "  Expected to find: chassis/ subdir + bootstrap.sh at the root." >&2
    exit 2
fi

# Sanity check: the backup must also look like a chassis clone (or post-#6
# customer dir). At a bare minimum we expect ONE of these to be present:
# .env, chassis.config.yaml, HEARTBEATS.md, CLAUDE.md.
HAS_CUSTOMER_FILE=false
for marker in .env chassis.config.yaml HEARTBEATS.md CLAUDE.md install-profile.md INSTALL_PROFILE.md; do
    if [[ -e "$BACKUP_DIR/$marker" ]]; then
        HAS_CUSTOMER_FILE=true
        break
    fi
done
if [[ "$HAS_CUSTOMER_FILE" != "true" ]]; then
    say "ERROR: $BACKUP_DIR does not look like a chassis clone backup" >&2
    say "  Expected at least one of: .env, chassis.config.yaml, HEARTBEATS.md, CLAUDE.md, install-profile.md" >&2
    exit 2
fi

# The EXCLUDE list = everything the new chassis subtree authoritatively
# provides. Anything in the backup matching these paths is left alone so the
# new chassis wins. Everything else gets restored.
#
# Mirrors the chassis tree's top-level layout (see ls chassis/ in the repo):
#   - chassis/ subtree itself (all chassis-managed code lives here)
#   - bootstrap.sh + Dockerfile + docker-compose.yml + INSTALL_PROFILE.md
#     (chassis-versioned defaults; the customer-edited INSTALL_PROFILE.md
#     lives at install-profile.md per docs/SELF_INSTALL.md so the casing
#     difference matters)
#   - docker/ + docs/ + .github/ + LICENSE + README.md + requirements.txt
#   - The new clone's .git/ - never overwrite git metadata.
EXCLUDES=(
    ".git/"
    "chassis/"
    "Dockerfile"
    "docker-compose.yml"
    "docker/"
    "INSTALL_PROFILE.md"
    "LICENSE"
    "README.md"
    "requirements.txt"
    ".env.example"
    ".dockerignore"
    ".gitignore"
    ".github/"
)

# Plus: any chassis-tracked file at the top level of the new tree should not
# be clobbered by a backup version. Build the exclude list dynamically from
# what's actually present in the new tree so future additions to the chassis
# subtree are handled automatically.
mapfile -t TOP_LEVEL < <(ls -1A "$TARGET_DIR" 2>/dev/null || true)
DYNAMIC_EXCLUDES=()
for entry in "${TOP_LEVEL[@]}"; do
    # Skip dirs/files that are explicitly customer-side per our model.
    case "$entry" in
        .env|.env.baked|.mcp.json|CLAUDE.md|HEARTBEATS.md|chassis.config.yaml|install-profile.md|chassis-compose.override.yml|scripts|scheduled-tasks|plugins|skills|state|data|briefings|memory|logs|backups|temp|launchd|state.json|.bootstrap-marker)
            continue
            ;;
    esac
    DYNAMIC_EXCLUDES+=("$entry")
done

# Verify rsync is available; it carries the heavy lifting for the actual
# copy + the EXCLUDE filter.
if ! command -v rsync >/dev/null 2>&1; then
    say "ERROR: rsync not in PATH - install it before running this migration." >&2
    exit 2
fi

# Build rsync flag list.
RSYNC_FLAGS=(-a)
if [[ "$DRY_RUN" == "true" ]]; then
    RSYNC_FLAGS+=(-vn)
else
    RSYNC_FLAGS+=(-v)
fi

# Default behavior: --ignore-existing (skip files already present in the
# target). --force overrides that to overwrite.
if [[ "$FORCE" == "true" ]]; then
    RSYNC_FLAGS+=(--itemize-changes)
else
    RSYNC_FLAGS+=(--ignore-existing)
fi

# Compose --exclude args for the static list + dynamic list.
EXCLUDE_FLAGS=()
for e in "${EXCLUDES[@]}" "${DYNAMIC_EXCLUDES[@]}"; do
    EXCLUDE_FLAGS+=(--exclude="$e")
done

say "==================================================="
say "Chassis migration restore (chassis#5 item 5)"
say "==================================================="
say "  Backup source (old clone): $BACKUP_DIR"
say "  Target (new clone):        $TARGET_DIR"
say "  Dry run:                   $DRY_RUN"
say "  Force-overwrite existing:  $FORCE"
say ""
say "Excluded from restore (chassis-managed, new clone wins):"
for e in "${EXCLUDES[@]}" "${DYNAMIC_EXCLUDES[@]}"; do
    say "    - $e"
done
say ""

if [[ "$DRY_RUN" != "true" && "$YES" != "true" ]]; then
    say "About to restore customer files from $BACKUP_DIR -> $TARGET_DIR"
    if [[ "$FORCE" == "true" ]]; then
        say "  WARNING: --force is on; existing files in target will be OVERWRITTEN."
    fi
    printf 'Type yes to proceed, anything else to abort: '
    read -r reply
    if [[ "$reply" != "yes" ]]; then
        say "Aborted by user."
        exit 4
    fi
fi

# Run rsync. Trailing slash on src so we copy the contents of $BACKUP_DIR,
# not the dir itself.
RC=0
rsync "${RSYNC_FLAGS[@]}" "${EXCLUDE_FLAGS[@]}" "$BACKUP_DIR"/ "$TARGET_DIR"/ || RC=$?

if [[ $RC -ne 0 ]]; then
    say "" >&2
    say "rsync exited non-zero ($RC) - inspect the output above" >&2
    exit 3
fi

say ""
say "==================================================="
say "Restore complete."
say "==================================================="
say ""
say "Post-restore checklist:"
say "  1. Verify the chassis subtree at $TARGET_DIR/chassis is at the SHA you expected:"
say "       cd $TARGET_DIR && git log -1 --oneline chassis/"
say "  2. Confirm critical customer files are present:"
say "       ls -la $TARGET_DIR/{.env,chassis.config.yaml,CLAUDE.md,HEARTBEATS.md,.mcp.json}"
say "       ls -d $TARGET_DIR/{scheduled-tasks,skills,plugins,scripts,data,state,briefings,memory,logs}"
say "  3. Run bootstrap.sh in re-bootstrap mode to re-render anything that"
say "     references chassis paths (launchd plists, customer scripts):"
say "       cd $TARGET_DIR && CHASSIS_HOME=\$(pwd) bash bootstrap.sh"
say "  4. Smoke-test the dispatcher in DRY_RUN mode before re-enabling the"
say "     launchd unit:"
say "       DRY_RUN=true bash $TARGET_DIR/chassis/scheduled-tasks/heartbeat-dispatcher.sh"
say "  5. Watch the first dispatcher tick after re-enable to confirm the"
say "     telemetry pipeline still works:"
say "       tail -f $TARGET_DIR/logs/scheduled/\$(date +%Y-%m-%d)-dispatcher.log"
say ""
if [[ "$DRY_RUN" == "true" ]]; then
    say "(dry-run: nothing was actually changed)"
fi
