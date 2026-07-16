#!/usr/bin/env bash
# Prepare build-context/ for the executor image (issues #41 / #66).
#
# The executor code lives in the private scrollinondubs/vibecodelisboa
# repo. We stage a stripped copy here BEFORE any docker/wrangler build so
# that:
#   - the GITHUB_PAT never enters the docker build (clone happens on the
#     host, not in a Dockerfile RUN)
#   - .git is removed entirely - vibecodelisboa's git history contains
#     live prod secrets (new-jaxity#454, rotation pending), so shipping
#     history into an image is a hard no
#   - .env* files can never ride along (a fresh clone has none; we scrub
#     anyway, belt and braces)
#
# Usage:
#   GITHUB_PAT=... ./build.sh
# Then, deploy-gated on Sean's approval:
#   CLOUDFLARE_API_TOKEN=... npx wrangler deploy   # DO NOT run without approval

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTEXT_DIR="${SCRIPT_DIR}/build-context"
REPO_DIR="${CONTEXT_DIR}/vibecodelisboa"

: "${GITHUB_PAT:?GITHUB_PAT must be set (jacketyjax PAT, read scope on scrollinondubs/vibecodelisboa)}"

rm -rf "${CONTEXT_DIR}"
mkdir -p "${CONTEXT_DIR}"

echo "[build] shallow-cloning vibecodelisboa (history stripped)..."
git clone --depth 1 \
  "https://x-access-token:${GITHUB_PAT}@github.com/scrollinondubs/vibecodelisboa.git" \
  "${REPO_DIR}"

echo "[build] scrubbing git metadata + any env files..."
rm -rf "${REPO_DIR}/.git"
find "${REPO_DIR}" -maxdepth 2 -name ".env*" -type f -delete

# node_modules is deliberately NOT installed here. The image targets
# linux/amd64; a host-side install on the macOS arm64 Mac mini would bake
# darwin binaries (esbuild, @libsql, sqlite-vec) into the image and tsx
# would fail at runtime. The Dockerfile runs npm install inside the image
# instead, so platform-specific optional deps resolve for the container.

echo "[build] build-context ready: ${REPO_DIR}"
echo "[build] next (GATED ON SEAN'S APPROVAL): CLOUDFLARE_API_TOKEN in env, then 'npx wrangler deploy' from ${SCRIPT_DIR}"
