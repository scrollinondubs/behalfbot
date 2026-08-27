#!/usr/bin/env bash
# Deploy the executor Worker + container with build identity attached
# (issues #173 / #185).
#
# Why this exists rather than a bare `npx wrangler deploy`:
#
# build.sh bakes appCommit/builtAt into the image, and the container
# reports them on its own /healthz. But the Worker answers /healthz itself
# and never forwards that path, on purpose, so a liveness probe cannot wake
# a standard-1 instance. That left the fields unreachable: the
# executor-drift monitor polls the Worker and always saw {"ok":true}, so it
# reported "deploy owed" even immediately after a deploy.
#
# The fix is to hand the same two values to the Worker as plain vars, which
# it returns from /healthz for free. This script is the thing that keeps
# the image and the Worker reporting the same commit, so use it instead of
# calling wrangler directly.
#
# Usage:
#   GITHUB_PAT=... ./build.sh          # stages build-context/, writes build-info.json
#   CLOUDFLARE_API_TOKEN=... ./deploy.sh
#
# The API token is the account-owned "behalfbot-fable-cf-containers" item in
# Vaultwarden. The ambient CLOUDFLARE_API_TOKEN in the Jax install env
# belongs to the AllBets account and targets the WRONG account; the
# account_id pin in wrangler.jsonc forces the right one regardless, but
# check `npx wrangler whoami` if a deploy looks odd.
#
# DO NOT run without Sean's explicit approval.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_INFO="${SCRIPT_DIR}/build-context/build-info.json"

if [[ ! -f "$BUILD_INFO" ]]; then
  echo "deploy: ${BUILD_INFO} is missing. Run build.sh first." >&2
  echo "deploy: refusing to ship a build with no provenance - that is the" >&2
  echo "        exact failure #173 exists to prevent." >&2
  exit 1
fi

APP_COMMIT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["appCommit"])' "$BUILD_INFO")"
BUILT_AT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["builtAt"])' "$BUILD_INFO")"

if [[ -z "$APP_COMMIT" || -z "$BUILT_AT" ]]; then
  echo "deploy: build-info.json has no appCommit/builtAt. Re-run build.sh." >&2
  exit 1
fi

echo "[deploy] app commit ${APP_COMMIT}, built ${BUILT_AT}"

cd "$SCRIPT_DIR"
exec npx wrangler deploy \
  --var "APP_COMMIT:${APP_COMMIT}" \
  --var "BUILT_AT:${BUILT_AT}" \
  "$@"
