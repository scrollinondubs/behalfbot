#!/usr/bin/env bash
# gather-dream.sh - decides whether tonight's dream run has work, at no model
# cost (#219).
#
# Dream is the opt-in nightly memory consolidation pass over the auto-memory
# notes store (chassis/memory/MEMORY_MODEL.md, chassis/docs/heartbeats/dream.md).
# The deterministic half lives here: it runs memory-integrity.py and the
# memory-index-build.py check, compares the counts with what the last run
# recorded, and fires only when there is something to do.
#
# Fires ({"count": 1, ...}) when any of these hold:
#   rotation_due             - DREAM_MIN_HOURS (default 20) since the last
#                              rotation run, or no run recorded yet
#   new_dead_links           - more dead note links than the last run recorded
#   new_orphans              - more orphan notes than the last run recorded
#   new_bad_second_brain_refs- more malformed second-brain refs than recorded
#   index_drift              - memory-index-build.py would rewrite an index,
#                              or more notes are listed by no index (and not
#                              by MEMORY.md) than the last run recorded
#   probe_failed             - a check could not run (store not found, a script
#                              crashed); details in `probes`
# Otherwise {"count": 0} and the heartbeat stays silent.
#
# 20h rather than 24h: the heartbeat is daily, and dispatcher jitter can land
# tonight's tick a few minutes earlier than last night's. A 24h gate would then
# skip every other night.
#
# The baseline for "new" is .lastIntegrity in the dream state file, which the
# dream prompt writes in its last phase. Absent baseline means every non-zero
# count is new. This script only reads state; it never writes it.
#
# State lives in its own file, $CUSTOMER_HOME/scheduled-tasks/dream-state.json,
# never in heartbeat-state.json: the dispatcher owns that file and rewrites it
# every tick, and a prompt doing jq > tmp && mv on it would race the dispatcher.
#
# ALWAYS exits 0. The dispatcher reads a non-zero gather exit as an error AND
# as count=0, so a gather that failed loudly would go silent exactly when it
# mattered. Failures travel inside the payload as probe_failed instead.
#
# Env:
#   CUSTOMER_HOME           install root (default ~/.behalfbot)
#   CLAUDE_AUTO_MEMORY_DIR  notes store; required on a containerized install,
#                           see chassis/scripts/_memory_store.py
#   DREAM_MIN_HOURS         rotation gate in hours (default 20)
#   DREAM_STATE_FILE        state file override (tests)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
CUSTOMER_HOME="${CUSTOMER_HOME:-${HOME}/.behalfbot}"
STATE_FILE="${DREAM_STATE_FILE:-$CUSTOMER_HOME/scheduled-tasks/dream-state.json}"

if ! command -v python3 >/dev/null 2>&1; then
    printf '{"count": 1, "reasons": ["probe_failed"], "probes": [{"kind": "probe_failed", "detail": "python3 not installed; dream checks cannot run"}]}\n'
    exit 0
fi

OUT="$(SCRIPT_DIR="$SCRIPT_DIR" CUSTOMER_HOME="$CUSTOMER_HOME" STATE_FILE="$STATE_FILE" \
    DREAM_MIN_HOURS="${DREAM_MIN_HOURS:-20}" python3 - <<'PY' 2>/dev/null
import contextlib
import datetime
import glob
import importlib.util
import io
import json
import os
import re
import subprocess
import sys

script_dir = os.environ["SCRIPT_DIR"]
customer_home = os.environ["CUSTOMER_HOME"]
state_file = os.environ["STATE_FILE"]
sys.path.insert(0, script_dir)

probes = []


def probe(detail):
    probes.append({"kind": "probe_failed", "detail": detail})


def as_int(v, default=0):
    try:
        return int(v)
    except Exception:
        return default


try:
    min_hours = float(os.environ.get("DREAM_MIN_HOURS") or 20)
except ValueError:
    min_hours = 20.0

try:
    with open(state_file) as f:
        state = json.load(f) or {}
    if not isinstance(state, dict):
        state = {}
except FileNotFoundError:
    state = {}
except Exception as exc:
    state = {}
    probe("dream state file %s unreadable (%s); treating as first run" % (state_file, type(exc).__name__))

