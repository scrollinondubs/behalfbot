#!/usr/bin/env bash
# test-alert-transport.sh - CHASSIS_ALERT_TRANSPORT for chassis_alert
# (_alert.sh) and alert-post.sh.
#
# The property that matters most is the one an existing install relies on:
# with the variable unset, nothing changes. Every Discord case below runs
# with the Matrix poster present and armed to record, so "unset means Discord
# only" is proven by the Matrix recorder staying empty, not assumed.
#
# Then the opt-in paths: matrix, the fallback to Discord when Matrix fails,
# both (success when either leg delivered), the transport read from .env when
# the caller's environment has none (launchd jobs), and caller values beating
# .env values.
#
# No network. discord-post.sh, post-to-channel.sh and matrix-post.py are
# stubs that record their argv and stdin.
#
# Exit 0 all pass, 1 on failure, 2 on harness error.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for f in _alert.sh alert-post.sh; do
    if [[ ! -f "${SCRIPT_DIR}/${f}" ]]; then
        echo "test-alert-transport: ${f} not found in ${SCRIPT_DIR}" >&2
        exit 2
    fi
done

fail=0
pass=0
ok() { pass=$((pass + 1)); echo "  ok   $1"; }
bad() { fail=$((fail + 1)); echo "  FAIL $1: $2"; }

TMP="$(mktemp -d)"
cleanup() { find "$TMP" -mindepth 1 -delete 2>/dev/null || true; rmdir "$TMP" 2>/dev/null || true; }
trap cleanup EXIT

LIB="$TMP/lib"
HOMEDIR="$TMP/home"
REC="$TMP/rec"
mkdir -p "$LIB" "$HOMEDIR" "$REC"
cp "${SCRIPT_DIR}/_alert.sh" "${SCRIPT_DIR}/alert-post.sh" "$LIB/"
chmod +x "$LIB/alert-post.sh"

cat > "$LIB/discord-post.sh" <<'STUB'
#!/bin/bash
printf '%s\n' "$@" > "${REC_DIR:?}/discord.args"
rc="$(cat "${REC_DIR}/discord.rc" 2>/dev/null || echo 0)"
exit "$rc"
STUB

cat > "$LIB/post-to-channel.sh" <<'STUB'
#!/bin/bash
# No webhook configured on this fake install: forces the bot-token step.
exit 1
STUB

cat > "$LIB/matrix-post.py" <<'STUB'
import os, sys
rec = os.environ["REC_DIR"]
body = sys.argv[2]
if body == "-":
    body = sys.stdin.read()
with open(os.path.join(rec, "matrix.args"), "w") as fh:
    fh.write("\n".join([sys.argv[1], body] + sys.argv[3:]) + "\n")
with open(os.path.join(rec, "matrix.env"), "w") as fh:
    fh.write("room=%s\n" % os.environ.get("CHASSIS_MATRIX_ALERTS_ROOM", ""))
try:
    rc = int(open(os.path.join(rec, "matrix.rc")).read().strip())
except Exception:
    rc = 0
sys.exit(rc)
STUB
chmod +x "$LIB/discord-post.sh" "$LIB/post-to-channel.sh" "$LIB/matrix-post.py"

PY_DIR="$(dirname "$(command -v python3)")"
BASE_PATH="$PY_DIR:/usr/bin:/bin:/usr/sbin:/sbin"

reset() {
    find "$REC" -mindepth 1 -delete 2>/dev/null || true
    cat > "$HOMEDIR/.env" <<'ENV'
DISCORD_BOT_TOKEN=fake-bot-token
DISCORD_ALERTS_CHANNEL_ID=111
DISCORD_OPS_CHANNEL_ID=222
DISCORD_PRIMARY_CHANNEL_ID=333
CHASSIS_MATRIX_ALERTS_ROOM=!alerts:example.org
ENV
    rm -f "$HOMEDIR/.env.baked"
}

# Run chassis_alert in a clean env. Extra NAME=VALUE pairs go first.
run_alert() {
    env -i PATH="$BASE_PATH" HOME="$TMP" CHASSIS_HOME="$HOMEDIR" REC_DIR="$REC" "$@" \
        bash -c 'set -euo pipefail; source "$0"; chassis_alert "$1"' "$LIB/_alert.sh" "${MSG:-hello ops}"
}

run_post() {
    env -i PATH="$BASE_PATH" HOME="$TMP" CHASSIS_HOME="$HOMEDIR" REC_DIR="$REC" "$@"
}

