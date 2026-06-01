#!/bin/bash
# chassis/scripts/hydrate-env-from-vw.sh
# =====================================
# Pull customer secrets from their self-hosted Vaultwarden via rbw, write to
# $CHASSIS_HOME/.env. Called by docker/entrypoint.sh hydrate-env mode at
# install kickoff (and on any subsequent secret rotation).
#
# Bootstrap-time only - the dispatcher loop NEVER reaches Vaultwarden at
# runtime. Re-hydrate on demand by running this again. Idempotent: doesn't
# clobber lines already in .env that aren't represented in the manifest.
#
# Item-name -> .env-var mapping lives in the per-customer install repo at
# install-<customer>-vw-items.md (or vw-items.json if you want
# machine-parseable). The chassis loads the manifest from CHASSIS_HOME or
# falls back to a built-in default.
#
# Required env (caller responsibility - passed via `docker compose run -e`):
#   RBW_EMAIL          Customer's VW master account email
#   RBW_URL            VW URL reachable from the chassis container
#                      (typically http://vaultwarden:80 inside compose stack)
#   RBW_MASTER_PASS    Customer's VW master password (one-time at install)
#
# Optional env:
#   VW_MANIFEST        Path to a JSON manifest of {item_name: env_var_name}.
#                      Default: $CHASSIS_HOME/vw-items.json or the built-in.
#   DRY_RUN=true       Print the manifest + intended writes without modifying .env
#
# Exit codes:
#   0 - .env written / unchanged (idempotent re-run)
#   2 - bad env / missing rbw / VW unreachable / manifest missing
#   3 - one or more items missing from VW (warning, not fatal; partial .env still written)

set -euo pipefail

: "${CHASSIS_HOME:?CHASSIS_HOME must be set}"
: "${RBW_EMAIL:?RBW_EMAIL must be set - the VW master account email for the customer}"
: "${RBW_URL:?RBW_URL must be set - VW URL reachable from this container}"
: "${RBW_MASTER_PASS:?RBW_MASTER_PASS must be set - VW master password}"

DRY_RUN="${DRY_RUN:-false}"
ENV_FILE="$CHASSIS_HOME/.env"
ENV_TMP="$CHASSIS_HOME/.env.tmp.$$"
PINENTRY_STUB="/tmp/rbw-pinentry-stub-$$"

log() {
    printf '[hydrate-env %(%H:%M:%S)T] %s\n' -1 "$*" >&2
}

cleanup() {
    rm -f "$PINENTRY_STUB" "$ENV_TMP" 2>/dev/null || true
}
trap cleanup EXIT

if ! command -v rbw >/dev/null 2>&1; then
    log "FATAL: rbw not installed (should be baked into chassis image)"
    exit 2
fi

# ---------- Manifest resolution ---------------------------------------------
# The manifest maps Vaultwarden item names to the destination .env variable.
# Custom-field items map to multiple env vars; the manifest supports both:
#
# {
#   "Behalf.bot - Telegram bot token":    "TELEGRAM_BOT_TOKEN",
#   "Behalf.bot - Google Workspace agent": {
#       "username": "GOOGLE_AGENT_EMAIL",
#       "password": "GOOGLE_AGENT_APP_PASSWORD"
#   },
#   "Behalf.bot - Notion DB IDs": {"notes": "@dotenv"}
# }
#
# "@dotenv" means parse the notes field as a KEY=value dotenv block.

