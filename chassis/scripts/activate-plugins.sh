#!/usr/bin/env bash
# activate-plugins.sh - real plugin activation (replaces the bootstrap.sh
# activate_plugins() log-only stub; behalfbot#53 Phase 0).
#
# For every ENABLED plugin (chassis.config.yaml modules.<name>.enabled):
#   1. Discover its directory across the layered plugin roots (first hit wins):
#        a. $CUSTOMER_HOME/plugins-local     - customer-private plugins, never
#           in the public behalfbot-plugins repo (e.g. angel-protocol, dating
#           on Sean's install). Highest precedence: local always overrides.
#        b. $CUSTOMER_HOME/plugins           - legacy customer-local plugins
#           (pre-dates this layering; midnight-oil lives here today). Kept for
#           back-compat; new private plugins should use plugins-local/.
#        c. $CUSTOMER_HOME/vendored-plugins  - fetched from behalfbot-plugins
#           at the pinned tag+SHA by fetch-plugins.sh.
#        d. /app/plugins                     - image-baked OFFLINE FALLBACK
#           (migration-era; goes away one VERSION cycle after Phase 2).
#   2. Run its setup.sh (idempotent contract). Failure is a WARN, not fatal.
#   3. Collect contracts.env into $CUSTOMER_HOME/chassis-env.sh.
#   4. Merge contracts.mcpServers into $CUSTOMER_HOME/.mcp.json inside a
#      managed marker ("_managed_by": "behalfbot-plugin:<name>") so re-runs
#      replace rather than duplicate, and manual entries are never touched.
#   5. Re-merge plugin triggers via merge-plugin-triggers.sh.
#
# Idempotent: running twice with no plugin changes produces identical outputs.
# Never exits nonzero for a single plugin failure; exits nonzero only when the
# environment itself is unusable (no config, no python3).

set -euo pipefail

: "${CHASSIS_HOME:?CHASSIS_HOME must be set}"
: "${CUSTOMER_HOME:=$CHASSIS_HOME}"
: "${CHASSIS_ROOT:=/app/chassis}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$CUSTOMER_HOME/chassis.config.yaml"
[[ -f "$CONFIG" ]] || CONFIG="$CHASSIS_HOME/chassis.config.yaml"

log() { printf '[activate-plugins] %s\n' "$*"; }

if [[ ! -f "$CONFIG" ]]; then
    log "ERROR: chassis.config.yaml not found - run earlier bootstrap steps first"
    exit 1
fi
command -v python3 >/dev/null 2>&1 || { log "ERROR: python3 not found"; exit 1; }

# Layered search path, highest precedence first. Overridable for tests.
: "${CHASSIS_PLUGINS_SEARCH_PATH:=$CUSTOMER_HOME/plugins-local:$CUSTOMER_HOME/plugins:$CUSTOMER_HOME/vendored-plugins:${CHASSIS_PLUGINS_ROOT:-/app/plugins}}"
export CHASSIS_PLUGINS_SEARCH_PATH

enabled=$(python3 "$SCRIPT_DIR/lib/enabled-plugins.py" "$CONFIG")
if [[ -z "$enabled" ]]; then
    log "no enabled plugin modules in $CONFIG - nothing to activate"
    exit 0
fi
log "enabled modules: $(tr '\n' ' ' <<<"$enabled")"

# --- discover: name<TAB>dir for each enabled plugin that exists on disk -----
discovered=$(ENABLED="$enabled" python3 - <<'PY'
import os
from pathlib import Path
search = [Path(p) for p in os.environ["CHASSIS_PLUGINS_SEARCH_PATH"].split(":") if p]
for name in os.environ["ENABLED"].split():
    for root in search:
        pdir = root / name
        if (pdir / "openclaw.plugin.json").is_file():
            print(f"{name}\t{pdir}")
            break
PY
)

if [[ -z "$discovered" ]]; then
    log "none of the enabled modules have a plugin directory on disk - done"
    exit 0
fi

# --- run setup.sh per plugin (idempotent contract, WARN on failure) ---------
while IFS=$'\t' read -r name pdir; do
    [[ -z "$name" ]] && continue
    log "activating $name ($pdir)"
    if [[ -f "$pdir/setup.sh" ]]; then
        if ! CHASSIS_HOME="$CHASSIS_HOME" CUSTOMER_HOME="$CUSTOMER_HOME" \
             PLUGIN_DIR="$pdir" bash "$pdir/setup.sh"; then
            log "WARN: $name setup.sh exited nonzero - plugin may be degraded"
        fi
    fi
done <<<"$discovered"

# --- contracts.env -> chassis-env.sh; contracts.mcpServers -> .mcp.json -----
DISCOVERED="$discovered" CUSTOMER_HOME="$CUSTOMER_HOME" CHASSIS_HOME="$CHASSIS_HOME" \
python3 - <<'PY'
import json, os, sys, tempfile
from pathlib import Path

customer = Path(os.environ["CUSTOMER_HOME"])
chassis_home = os.environ["CHASSIS_HOME"]
rows = [r.split("\t") for r in os.environ["DISCOVERED"].splitlines() if r.strip()]

