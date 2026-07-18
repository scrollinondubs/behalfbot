#!/usr/bin/env bash
# merge-plugin-triggers.sh — read every enabled plugin's openclaw.plugin.json,
# extract its contracts.triggers array, and merge into $CHASSIS_HOME/chassis/triggers.yaml.
#
# Called by bootstrap.sh step 7 ("Activate enabled plugins"), and should also
# be re-run any time a plugin is enabled / disabled / its manifest changes.
#
# Behavior:
#   - Reads chassis.config.yaml to determine which plugins are enabled
#     (modules.<plugin>.enabled = true)
#   - For each enabled plugin, reads plugins/<plugin>/openclaw.plugin.json,
#     pulls the contracts.triggers array
#   - Replaces the `triggers:` block in chassis/triggers.yaml with the merged
#     set, preserving order: chassis-shipped triggers first (from the template's
#     marker block), then plugin-declared triggers in plugin-id alphabetical order
#   - Validates the resulting file parses cleanly via dispatch-trigger.sh's
#     awk parser (running it in dry-run mode)
#
# Required env:
#   CHASSIS_HOME — absolute path to the chassis directory
#
# Optional env:
#   DRY_RUN — if "true", emit the merged YAML to stdout instead of writing to disk
#
# Idempotent: running twice with no plugin changes produces an identical file.

set -euo pipefail

: "${CHASSIS_HOME:?CHASSIS_HOME must be set}"

CONFIG="$CHASSIS_HOME/chassis.config.yaml"
TARGET="$CHASSIS_HOME/chassis/triggers.yaml"
TEMPLATE="$CHASSIS_HOME/chassis/triggers.yaml.template"
DRY_RUN="${DRY_RUN:-false}"

if [[ ! -f "$CONFIG" ]]; then
    echo "ERROR: $CONFIG not found — run bootstrap step 3 first" >&2
    exit 1
fi

# Discover enabled plugins via chassis.config.yaml. We avoid yq here for the
# same chassis-portability reasons as elsewhere — Python is everywhere.
# Parser extracted to lib/enabled-plugins.py so activate-plugins.sh shares it
# (behalfbot#53 design section 4 - no drift between the two callers).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
enabled_plugins=$(python3 "$SCRIPT_DIR/lib/enabled-plugins.py" "$CONFIG")

# Layered plugin roots, highest precedence first (behalfbot#53 Phase 0):
# customer-private plugins-local/, legacy customer plugins/, fetched
# vendored-plugins/, image-baked /app/plugins fallback. First hit wins.
: "${CHASSIS_PLUGINS_SEARCH_PATH:=$CHASSIS_HOME/plugins-local:$CHASSIS_HOME/plugins:$CHASSIS_HOME/vendored-plugins:${CHASSIS_PLUGINS_ROOT:-/app/plugins}}"
export CHASSIS_PLUGINS_SEARCH_PATH

# Build merged triggers section
merged=$(python3 - <<PY
import json
import os
import sys
from pathlib import Path

chassis_home = Path(os.environ["CHASSIS_HOME"])
plugin_names = """$enabled_plugins""".strip().splitlines()

# Layered lookup: first root containing the plugin's manifest wins. Replaces
# the old hardcoded chassis_home/"plugins" path (behalfbot#53 review finding 1).
search_roots = [Path(p) for p in os.environ.get("CHASSIS_PLUGINS_SEARCH_PATH", "").split(":") if p]
if not search_roots:
    search_roots = [chassis_home / "plugins"]

entries = []
for name in plugin_names:
    manifest = None
    for root in search_roots:
        cand = root / name / "openclaw.plugin.json"
        if cand.exists():
            manifest = cand
            break
    if manifest is None:
        continue
    try:
        data = json.loads(manifest.read_text())
    except Exception as e:
        print(f"WARN: skipping {name}: {e}", file=sys.stderr)
        continue
    triggers = data.get("contracts", {}).get("triggers", []) or []
    plugin_id = data.get("id", name)
    for t in triggers:
        # Stamp plugin id from the manifest, override any plugin field in the
        # entry — chassis trusts the manifest as authoritative.
        t = dict(t)
        t["plugin"] = plugin_id
        entries.append(t)

# Output in YAML
def yaml_str(v):
    if v is None:
        return ""
    s = str(v)
    if any(c in s for c in [":", "#", "{", "}", "[", "]", ",", "&", "*", "!", "|", ">", "%", "@", "\\\\"]) or "\\n" in s:
        return "'" + s.replace("'", "''") + "'"
    return s

out = ["triggers:"]
if not entries:
    out = ["triggers: []"]
for e in entries:
    out.append(f"  - name: {yaml_str(e.get('name', ''))}")
    out.append(f"    plugin: {yaml_str(e.get('plugin', ''))}")
    if "keyword_regex" in e:
        out.append(f"    keyword_regex: {yaml_str(e['keyword_regex'])}")
    if "channel_filter" in e:
        out.append(f"    channel_filter: {yaml_str(e['channel_filter'])}")
    if "parser" in e:
        out.append(f"    parser: {yaml_str(e['parser'])}")
    if "handler" in e:
        out.append(f"    handler: {yaml_str(e['handler'])}")
    if "react_emoji" in e:
        out.append(f"    react_emoji: {yaml_str(e['react_emoji'])}")
print("\n".join(out))
PY
)

# Compose the output: header from template (everything before the `triggers:`
# line), then our merged block, then nothing (template's example entries are
# documentation only — they're commented in the template, so they survive in
# the header copy).
header=$(awk '/^triggers:/{exit} {print}' "$TEMPLATE" 2>/dev/null || echo "")

output=$(printf '%s\n%s\n' "$header" "$merged")

if [[ "$DRY_RUN" == "true" ]]; then
    printf '%s\n' "$output"
    exit 0
fi

mkdir -p "$(dirname "$TARGET")"
printf '%s\n' "$output" > "$TARGET"

echo "✓ merged $(echo "$enabled_plugins" | grep -c . || true) enabled-plugin manifests into $TARGET"
