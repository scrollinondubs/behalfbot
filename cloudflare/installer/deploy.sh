#!/usr/bin/env bash
# Deploy behalfbot-installer. Needs Sean's explicit approval each time until
# this Worker has an approved auto-deploy path like the executor's.
#
# Usage:
#   CLOUDFLARE_API_TOKEN=... ./deploy.sh
#
# The API token is the account-owned "behalfbot-fable-cf-containers" item in
# Vaultwarden. Typecheck and tests run first; a red build does not ship.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

npm run --silent typecheck
npm test --silent

exec npx wrangler deploy "$@"