DEFAULT_MANIFEST=$(cat <<'JSON'
{
  "Behalf.bot - Google Workspace agent": {
    "username": "GOOGLE_AGENT_EMAIL",
    "password": "GOOGLE_AGENT_APP_PASSWORD"
  },
  "Behalf.bot - Google OAuth client": {
    "username": "GOOGLE_CLIENT_ID",
    "password": "GOOGLE_CLIENT_SECRET"
  },
  "Behalf.bot - GitHub PAT": "GITHUB_PAT",
  "Behalf.bot - Telegram bot token": "TELEGRAM_BOT_TOKEN",
  "Behalf.bot - Telegram Marc user_id": "INSTALLER_TELEGRAM_USER_ID",
  "Behalf.bot - Slack bot token": "SLACK_BOT_TOKEN",
  "Behalf.bot - Slack workspace_id": "SLACK_WORKSPACE_ID",
  "Behalf.bot - Slack Marc user_id": "INSTALLER_SLACK_USER_ID",
  "Behalf.bot - Notion integration token": "NOTION_INTEGRATION_TOKEN",
  "Behalf.bot - Notion DB IDs": {"notes": "@dotenv"},
  "Behalf.bot - Vaultwarden API token": "RBW_VW_API_TOKEN",
  "Behalf.bot - Postgres password": "POSTGRES_PASSWORD",
  "Behalf.bot - chassis OpenAI key": "OPENAI_API_KEY",
  "Behalf.bot - Tailscale auth key": "TAILSCALE_AUTHKEY",
  "Behalf.bot - digital debit card": {"notes": "@dotenv"},
  "Behalf.bot - card API credentials": "BANKING_API_TOKEN"
}
JSON
)

MANIFEST_JSON="${VW_MANIFEST:-}"
if [[ -n "$MANIFEST_JSON" && -f "$MANIFEST_JSON" ]]; then
    log "loading manifest from $MANIFEST_JSON"
    MANIFEST_DATA=$(cat "$MANIFEST_JSON")
elif [[ -f "$CHASSIS_HOME/vw-items.json" ]]; then
    log "loading manifest from $CHASSIS_HOME/vw-items.json"
    MANIFEST_DATA=$(cat "$CHASSIS_HOME/vw-items.json")
else
    log "using built-in default manifest (no $CHASSIS_HOME/vw-items.json found)"
    MANIFEST_DATA="$DEFAULT_MANIFEST"
fi

if ! echo "$MANIFEST_DATA" | jq empty 2>/dev/null; then
    log "FATAL: manifest is not valid JSON"
    exit 2
fi

# ---------- rbw config + unlock ---------------------------------------------
# rbw stores config in ~/.config/rbw/config.json. Set fresh on every run since
# the customer's VW URL + email might have changed (e.g. host migration).

mkdir -p "$HOME/.config/rbw"
rbw config set email "$RBW_EMAIL" >/dev/null
rbw config set base_url "$RBW_URL" >/dev/null
rbw config set pinentry "$PINENTRY_STUB" >/dev/null

cat > "$PINENTRY_STUB" <<'PINENTRY_EOF'
#!/bin/sh
# Pinentry stub - emits the master password from env to the rbw prompt protocol.
while IFS= read -r line; do
  case "$line" in
    GETPIN) printf 'D %s\nOK\n' "$RBW_MASTER_PASS" ;;
    BYE)    printf 'OK\n'; exit 0 ;;
    *)      printf 'OK\n' ;;
  esac
done
PINENTRY_EOF
chmod +x "$PINENTRY_STUB"

log "logging in to VW at $RBW_URL as $RBW_EMAIL"
if ! RBW_MASTER_PASS="$RBW_MASTER_PASS" rbw login >/dev/null 2>&1; then
    log "FATAL: rbw login failed (check RBW_URL reachable from container, RBW_EMAIL correct, RBW_MASTER_PASS correct)"
    exit 2
fi

log "syncing vault"
rbw sync >/dev/null 2>&1 || {
    log "FATAL: rbw sync failed"
    exit 2
}

# ---------- Walk manifest, pull each item, build .env -----------------------

declare -a PULLED=()
declare -a MISSING=()
declare -A WRITES=()

extract_dotenv() {
    # Filter a notes blob to lines matching KEY=value. Drop comments + blanks.
    awk '/^[A-Za-z_][A-Za-z0-9_]*=/ {print}'
}