called() { [[ -f "$REC/$1.args" ]]; }
arg() { sed -n "${2}p" "$REC/$1.args"; }

echo "chassis_alert"

reset
if run_alert; then rc=0; else rc=$?; fi
if [[ $rc -eq 0 ]] && called discord && ! called matrix && [[ "$(arg discord 1)" == "111" ]]; then
    ok "unset transport posts to Discord only, alerts channel"
else
    bad "unset transport" "rc=$rc discord=$(called discord && echo y) matrix=$(called matrix && echo y)"
fi

reset
if run_alert CHASSIS_ALERT_TRANSPORT=discord && called discord && ! called matrix; then
    ok "transport=discord is the same as unset"
else
    bad "transport=discord" "unexpected legs"
fi

reset
if run_alert CHASSIS_ALERT_TRANSPORT=carrier-pigeon && called discord && ! called matrix; then
    ok "unknown transport falls back to Discord"
else
    bad "unknown transport" "unexpected legs"
fi

reset
echo "CHASSIS_ALERT_TRANSPORT=matrix" >> "$HOMEDIR/.env"
if run_alert && called matrix && ! called discord; then
    ok "transport=matrix read from .env when the environment has none"
else
    bad "transport from .env" "matrix=$(called matrix && echo y) discord=$(called discord && echo y)"
fi
if [[ "$(arg matrix 1)" == "alerts" && "$(arg matrix 2)" == "hello ops" ]]; then
    ok "matrix leg gets room key alerts and the message"
else
    bad "matrix args" "$(cat "$REC/matrix.args" 2>/dev/null)"
fi
if grep -q 'room=!alerts:example.org' "$REC/matrix.env" 2>/dev/null; then
    ok "matrix leg sees the room id from .env"
else
    bad "matrix env" "room id not exported to matrix-post.py"
fi

reset
echo "CHASSIS_ALERT_TRANSPORT=discord" > "$HOMEDIR/.env.baked"
echo "CHASSIS_ALERT_TRANSPORT=matrix" >> "$HOMEDIR/.env"
if run_alert && ! called matrix; then
    ok ".env.baked wins over .env for the transport"
else
    bad ".env.baked preference" "matrix leg ran"
fi

reset
if MSG='@/etc/passwd is not a path' run_alert CHASSIS_ALERT_TRANSPORT=matrix \
    && [[ "$(arg matrix 2)" == "@/etc/passwd is not a path" ]]; then
    ok "a message starting with @ reaches Matrix as text, via stdin"
else
    bad "@ message" "$(cat "$REC/matrix.args" 2>/dev/null)"
fi

reset
echo 3 > "$REC/matrix.rc"
if run_alert CHASSIS_ALERT_TRANSPORT=matrix && called matrix && called discord; then
    ok "matrix failure falls back to Discord and still succeeds"
else
    bad "matrix fallback" "no Discord fallback"
fi

reset
if run_alert CHASSIS_ALERT_TRANSPORT=both && called matrix && called discord; then
    ok "both posts to both legs"
else
    bad "both" "missing a leg"
fi

reset
echo 3 > "$REC/discord.rc"
if run_alert CHASSIS_ALERT_TRANSPORT=both; then
    ok "both succeeds when only Matrix delivered"
else
    bad "both, discord down" "reported failure"
fi

reset
echo 3 > "$REC/discord.rc"
echo 3 > "$REC/matrix.rc"
if run_alert CHASSIS_ALERT_TRANSPORT=both; then
    bad "both, all down" "reported success"
else
    ok "both fails when neither leg delivered"
fi

reset
cat > "$LIB/recorder" <<'STUB'
#!/bin/bash
printf '%s\n' "$1" > "${REC_DIR:?}/cmd.args"
STUB
chmod +x "$LIB/recorder"
if run_alert CHASSIS_ALERT_TRANSPORT=matrix CHASSIS_ALERT_CMD="$LIB/recorder" \
    && [[ -f "$REC/cmd.args" ]] && ! called matrix && ! called discord; then
    ok "CHASSIS_ALERT_CMD still overrides the transport"
else
    bad "CHASSIS_ALERT_CMD" "transport legs ran"
fi

echo "alert-post.sh"

reset
out="$(run_post "$LIB/alert-post.sh" alerts "disk 91%")"
if [[ $? -eq 0 && "$(arg discord 1)" == "111" && "$(arg discord 2)" == "disk 91%" ]] && ! called matrix; then
    ok "default: alerts key goes to DISCORD_ALERTS_CHANNEL_ID only"
