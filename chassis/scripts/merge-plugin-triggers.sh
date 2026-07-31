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
#   - For each enabled plugin, resolves its openclaw.plugin.json the same way
#     every other plugin-tree consumer does: via CHASSIS_PLUGINS_ROOT (the
#     resolved baked+fetched overlay from resolve-plugin-root.sh / _env.sh —
#     behalfbot#95), falling back to $CUSTOMER_HOME/plugins/<plugin> for
#     customer-local-only plugins (e.g. midnight-oil) that are never baked or
#     fetched — see chassis/scripts/fetch-plugins.sh's note on that directory.
#     Pulls the contracts.triggers array.
#   - Replaces the `triggers:` block in chassis/triggers.yaml with the merged
#     set, preserving order: chassis-shipped triggers first (from the template's
#     marker block), then plugin-declared triggers in plugin-id alphabetical order
#   - Asserts that every enabled plugin resolves to a manifest somewhere in
#     that search (resolved root, then customer-local), and fails loudly
#     (nonzero exit) instead of silently merging zero triggers when the
#     lookup points at the wrong directory — see behalfbot#98.
#
# Required env:
#   CHASSIS_HOME — absolute path to the chassis directory
#
# Optional env:
#   CHASSIS_PLUGINS_ROOT — resolved plugin root; resolved via _env.sh /
#       resolve-plugin-root.sh when unset
#   CUSTOMER_HOME — customer state root (holds the local-only plugins/ dir);
#       defaults to CHASSIS_HOME when unset, same as _env.sh
#   DRY_RUN — if "true", emit the merged YAML to stdout instead of writing to disk
#
# Idempotent: running twice with no plugin changes produces an identical file.

set -euo pipefail

: "${CHASSIS_HOME:?CHASSIS_HOME must be set}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_env.sh"

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
enabled_plugins=$(python3 - "$CONFIG" <<'PY'
import sys
import re

# Tiny YAML parser sufficient for chassis.config.yaml's modules block. Avoids
# shipping PyYAML as a hard dep. Handles the modules: <plugin>: enabled: <bool>
# pattern only.
path = sys.argv[1]
in_modules = False
plugin = None
plugins = []
for raw in open(path):
    line = raw.rstrip("\n")
    if not line.strip() or line.lstrip().startswith("#"):
        continue
    indent = len(line) - len(line.lstrip())
    stripped = line.strip()
    if indent == 0:
        in_modules = stripped == "modules:"
        plugin = None
        continue
    if not in_modules:
        continue
    if indent == 2 and stripped.endswith(":"):
        plugin = stripped[:-1]
        continue
    if indent == 4 and plugin is not None:
        m = re.match(r"enabled:\s*(true|false)\b", stripped)
        if m and m.group(1) == "true":
            plugins.append(plugin)
            plugin = None  # one shot per plugin
        elif m and m.group(1) == "false":
            plugin = None
print("\n".join(sorted(plugins)))
PY
)

# Build merged triggers section. Resolve each plugin's manifest via
# CHASSIS_PLUGINS_ROOT first (the same resolved baked+fetched overlay every
# other plugin-tree consumer uses), then fall back to $CUSTOMER_HOME/plugins
# for customer-local-only plugins that are never baked or fetched.
merged=$(CHASSIS_PLUGINS_ROOT="$CHASSIS_PLUGINS_ROOT" CUSTOMER_HOME="$CUSTOMER_HOME" python3 - <<PY
import json
import os
import sys
from pathlib import Path

plugins_root = Path(os.environ["CHASSIS_PLUGINS_ROOT"])
customer_plugins = Path(os.environ["CUSTOMER_HOME"]) / "plugins"
plugin_names = """$enabled_plugins""".strip().splitlines()

entries = []
not_found = []
for name in plugin_names:
    manifest = plugins_root / name / "openclaw.plugin.json"
    source = "plugins_root"
    if not manifest.exists():
        manifest = customer_plugins / name / "openclaw.plugin.json"
        source = "customer_local"
    if not manifest.exists():
        # Assertion (behalfbot#98): an enabled plugin with no manifest in
        # EITHER the resolved plugin root or the customer-local tree is
        # unresolvable — the exact silent-drop shape this issue exists to
        # catch (every enabled plugin used to resolve to nothing whenever
        # the lookup pointed at the wrong directory, and this script kept
        # printing a success line anyway). Fail loudly instead.
        not_found.append(name)
        continue
    try:
        data = json.loads(manifest.read_text())
    except Exception as e:
        print(f"WARN: skipping {name} ({source}): {e}", file=sys.stderr)
        continue
    triggers = data.get("contracts", {}).get("triggers", []) or []
    plugin_id = data.get("id", name)
    for t in triggers:
        # Stamp plugin id from the manifest, override any plugin field in the
        # entry — chassis trusts the manifest as authoritative.
        t = dict(t)
        t["plugin"] = plugin_id
        entries.append(t)

if not_found:
    print(
        f"ERROR: enabled plugin(s) have no manifest under {plugins_root} or {customer_plugins}: "
        + ", ".join(not_found),
        file=sys.stderr,
    )
    sys.exit(1)

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