MARKER_KEY = "_managed_by"
MARKER_PREFIX = "behalfbot-plugin:"

def expand(value, plugin_dir):
    if not isinstance(value, str):
        return value
    return (value
            .replace("${CHASSIS_HOME}", chassis_home)
            .replace("${CUSTOMER_HOME}", str(customer))
            .replace("${PLUGIN_DIR}", str(plugin_dir)))

env_lines = []
mcp_servers = {}  # server-name -> (plugin, config)

for name, pdir in rows:
    pdir = Path(pdir)
    try:
        manifest = json.loads((pdir / "openclaw.plugin.json").read_text())
    except Exception as e:
        print(f"[activate-plugins] WARN: {name} manifest unreadable: {e}", file=sys.stderr)
        continue
    contracts = manifest.get("contracts", {}) or {}

    for key, val in sorted((contracts.get("env") or {}).items()):
        env_lines.append(f'export {key}="{expand(val, pdir)}"  # plugin: {name}')

    for sname, sconf in sorted((contracts.get("mcpServers") or {}).items()):
        if sname in mcp_servers:
            print(f"[activate-plugins] WARN: MCP server '{sname}' declared by both "
                  f"{mcp_servers[sname][0]} and {name} - keeping {mcp_servers[sname][0]} "
                  f"(higher-precedence layer)", file=sys.stderr)
            continue
        conf = json.loads(json.dumps(sconf))  # deep copy
        for k in ("command",):
            if k in conf:
                conf[k] = expand(conf[k], pdir)
        if isinstance(conf.get("args"), list):
            conf["args"] = [expand(a, pdir) for a in conf["args"]]
        if isinstance(conf.get("env"), dict):
            conf["env"] = {k: expand(v, pdir) for k, v in conf["env"].items()}
        conf[MARKER_KEY] = MARKER_PREFIX + name
        mcp_servers[sname] = (name, conf)

# chassis-env.sh - fully regenerated each run (managed file).
env_path = customer / "chassis-env.sh"
header = [
    "#!/usr/bin/env bash",
    "# chassis-env.sh - GENERATED by activate-plugins.sh. Do not edit; your",
    "# changes will be overwritten on the next bootstrap. Sourced by the",
    "# dispatcher / launchd / systemd units to expose plugin env contracts.",
    "",
]
env_path.write_text("\n".join(header + env_lines) + "\n")
print(f"[activate-plugins] wrote {env_path} ({len(env_lines)} exports)")

# .mcp.json merge - only inside our managed marker entries.
mcp_path = customer / ".mcp.json"
if not mcp_path.is_file():
    if mcp_servers:
        print(f"[activate-plugins] WARN: {mcp_path} missing - cannot register "
              f"{len(mcp_servers)} plugin MCP server(s). Run bootstrap-mcp-config.sh first.",
              file=sys.stderr)
else:
    try:
        mcp = json.loads(mcp_path.read_text())
    except Exception as e:
        print(f"[activate-plugins] WARN: {mcp_path} unparseable ({e}) - skipping MCP merge",
              file=sys.stderr)
        mcp = None
    if mcp is not None:
        servers = mcp.setdefault("mcpServers", {})
        removed = [k for k, v in servers.items()
                   if isinstance(v, dict) and str(v.get(MARKER_KEY, "")).startswith(MARKER_PREFIX)]
        for k in removed:
            del servers[k]
        added = []
        for sname, (pname, conf) in sorted(mcp_servers.items()):
            if sname in servers:
                print(f"[activate-plugins] WARN: '{sname}' already exists in .mcp.json "
                      f"(unmanaged) - leaving the manual entry alone", file=sys.stderr)
                continue
            servers[sname] = conf
            added.append(sname)
        fd, tmp = tempfile.mkstemp(dir=str(customer), prefix=".mcp.json.")
        with os.fdopen(fd, "w") as f:
            json.dump(mcp, f, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, mcp_path)
        print(f"[activate-plugins] .mcp.json: removed {len(removed)} stale managed "
              f"entr{'y' if len(removed)==1 else 'ies'}, added {added or 'none'}")
PY

# --- trigger merge (already-real machinery, now pointed at the layers) ------
MERGER=""
for cand in \
    "$SCRIPT_DIR/merge-plugin-triggers.sh" \
    "$CHASSIS_ROOT/scripts/merge-plugin-triggers.sh"; do
    [[ -f "$cand" ]] && { MERGER="$cand"; break; }
done
if [[ -n "$MERGER" ]]; then
    if ! CHASSIS_HOME="$CHASSIS_HOME" CHASSIS_PLUGINS_SEARCH_PATH="$CHASSIS_PLUGINS_SEARCH_PATH" \
         bash "$MERGER"; then
        log "WARN: merge-plugin-triggers.sh failed - triggers.yaml may be stale"
    fi
else
    log "WARN: merge-plugin-triggers.sh not found - skipping trigger merge"
fi

log "activation complete"