# Read existing .env to preserve any lines that aren't manifest-managed
# (e.g. operator-added entries between hydrations).
declare -A EXISTING=()
if [[ -f "$ENV_FILE" ]]; then
    while IFS='=' read -r key value; do
        [[ -z "$key" ]] && continue
        [[ "$key" =~ ^# ]] && continue
        EXISTING["$key"]="$value"
    done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE" || true)
fi

# Iterate the manifest items.
ITEM_COUNT=$(echo "$MANIFEST_DATA" | jq 'length')
log "manifest has $ITEM_COUNT items"

while IFS= read -r line; do
    item_name=$(echo "$line" | jq -r '.key')
    spec=$(echo "$line" | jq -c '.value')

    if [[ "$spec" =~ ^\".*\"$ ]]; then
        # Simple string spec: pull password field, write to single env var
        env_var=$(echo "$spec" | jq -r '.')
        if value=$(rbw get "$item_name" 2>/dev/null); then
            WRITES["$env_var"]="$value"
            PULLED+=("$item_name -> $env_var")
        else
            MISSING+=("$item_name -> $env_var")
        fi
    else
        # Object spec: per-field mapping
        while IFS= read -r field_line; do
            field=$(echo "$field_line" | jq -r '.key')
            env_var=$(echo "$field_line" | jq -r '.value')
            case "$field" in
                username|password|notes|uri|totp)
                    value=$(rbw get --field "$field" "$item_name" 2>/dev/null || echo "")
                    ;;
                *)
                    # Custom-field name lookup via raw item JSON
                    value=$(rbw get --raw "$item_name" 2>/dev/null \
                        | jq -r --arg f "$field" '.fields[]? | select(.name == $f) | .value' \
                        || echo "")
                    ;;
            esac

            if [[ -z "$value" ]]; then
                MISSING+=("$item_name field $field -> $env_var")
                continue
            fi

            if [[ "$env_var" == "@dotenv" ]]; then
                # Parse value as KEY=value lines; write each
                while IFS='=' read -r k v; do
                    [[ -z "$k" ]] && continue
                    WRITES["$k"]="$v"
                    PULLED+=("$item_name field $field -> dotenv $k")
                done < <(echo "$value" | extract_dotenv)
            else
                WRITES["$env_var"]="$value"
                PULLED+=("$item_name field $field -> $env_var")
            fi
        done < <(echo "$spec" | jq -c 'to_entries[]')
    fi
done < <(echo "$MANIFEST_DATA" | jq -c 'to_entries[]')

# ---------- Compose new .env ------------------------------------------------

{
    printf '# Hydrated by chassis/scripts/hydrate-env-from-vw.sh on %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '# Source: Vaultwarden at %s (account: %s)\n' "$RBW_URL" "$RBW_EMAIL"
    printf '# Re-run to refresh after secret rotation. Idempotent.\n#\n'

    # Manifest-pulled values
    for key in "${!WRITES[@]}"; do
        printf '%s=%s\n' "$key" "${WRITES[$key]}"
    done | sort

    # Preserve existing operator-added entries not in manifest
    printf '\n# === Operator-added entries preserved from prior .env ===\n'
    for key in "${!EXISTING[@]}"; do
        if [[ -z "${WRITES[$key]:-}" ]]; then
            printf '%s=%s\n' "$key" "${EXISTING[$key]}"
        fi
    done | sort
} > "$ENV_TMP"

if [[ "$DRY_RUN" == "true" ]]; then
    log "DRY_RUN - would write $(wc -l < "$ENV_TMP") lines to $ENV_FILE"
    log "pulled items:"
    for p in "${PULLED[@]}"; do log "  $p"; done
    if [[ ${#MISSING[@]} -gt 0 ]]; then
        log "missing items:"
        for m in "${MISSING[@]}"; do log "  $m"; done
    fi
    exit 0
fi

mv "$ENV_TMP" "$ENV_FILE"
chmod 600 "$ENV_FILE"
log "wrote $(wc -l < "$ENV_FILE") lines to $ENV_FILE"
log "pulled ${#PULLED[@]} item-field mappings from VW"

if [[ ${#MISSING[@]} -gt 0 ]]; then
    log "WARN: ${#MISSING[@]} manifest item(s) missing from VW (partial hydration):"
    for m in "${MISSING[@]}"; do log "  $m"; done
    exit 3
fi

exit 0
