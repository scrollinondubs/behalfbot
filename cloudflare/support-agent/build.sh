#!/usr/bin/env bash
# Stage build-context/knowledge/ for the support-agent image (issue #66).
#
# Deny-by-default packaging: ONLY paths listed in knowledge-manifest.txt
# (relative to the behalfbot repo root) are copied. After staging, a
# verifier fails the build if any forbidden path pattern is present, so a
# careless manifest edit cannot silently widen the boundary.
#
# Unlike the executor's build.sh, this needs NO GITHUB_PAT and no clone:
# the corpus is the public behalfbot repo this script lives in.
#
# Usage:
#   ./build.sh
# Then, deploy-gated on Sean's approval:
#   CLOUDFLARE_API_TOKEN=... npx wrangler deploy   # DO NOT run without approval

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CONTEXT_DIR="${SCRIPT_DIR}/build-context"
KNOWLEDGE_DIR="${CONTEXT_DIR}/knowledge"
MANIFEST="${SCRIPT_DIR}/knowledge-manifest.txt"

rm -rf "${CONTEXT_DIR}"
mkdir -p "${KNOWLEDGE_DIR}"

echo "[build] staging knowledge corpus from manifest..."
while IFS= read -r line; do
  # Skip comments and blanks.
  [[ -z "${line}" || "${line}" == \#* ]] && continue
  src="${REPO_ROOT}/${line}"
  if [[ ! -e "${src}" ]]; then
    echo "[build] FATAL: manifest path does not exist: ${line}" >&2
    exit 1
  fi
  dest="${KNOWLEDGE_DIR}/${line}"
  mkdir -p "$(dirname "${dest}")"
  if [[ -d "${src}" ]]; then
    cp -R "${src}/" "${dest%/}"
  else
    cp "${src}" "${dest}"
  fi
done < "${MANIFEST}"

echo "[build] scrubbing anything that must never ship..."
find "${KNOWLEDGE_DIR}" \( -name ".env*" -o -name "*.jsonl" -o -name ".git" \) \
  -exec rm -rf {} + 2>/dev/null || true

echo "[build] verifying the #66 boundary (path-based, fail-closed)..."
FORBIDDEN='dating|whatsapp|/bfl|jax-private|welfare-check|vaultwarden|\.env'
violations="$(cd "${KNOWLEDGE_DIR}" && find . -type f | grep -Ei "${FORBIDDEN}" || true)"
if [[ -n "${violations}" ]]; then
  echo "[build] FATAL: forbidden paths staged into the image:" >&2
  echo "${violations}" >&2
  exit 1
fi

count="$(find "${KNOWLEDGE_DIR}" -type f | wc -l | tr -d ' ')"
echo "[build] knowledge corpus staged: ${count} files in ${KNOWLEDGE_DIR}"
echo "[build] next (GATED ON SEAN'S APPROVAL): export the sean@grid7.com CLOUDFLARE_API_TOKEN per README.md, then 'npx wrangler deploy' from ${SCRIPT_DIR}"
