#!/usr/bin/env bash
# test-dream-gather.sh - behavioural tests for gather-dream.sh (#219).
#
# The gather decides, for free, whether the paid dream run has anything to do.
# Its two failure modes are both expensive: firing every night on a finding the
# last run already reported (a $1.50 nightly loop on nothing new), and going
# silent when it cannot run (a monitor that says nothing while broken). Both
# halves are asserted.
#
# Scenarios:
#   1. clean store, recent run                          -> count 0
#   2. first run ever (no state file)                    -> count 1, rotation_due
#   3. last run 48h ago                                  -> count 1, rotation_due
#   4. a new dead [[link]]                               -> count 1, new_dead_links
#   5. same dead link recorded as baseline               -> count 0 (no re-fire)
#   6. an index out of date                              -> count 1, index_drift
#   7. store not found                                   -> count 1, probe_failed
#   8. store without MEMORY.md                           -> count 1, probe_failed
#   9. payload carries what the prompt must not guess    -> fields present
#  10. ALWAYS exit 0, stdout is one JSON object
#  11. store nested in another git repo                 -> store_is_git false
#  12. store that is its own git repo                   -> store_is_git true
#
# No network, no docker, no git remote. Exit 0 all pass, 1 on failure.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATHER="${SCRIPT_DIR}/gather-dream.sh"

if ! command -v jq >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
    echo "test-dream-gather: jq and python3 required" >&2
    exit 2
fi

fail=0
pass=0
TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

# make_store <name> - a minimal healthy two-level store and an install root.
make_store() {
    local root="$TMP/$1"
    mkdir -p "$root/home/scheduled-tasks" "$root/store"
    cat > "$root/store/MEMORY.md" <<'EOF'
# Root

- [Tools](index-tools.md) - tools domain
EOF
    cat > "$root/store/feedback_linked.md" <<'EOF'
---
name: Linked
description: A correction that is listed.
type: feedback
domain: tools
---
body
EOF
    printf '# Tools\n\n- [Linked](feedback_linked.md) - A correction that is listed.\n' \
        > "$root/store/index-tools.md"
    printf '%s' "$root"
}

# write_state <root> <hours-ago> [baseline-json]
write_state() {
    local root="$1" hours="$2" baseline="${3:-}"
    local ts
    ts="$(python3 -c "import datetime,sys; print((datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=float(sys.argv[1]))).strftime('%Y-%m-%dT%H:%M:%SZ'))" "$hours")"
    if [[ -n "$baseline" ]]; then
        jq -n --arg ts "$ts" --argjson b "$baseline" '{lastDreamTime:$ts, domainCursor:0, lastIntegrity:$b}' \
            > "$root/home/scheduled-tasks/dream-state.json"
    else
        jq -n --arg ts "$ts" '{lastDreamTime:$ts, domainCursor:0}' > "$root/home/scheduled-tasks/dream-state.json"
    fi
}

run_gather() {
    local root="$1" store="${2:-$1/store}"
    env -u CLAUDE_AUTO_MEMORY_DIR CUSTOMER_HOME="$root/home" CLAUDE_AUTO_MEMORY_DIR="$store" \
        HOME="$root/home" bash "$GATHER"
}

# assert_gather <name> <root> <want-count> [want-reason] [store]
assert_gather() {
    local name="$1" root="$2" want="$3" reason="${4:-}" store="${5:-$2/store}"
    local out rc count
    out="$(run_gather "$root" "$store")"
    rc=$?
    if [[ $rc -ne 0 ]]; then
        printf '  FAIL %s: exited %d (a non-zero gather exit reads as count=0)\n' "$name" "$rc"
        fail=$((fail + 1)); return
    fi
    if [[ "$(printf '%s\n' "$out" | grep -c .)" != "1" ]] || ! printf '%s' "$out" | jq -e 'type=="object"' >/dev/null 2>&1; then
        printf '  FAIL %s: stdout is not one JSON object: %s\n' "$name" "$out"
        fail=$((fail + 1)); return
    fi
    count="$(printf '%s' "$out" | jq -r '.count')"
    if [[ "$count" != "$want" ]]; then
        printf '  FAIL %s: expected count %s, got %s\n       | %s\n' "$name" "$want" "$count" "$out"
        fail=$((fail + 1)); return
    fi
    if [[ -n "$reason" ]] && ! printf '%s' "$out" | jq -e --arg r "$reason" '.reasons | index($r)' >/dev/null 2>&1; then
        printf '  FAIL %s: reasons lack %s\n       | %s\n' "$name" "$reason" "$out"
        fail=$((fail + 1)); return
    fi
    printf '  ok   %s (count=%s%s)\n' "$name" "$count" "${reason:+, $reason}"
    pass=$((pass + 1))
    LAST_OUT="$out"
}

