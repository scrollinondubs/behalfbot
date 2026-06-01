#!/usr/bin/env bash
# verify-match.sh — Reverse-image-search a dating-match profile photo via the
# four-engine consensus (TinEye + Google Lens + PimEyes + Yandex).
#
# Wraps verify-match.py + a dedicated Playwright venv at ${CHASSIS_HOME}/.venv-dating.
#
# Usage:
#   verify-match.sh --name "Jane" --platform hinge --photo /path/to/face.jpg
#
# Exit codes:
#   0 = green / no_signal (proceed with reply)
#   1 = yellow / unknown (review — surface to installer's social channel)
#   2 = red / catfish (drop comms — silent auto-reject)

set -euo pipefail

# Resolve plugin root: directory containing this script's parent.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(dirname "$SCRIPT_DIR")"

# CHASSIS_HOME is set by chassis bootstrap; fall back to the plugin's
# grandparent (assumes plugins/dating/scripts/verify-match.sh layout).
CHASSIS_HOME="${CHASSIS_HOME:-$(cd "$PLUGIN_DIR/../.." && pwd)}"

VENV="${DATING_VERIFY_VENV:-${CHASSIS_HOME}/.venv-dating}"
PY="$VENV/bin/python"
SCRIPT="$SCRIPT_DIR/verify-match.py"

if [[ ! -x "$PY" ]]; then
  echo "verify-match: venv missing at $VENV. Recreate with:" >&2
  echo "  python3 -m venv \"$VENV\"" >&2
  echo "  \"$VENV/bin/pip\" install playwright" >&2
  echo "  \"$VENV/bin/playwright\" install chromium" >&2
  exit 1
fi

if [[ ! -f "$SCRIPT" ]]; then
  echo "verify-match: $SCRIPT not found" >&2
  exit 1
fi

exec "$PY" "$SCRIPT" "$@"