else
    bad "alert-post default" "$(cat "$REC/discord.args" 2>/dev/null)"
fi
if [[ "$out" == *"matrix=skip discord=ok"* ]]; then
    ok "status line reports matrix=skip discord=ok"
else
    bad "status line" "$out"
fi

reset
run_post "$LIB/alert-post.sh" ops "x" >/dev/null
if [[ "$(arg discord 1)" == "222" ]]; then ok "ops key goes to DISCORD_OPS_CHANNEL_ID"; else bad "ops key" "$(arg discord 1)"; fi

reset
run_post "$LIB/alert-post.sh" dm "x" >/dev/null
if [[ "$(arg discord 1)" == "333" ]]; then ok "dm key goes to DISCORD_PRIMARY_CHANNEL_ID"; else bad "dm key" "$(arg discord 1)"; fi

reset
sed -i.bak '/DISCORD_ALERTS_CHANNEL_ID/d' "$HOMEDIR/.env"
run_post "$LIB/alert-post.sh" alerts "x" >/dev/null
if [[ "$(arg discord 1)" == "222" ]]; then ok "alerts falls back to the ops channel"; else bad "alerts fallback" "$(arg discord 1)"; fi

reset
run_post "$LIB/alert-post.sh" --discord-channel 999 alerts "x" >/dev/null
if [[ "$(arg discord 1)" == "999" ]]; then ok "--discord-channel overrides the key"; else bad "override" "$(arg discord 1)"; fi

reset
printf 'body from file\n' > "$TMP/body.txt"
printf 'png' > "$TMP/shot.png"
run_post CHASSIS_ALERT_TRANSPORT=matrix "$LIB/alert-post.sh" ops "@$TMP/body.txt" "$TMP/shot.png" >/dev/null
if [[ "$(arg matrix 1)" == "ops" && "$(arg matrix 2)" == "@$TMP/body.txt" && "$(arg matrix 3)" == "$TMP/shot.png" ]] && ! called discord; then
    ok "matrix: key, @file body and attachments pass through"
else
    bad "matrix passthrough" "$(cat "$REC/matrix.args" 2>/dev/null)"
fi

reset
echo "CHASSIS_ALERT_TRANSPORT=discord" >> "$HOMEDIR/.env"
run_post CHASSIS_ALERT_TRANSPORT=matrix "$LIB/alert-post.sh" alerts "x" >/dev/null
if called matrix && ! called discord; then
    ok "caller's CHASSIS_ALERT_TRANSPORT beats .env"
else
    bad "caller precedence" "matrix=$(called matrix && echo y) discord=$(called discord && echo y)"
fi

reset
echo 3 > "$REC/matrix.rc"
out="$(run_post CHASSIS_ALERT_TRANSPORT=matrix "$LIB/alert-post.sh" alerts "x" 2>/dev/null)"
if [[ $? -eq 0 && "$out" == *"matrix=fail discord=ok"* ]]; then
    ok "matrix: failure falls back to Discord"
else
    bad "matrix fallback" "$out"
fi

reset
echo 3 > "$REC/discord.rc"
out="$(run_post CHASSIS_ALERT_TRANSPORT=both "$LIB/alert-post.sh" alerts "x" 2>/dev/null)"
if [[ $? -eq 0 && "$out" == *"matrix=ok discord=fail"* ]]; then
    ok "both: one leg delivered is success, and the status line names the failed leg"
else
    bad "both partial" "$out"
fi

reset
echo 3 > "$REC/discord.rc"
echo 3 > "$REC/matrix.rc"
run_post CHASSIS_ALERT_TRANSPORT=both "$LIB/alert-post.sh" alerts "x" >/dev/null 2>&1
rc=$?
if [[ $rc -eq 3 ]]; then ok "nothing delivered exits 3"; else bad "nothing delivered" "rc=$rc"; fi

reset
run_post "$LIB/alert-post.sh" alerts >/dev/null 2>&1
rc=$?
if [[ $rc -eq 1 ]]; then ok "missing message exits 1"; else bad "usage" "rc=$rc"; fi

reset
out="$(run_post "$LIB/alert-post.sh" alerts "x" 2>&1)"
if [[ "$out" != *"fake-bot-token"* ]]; then
    ok "bot token never printed"
else
    bad "token leak" "token in output"
fi

echo
echo "test-alert-transport: ${pass} passed, ${fail} failed"
[[ $fail -eq 0 ]]