echo "test-dream-gather"

r="$(make_store clean)"
write_state "$r" 2
assert_gather "clean store, recent run is silent" "$r" 0

r="$(make_store first)"
assert_gather "first run ever fires the rotation" "$r" 1 rotation_due

r="$(make_store stale)"
write_state "$r" 48
assert_gather "last run 48h ago fires the rotation" "$r" 1 rotation_due

r="$(make_store deadlink)"
write_state "$r" 2
printf -- '---\nname: Linked\ndescription: A correction that is listed.\ntype: feedback\ndomain: tools\n---\nsee [[feedback_gone]]\n' \
    > "$r/store/feedback_linked.md"
assert_gather "a new dead note link fires" "$r" 1 new_dead_links

# The load-bearing property: a finding the last run recorded must not re-fire
# the paid run every night until a human fixes it.
counts="$(printf '%s' "$LAST_OUT" | jq -c '.integrity + {unlisted_orphans: .index.unlisted_orphans}')"
write_state "$r" 2 "$counts"
assert_gather "the same dead link, already in the baseline, stays silent" "$r" 0

r="$(make_store drift)"
write_state "$r" 2
printf '# Tools\n\n- [Old title](feedback_linked.md) - stale wording\n' > "$r/store/index-tools.md"
assert_gather "an out-of-date index fires" "$r" 1 index_drift

r="$(make_store nostore)"
write_state "$r" 2
assert_gather "missing store fires probe_failed" "$r" 1 probe_failed "$TMP/does-not-exist"

r="$(make_store nomemory)"
write_state "$r" 2
rm "$r/store/MEMORY.md"
assert_gather "store without MEMORY.md fires probe_failed" "$r" 1 probe_failed

r="$(make_store payload)"
printf 'second_brain:\n  backend: obsidian\n  mode: adapter\nmemory:\n  dream_inbox_doc: "Inbox/Dream.md"\n' \
    > "$r/home/chassis.config.yaml"
assert_gather "payload on a fired run" "$r" 1 rotation_due
missing="$(printf '%s' "$LAST_OUT" | jq -r '[ "memory_dir","customer_home","chassis_scripts","state_file",
    "store_is_git","customer_repo","second_brain_mode","inbox_doc","tonight_domain","integrity",
    "baseline","new_since_baseline","index" ] - (keys) | join(",")')"
if [[ -z "$missing" ]]; then
    printf '  ok   payload carries every field the prompt reads\n'
    pass=$((pass + 1))
else
    printf '  FAIL payload missing: %s\n' "$missing"
    fail=$((fail + 1))
fi
if python3 -c 'import yaml' >/dev/null 2>&1; then
    got="$(printf '%s' "$LAST_OUT" | jq -r '[.second_brain_mode, .inbox_doc, .tonight_domain] | join("|")')"
    if [[ "$got" == "adapter|Inbox/Dream.md|index-tools.md" ]]; then
        printf '  ok   payload reads mode, inbox doc and tonight'"'"'s domain from config and store\n'
        pass=$((pass + 1))
    else
        printf '  FAIL payload values: %s\n' "$got"
        fail=$((fail + 1))
    fi
else
    echo "  skip config values (python3 has no PyYAML; the chassis image ships it)"
fi

# store_is_git means the store is its OWN repo. A store nested in the install
# repo must report false, or the prompt commits into the install repo's
# checked-out branch unattended.
r="$(make_store nested)"
write_state "$r" 48
git -C "$r" init -q
assert_gather "store nested in another repo" "$r" 1 rotation_due
got="$(printf '%s' "$LAST_OUT" | jq -r '[.store_is_git, (.store_git_toplevel != "")] | map(tostring) | join(",")')"
if [[ "$got" == "false,true" ]]; then
    printf '  ok   nested store is not its own repo (store_is_git false, toplevel named)\n'
    pass=$((pass + 1))
else
    printf '  FAIL nested store: store_is_git,toplevel-set = %s\n' "$got"
    fail=$((fail + 1))
fi
git -C "$r/store" init -q
assert_gather "store with its own repo" "$r" 1 rotation_due
if [[ "$(printf '%s' "$LAST_OUT" | jq -r '.store_is_git')" == "true" ]]; then
    printf '  ok   store with its own repo reports store_is_git true\n'
    pass=$((pass + 1))
else
    printf '  FAIL store with its own repo: %s\n' "$(printf '%s' "$LAST_OUT" | jq -c '{store_is_git,store_git_toplevel}')"
    fail=$((fail + 1))
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ $fail -eq 0 ]] || exit 1
