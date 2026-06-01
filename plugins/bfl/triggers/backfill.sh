#!/bin/bash
# backfill.sh — chassis trigger handler for the BFL Backfill: keyword.
#
# This script is invoked by the chassis trigger dispatcher (declared in
# openclaw.plugin.json contracts.triggers) when a message matching
# `^Backfill[\s:]+` lands in the configured health channel.
#
# Contract (per the chassis trigger dispatch framework — finalized in
# <v1-reference-install>#506; this plugin is shipping ahead of #506 with a
# stub that echoes its inputs):
#
#   ENV:
#     CHASSIS_HOME              chassis root
#     TRIGGER_NAME              "backfill"
#     TRIGGER_MESSAGE_RAW       full Discord message body
#     TRIGGER_MESSAGE_AFTER_KW  message body with "^Backfill[\s:]+" stripped
#     TRIGGER_CHANNEL_ID        Discord channel ID
#     TRIGGER_USER_ID           Discord user ID
#     TRIGGER_PARSED_ARGS_JSON  JSON object emitted by the trigger's `parser`
#                               (here: "bfl-natural-language-meal") with keys
#                               { description, time_actual, protein_portions,
#                                 carb_portions } populated via the parser
#                               library #506 ships.
#
# OUTPUT:
#   stdout — JSON line { ok, meal_num, message } the dispatcher posts back to
#   the channel as the trigger acknowledgement (the react_emoji is set
#   separately via the dispatcher's react step).
#
# V1-pre-#506 BEHAVIOUR:
#   When the dispatcher trigger framework (<v1-reference-install>#506) is not yet
#   present, this handler runs in pure echo mode — it dumps the env vars to
#   stderr and emits a "{\"ok\": false, \"reason\": \"trigger_framework_pending\"}"
#   line on stdout. That lets the plugin land in the chassis tree without
#   blocking on #506 + lets installers verify the wiring once #506 lands.
#   Replace this stub with the real handler body once the parser library
#   lands.

set -euo pipefail

: "${CHASSIS_HOME:?CHASSIS_HOME must be set (chassis trigger dispatcher contract)}"

PLUGIN_DIR="${CHASSIS_HOME}/plugins/bfl"
PYTHON="${PYTHON:-python3}"

# Echo received env to stderr so the dispatcher log captures the wiring.
{
  echo "[bfl backfill trigger] received message"
  echo "  TRIGGER_NAME=${TRIGGER_NAME:-<unset>}"
  echo "  TRIGGER_CHANNEL_ID=${TRIGGER_CHANNEL_ID:-<unset>}"
  echo "  TRIGGER_USER_ID=${TRIGGER_USER_ID:-<unset>}"
  echo "  TRIGGER_MESSAGE_RAW=${TRIGGER_MESSAGE_RAW:-<unset>}"
  echo "  TRIGGER_MESSAGE_AFTER_KW=${TRIGGER_MESSAGE_AFTER_KW:-<unset>}"
  echo "  TRIGGER_PARSED_ARGS_JSON=${TRIGGER_PARSED_ARGS_JSON:-<unset>}"
} >&2

# When the dispatcher hasn't populated TRIGGER_PARSED_ARGS_JSON yet (#506
# pending), emit the stub status and exit cleanly.
if [[ -z "${TRIGGER_PARSED_ARGS_JSON:-}" ]]; then
  jq -nc '{ok: false, reason: "trigger_framework_pending", note: "parser stub: TRIGGER_PARSED_ARGS_JSON not provided. Once <v1-reference-install>#506 lands the parser library, this handler will invoke bfl-backfill-meal.py with the parsed --description / --time-actual / --protein-portions / --carb-portions fields."}'
  exit 0
fi

# Real-handler path: parse the JSON and call bfl-backfill-meal.py.
desc=$(jq -r '.description // ""' <<<"$TRIGGER_PARSED_ARGS_JSON")
time_actual=$(jq -r '.time_actual // empty' <<<"$TRIGGER_PARSED_ARGS_JSON")
protein=$(jq -r '.protein_portions // empty' <<<"$TRIGGER_PARSED_ARGS_JSON")
carbs=$(jq -r '.carb_portions // empty' <<<"$TRIGGER_PARSED_ARGS_JSON")

if [[ -z "$desc" ]]; then
  jq -nc '{ok: false, reason: "missing_description", note: "Backfill trigger requires a non-empty description. Reply asking the user to clarify."}'
  exit 0
fi

ARGS=("--description" "$desc")
[[ -n "$time_actual" ]] && ARGS+=("--time-actual" "$time_actual")
[[ -n "$protein" ]] && ARGS+=("--protein-portions" "$protein")
[[ -n "$carbs" ]] && ARGS+=("--carb-portions" "$carbs")

# bfl-backfill-meal.py prints one JSON line on stdout with the inserted row;
# pass it through verbatim so the dispatcher can read meal_num + date and
# render the channel acknowledgement.
result_json=$("$PYTHON" "$PLUGIN_DIR/scripts/bfl-backfill-meal.py" "${ARGS[@]}" 2>/dev/null) || {
  rc=$?
  jq -nc --argjson rc "$rc" '{ok: false, reason: "backfill_script_failed", exit_code: $rc}'
  exit 0
}

# Wrap the script output with ok=true so the dispatcher knows it succeeded.
jq -nc --argjson row "$result_json" '{ok: true} + $row'
