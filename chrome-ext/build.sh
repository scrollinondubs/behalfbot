#!/usr/bin/env bash
# Zips the extension for "Load unpacked" (unzip, then chrome://extensions,
# Developer mode, Load unpacked, pick the folder).
#
#   ./build.sh                                   relay = the deployed installer
#   RELAY_ORIGIN=http://localhost:8787 ./build.sh   local wrangler dev
#
# Output: dist/behalfbot-autopilot/ (the folder to load) and
# dist/behalfbot-autopilot.zip.
set -euo pipefail
cd "$(dirname "$0")"
RELAY="${RELAY_ORIGIN:-https://behalfbot-installer.sean-8a1.workers.dev}"
if [[ ! "$RELAY" =~ ^https?://[A-Za-z0-9.-]+(:[0-9]+)?$ ]]; then
  echo "RELAY_ORIGIN must be an origin like https://host or http://localhost:8787" >&2
  exit 1
fi
OUT=dist/behalfbot-autopilot
rm -rf dist
mkdir -p "$OUT"
cp manifest.json background.js policy.js config.js popup.html popup.js sidepanel.html sidepanel.js style.css "$OUT/"
sed -i.bak "s#^export const RELAY_ORIGIN = .*#export const RELAY_ORIGIN = '$RELAY'#" "$OUT/config.js"
node -e '
  const fs = require("fs"); const p = process.argv[1]; const m = JSON.parse(fs.readFileSync(p, "utf8"))
  m.host_permissions = [process.argv[2] + "/*"]; fs.writeFileSync(p, JSON.stringify(m, null, 2) + "\n")
' "$OUT/manifest.json" "$RELAY"
rm -f "$OUT"/*.bak
(cd dist && zip -qr behalfbot-autopilot.zip behalfbot-autopilot)
echo "built dist/behalfbot-autopilot.zip for relay $RELAY"
