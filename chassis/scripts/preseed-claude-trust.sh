#!/usr/bin/env bash
# preseed-claude-trust.sh - ensure Claude Code has CUSTOMER_HOME marked as
# trusted in ~/.claude.json, so claude launches into that directory don't
# park on the first-run folder-trust prompt.
#
# Background (chassis#21):
#   Claude Code stores folder-trust per absolute path in ~/.claude.json
#   under projects["<abs path>"].hasTrustDialogAccepted. The flag
#   --dangerously-skip-permissions does NOT bypass this gate; it controls
#   the per-tool permission classifier, which only runs after trust is
#   established. The #6 CUSTOMER_HOME migration moved the Discord launch
#   cwd to ${CUSTOMER_HOME} (typically ~/.behalfbot), a path no install
#   had ever trusted - so every restart parked on the trust prompt,
#   waiting on keyboard input that nothing was around to answer.
#
# Why this is its own script:
#   chassis#23 follow-up - the original PR put this logic only inside
#   bootstrap-customer-scripts.sh, so the recovery path (watchdog → restart
#   script → claude) didn't re-seed trust after a wipe. The watchdog would
#   detect the parked prompt, run restart, claude would park again, and
#   the bot would infinite-loop with no actual recovery. Extracting into
#   a standalone idempotent script lets BOTH bootstrap and the restart
#   template call the same pre-seed before launching claude.
#
# Usage:
#   bash chassis/scripts/preseed-claude-trust.sh <CUSTOMER_HOME>
#
# Idempotent. Safe to call from restart templates on every restart.
#
# Exit codes:
#   0 - trust entry present (either pre-existing or freshly seeded)
#   0 - python3 missing, warning printed, caller should treat as soft-fail
#       (restart can still proceed; first claude launch will hit the prompt)
#   1 - ~/.claude.json is invalid JSON or has unexpected shape;
#       refuses to clobber. Caller must investigate manually.

set -euo pipefail

TARGET_DIR="${1:-}"
if [[ -z "$TARGET_DIR" ]]; then
    echo "Usage: preseed-claude-trust.sh <abs-path-to-trust>" >&2
    exit 2
fi

CLAUDE_CONFIG="${CLAUDE_CONFIG:-$HOME/.claude.json}"

if ! command -v python3 >/dev/null 2>&1; then
    echo "  WARN: python3 not on PATH; skipping claude-trust pre-seed for $TARGET_DIR" >&2
    echo "        Next claude launch into $TARGET_DIR will hit the trust prompt." >&2
    exit 0
fi

if ! python3 - "$CLAUDE_CONFIG" "$TARGET_DIR" <<'PYEOF'
import json
import os
import sys
import tempfile

config_path, target_dir = sys.argv[1], sys.argv[2]

if os.path.exists(config_path):
    try:
        with open(config_path) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"  ERROR: {config_path} is not valid JSON: {e}", file=sys.stderr)
        print(f"         Refusing to overwrite. Fix or delete the file and re-run.", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict):
        print(f"  ERROR: {config_path} top-level is not a JSON object; refusing to edit.", file=sys.stderr)
        sys.exit(1)
else:
    data = {}

projects = data.setdefault("projects", {})
if not isinstance(projects, dict):
    print(f"  ERROR: {config_path} 'projects' is not an object; refusing to edit.", file=sys.stderr)
    sys.exit(1)

entry = projects.setdefault(target_dir, {})
if not isinstance(entry, dict):
    print(f"  ERROR: {config_path} projects['{target_dir}'] is not an object; refusing to edit.", file=sys.stderr)
    sys.exit(1)

already_trusted = entry.get("hasTrustDialogAccepted") is True and entry.get("hasCompletedProjectOnboarding") is True
entry["hasTrustDialogAccepted"] = True
entry["hasCompletedProjectOnboarding"] = True

tmp_fd, tmp_path = tempfile.mkstemp(prefix=".claude.json.", dir=os.path.dirname(config_path) or ".")
try:
    with os.fdopen(tmp_fd, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp_path, config_path)
except Exception:
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
    raise

if already_trusted:
    print(f"  claude folder-trust for {target_dir}: already set (no-op)")
else:
    print(f"  claude folder-trust for {target_dir}: pre-seeded in {config_path}")
PYEOF
then
    echo "  ERROR: claude-trust pre-seed for $TARGET_DIR failed." >&2
    exit 1
fi