now = datetime.datetime.now(datetime.timezone.utc)
hours_since = None
last = state.get("lastDreamTime") or ""
if last:
    try:
        ts = datetime.datetime.fromisoformat(last.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        hours_since = round((now - ts).total_seconds() / 3600, 1)
    except Exception:
        hours_since = None
rotation_due = hours_since is None or hours_since >= min_hours

# Config: second-brain mode, the inbox doc id, the assistant name for the
# inbox title. PyYAML ships in the chassis image; on a host without it the
# prompt is told the mode is unknown and keeps the inbox block in its report.
# Not a probe failure: an unreadable config must not fire a paid run by
# itself, it only means the prompt treats the mode as unknown.
cfg = {}
config_note = ""
cfg_path = os.path.join(customer_home, "chassis.config.yaml")
try:
    import yaml

    with open(cfg_path) as f:
        cfg = yaml.safe_load(f) or {}
except FileNotFoundError:
    config_note = "no chassis.config.yaml at %s" % cfg_path
except Exception as exc:
    config_note = "chassis.config.yaml not read (%s)" % type(exc).__name__


def cfg_get(*path):
    cur = cfg
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


sb_mode = (cfg_get("second_brain", "mode") or "direct") if cfg else "unknown"
inbox_doc = cfg_get("memory", "dream_inbox_doc") or state.get("inboxDocId") or ""
assistant = cfg_get("identity", "assistant", "name") or ""

customer_repo = ""
try:
    origin = subprocess.run(["git", "-C", customer_home, "remote", "get-url", "origin"],
                            capture_output=True, text=True, timeout=10).stdout.strip()
    m = re.search(r"github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?/?$", origin)
    if m and m.group(1).lower() != "scrollinondubs/behalfbot":
        customer_repo = m.group(1)
except Exception:
    pass

from _memory_store import resolve_store  # noqa: E402

store, store_how, searched = resolve_store(None)
if store is None:
    probe("notes store not found (searched: %s); set CLAUDE_AUTO_MEMORY_DIR" % ", ".join(searched))
    print(json.dumps({"count": 1, "reasons": ["probe_failed"], "probes": probes,
                      "customer_home": customer_home}))
    sys.exit(0)
store = str(store)
if not os.path.isfile(os.path.join(store, "MEMORY.md")):
    probe("no MEMORY.md in %s (%s); wrong directory, or the store was never set up" % (store, store_how))

store_is_git = subprocess.run(["git", "-C", store, "rev-parse", "--is-inside-work-tree"],
                              capture_output=True, text=True).stdout.strip() == "true"

indexes = sorted(os.path.basename(p) for p in glob.glob(os.path.join(store, "index-*.md")))
cursor = as_int(state.get("domainCursor"), 0)
tonight = indexes[cursor % len(indexes)] if indexes else None

integrity = {}
try:
    out = subprocess.run(
        [sys.executable, os.path.join(script_dir, "memory-integrity.py"), "--json",
         "--store", store, "--repo", customer_home],
        capture_output=True, text=True, timeout=120,
    )
    data = json.loads(out.stdout)
    if "error" in data:
        probe("memory-integrity.py: %s" % data["error"])
    else:
        integrity = data.get("counts", {})
except Exception as exc:
    probe("memory-integrity.py: %s" % type(exc).__name__)

index = {}
try:
    spec = importlib.util.spec_from_file_location(
        "memory_index_build", os.path.join(script_dir, "memory-index-build.py"))
    mib = importlib.util.module_from_spec(spec)
    # @dataclass resolves its module through sys.modules; without this, exec fails.
    sys.modules["memory_index_build"] = mib
    spec.loader.exec_module(mib)
    with contextlib.redirect_stdout(io.StringIO()):
        r = mib.build(store)
    index = {
        "would_rewrite": sorted(ix.file for d, ix in r["indexes"].items() if r["rendered"][d] != ix.text),
        "missing_indexes": r["missing_indexes"],
        "unlisted_orphans": len(r["unlisted_orphans"]),
        "unlisted_memory_only": len(r["unlisted_memory_only"]),
        "dead_pointer_lines": sum(len(ix.dead_pointer_links) for ix in r["indexes"].values()),
        "superseded": len(r["superseded"]),
        "missing_name_or_description": len(r["missing_fields"]),
        "domain_inferred": len(r["inferred"]),
        "domain_explicit": len(r["explicit"]),
    }
except Exception as exc:
    probe("memory-index-build.py: %s" % type(exc).__name__)

baseline = state.get("lastIntegrity") or {}
TRACKED = {
    "dead_note_link": "new_dead_links",
    "orphan_note": "new_orphans",
    "bad_second_brain_ref": "new_bad_second_brain_refs",
}
new = {}
reasons = []
for key, reason in TRACKED.items():
    new[key] = max(0, as_int(integrity.get(key)) - as_int(baseline.get(key)))
    if new[key] > 0:
        reasons.append(reason)
if index:
    new["unlisted_orphans"] = max(0, index["unlisted_orphans"] - as_int(baseline.get("unlisted_orphans")))
    if index["would_rewrite"] or index["missing_indexes"] or new["unlisted_orphans"] > 0:
        reasons.append("index_drift")
if rotation_due:
    reasons.insert(0, "rotation_due")
if probes:
    reasons.append("probe_failed")

if not reasons:
    print(json.dumps({"count": 0}))
    sys.exit(0)

print(json.dumps({
    "count": 1,
    "kind": "dream",
    "reasons": reasons,
    "probes": probes,
    "rotation_due": rotation_due,
    "hours_since_last": hours_since,
    "customer_home": customer_home,
    "chassis_scripts": script_dir,
    "state_file": state_file,
    "memory_dir": store,
    "memory_dir_source": store_how,
    "store_is_git": store_is_git,
    "customer_repo": customer_repo,
    "second_brain_mode": sb_mode,
    "config_note": config_note,
    "inbox_doc": inbox_doc,
    "assistant_name": assistant,
    "memory_files": len(glob.glob(os.path.join(store, "*.md"))),
    "domain_cursor": cursor,
    "domains_total": len(indexes),
    "tonight_domain": tonight,
    "domain_resume": state.get("domainResume") or "",
    "integrity": integrity,
    "baseline": {k: as_int(baseline.get(k)) for k in list(TRACKED) + ["unlisted_orphans"]},
    "new_since_baseline": new,
    "index": index,
}))
PY
)"

if [[ -z "$OUT" ]]; then
    printf '{"count": 1, "reasons": ["probe_failed"], "probes": [{"kind": "probe_failed", "detail": "gather-dream python block crashed"}]}\n'
    exit 0
fi
printf '%s\n' "$OUT"
exit 0
