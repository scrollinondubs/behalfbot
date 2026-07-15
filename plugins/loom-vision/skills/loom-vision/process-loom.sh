#!/bin/bash
# process-loom.sh - Download a Loom video, extract key frames + transcript.
#
# Usage:
#   bash plugins/loom-vision/skills/loom-vision/process-loom.sh <loom-share-url>
#
# Output:
#   Creates ${OUTPUT_ROOT}/loom-<video_id>/ containing:
#     - video.mp4         (full source video, kept for re-sampling)
#     - transcript.json   (raw loom-dl transcript, kept verbatim)
#     - transcript.vtt    (WebVTT built from the JSON - what the agent reads)
#     - transcript.txt    (plaintext transcript, no timestamps)
#     - frame_NNN.jpg     (sampled frames at FRAME_INTERVAL_SECONDS cadence)
#   Prints the output directory path to stdout. Progress messages go to stderr.
#
# Configuration (env vars; defaults match plugin configSchema):
#   OUTPUT_ROOT               default ${CHASSIS_HOME}/temp
#   FRAME_INTERVAL_SECONDS    default 5 (try 2-3 for fast bug repros)
#   FRAME_MAX_WIDTH_PX        default 1280
#   FRAME_QUALITY             default 3 (ffmpeg -q:v, 1-31, lower = better)
#
# Dependencies (installed via plugins/loom-vision/setup.sh):
#   - node    (transcript JSON -> VTT conversion; also required by loom-dl)
#   - loom-dl (npm install -g loom-dl)
#   - ffmpeg  (brew install ffmpeg; supplies ffprobe for duration bounding)

set -euo pipefail

URL="${1:-}"
if [[ -z "$URL" ]]; then
  echo "ERROR: Usage: process-loom.sh <loom-share-url>" >&2
  exit 1
fi

OUTPUT_ROOT="${OUTPUT_ROOT:-${CHASSIS_HOME:-$HOME}/temp}"
FRAME_INTERVAL_SECONDS="${FRAME_INTERVAL_SECONDS:-5}"
FRAME_MAX_WIDTH_PX="${FRAME_MAX_WIDTH_PX:-1280}"
FRAME_QUALITY="${FRAME_QUALITY:-3}"

# Sanity-check deps so the script fails loudly with a clear message instead
# of midway through the pipeline.
if ! command -v loom-dl >/dev/null 2>&1; then
  echo "ERROR: loom-dl not found in PATH. Install with: npm install -g loom-dl" >&2
  echo "       (Or run plugins/loom-vision/setup.sh to install all deps.)" >&2
  exit 2
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ERROR: ffmpeg not found in PATH. Install with: brew install ffmpeg" >&2
  exit 2
fi
if ! command -v node >/dev/null 2>&1; then
  echo "ERROR: node not found in PATH. Install with: brew install node" >&2
  exit 2
fi

# Extract video ID from URL - Loom share URLs are .../share/<32-hex>/...
VIDEO_ID=$(echo "$URL" | grep -oE '[a-f0-9]{32}' | head -1)
if [[ -z "$VIDEO_ID" ]]; then
  echo "ERROR: Could not extract a 32-hex video ID from URL: $URL" >&2
  echo "       Expected format: https://www.loom.com/share/<32-hex-id>/..." >&2
  exit 1
fi

OUTPUT_DIR="$OUTPUT_ROOT/loom-$VIDEO_ID"
mkdir -p "$OUTPUT_DIR"

# Download the video + transcript. The .mp4 extension on --out is load-bearing:
# loom-dl treats a path ending in .mp4 as an explicit file. Do NOT change it to
# a bare directory - loom-dl would then misinterpret the target and break.
echo "Downloading Loom video $VIDEO_ID..." >&2
loom-dl --url "$URL" --out "$OUTPUT_DIR/video.mp4" --transcript 2>&2

# --- transcript JSON -> VTT + plaintext ------------------------------------
# transcript JSON->VTT conversion added per beta feedback from Bart Boughton,
# 2026-07-15. loom-dl --transcript writes "<out-basename>.transcript.json"
# (schemaVersion 1.1.x: { "phrases": [ { "ts": <seconds>, "value": <text> } ] }),
# NOT a transcript.vtt. Older docs told the agent to read transcript.vtt, which
# never existed, so the transcript half of the skill silently did nothing. The
# embedded converter is defensive about field names and always emits a valid VTT
# (even timing-less) so the agent's "read the transcript" step never dead-ends.
echo "Converting transcript to VTT + plaintext..." >&2

# loom-dl derives the transcript name from the --out basename: video.mp4 -> video.transcript.json
TRANSCRIPT_JSON="$OUTPUT_DIR/video.transcript.json"
if [[ ! -f "$TRANSCRIPT_JSON" ]]; then
  TRANSCRIPT_JSON=$(ls -1 "$OUTPUT_DIR"/*.transcript.json 2>/dev/null | head -1 || true)
  if [[ -z "${TRANSCRIPT_JSON:-}" ]]; then
    TRANSCRIPT_JSON=$(ls -1 "$OUTPUT_DIR"/*.json 2>/dev/null | grep -v '/transcript\.json$' | head -1 || true)
  fi
fi

VTT="$OUTPUT_DIR/transcript.vtt"
TXT="$OUTPUT_DIR/transcript.txt"

# Best-effort video duration (seconds) to bound the final cue.
DURATION_SECONDS=""
if command -v ffprobe >/dev/null 2>&1; then
  DURATION_SECONDS=$(ffprobe -v error -show_entries format=duration -of default=nk=1:nw=1 "$OUTPUT_DIR/video.mp4" 2>/dev/null || true)
fi

if [[ -n "${TRANSCRIPT_JSON:-}" && -f "$TRANSCRIPT_JSON" ]]; then
  # Keep the raw JSON under a predictable name alongside the converted files.
  if [[ "$TRANSCRIPT_JSON" != "$OUTPUT_DIR/transcript.json" ]]; then
    cp -f "$TRANSCRIPT_JSON" "$OUTPUT_DIR/transcript.json"
  fi
  node - "$TRANSCRIPT_JSON" "$VTT" "$TXT" "${DURATION_SECONDS:-0}" <<'LOOMVTT'
const fs = require("fs");
const args = process.argv.slice(2);
const jsonPath = args[0], vttPath = args[1], txtPath = args[2];
const duration = parseFloat(args[3]) || 0;

function writeStub(msg) {
  fs.writeFileSync(vttPath, "WEBVTT\n\nNOTE " + msg + "\n");
  fs.writeFileSync(txtPath, "");
  console.error("WARN: " + msg);
}

let root;
try { root = JSON.parse(fs.readFileSync(jsonPath, "utf8")); }
catch (e) { writeStub("transcript JSON could not be parsed: " + e.message); process.exit(0); }

function firstArray() {
  for (let i = 0; i < arguments.length; i++) {
    const c = arguments[i];
    if (Array.isArray(c) && c.length) return c;
  }
  return null;
}
let segs = firstArray(
  root && root.phrases,
  Array.isArray(root) ? root : null,
  root && root.transcript,
  root && root.segments,
  root && root.cues,
  root && root.captions,
  root && root.results
);
let wordLevel = false;
if (!segs) {
  const words = firstArray(root && root.words, root && root.tokens);
  if (words) { segs = words; wordLevel = true; }
}
if (!segs) {
  const blob = root && (root.text || root.transcript || root.value);
  segs = (typeof blob === "string" && blob.trim()) ? [{ value: blob }] : [];
}

const START_KEYS = ["start_ts", "startTime", "start", "ts", "tsStart", "begin", "offset", "from", "startTimeMs", "startMs"];
const END_KEYS = ["end_ts", "endTime", "end", "tsEnd", "stop", "to", "endTimeMs", "endMs"];
const TEXT_KEYS = ["value", "text", "content", "phrase", "caption", "transcript", "word", "token"];

function parseClock(s) {
  const parts = String(s).trim().split(":").map(Number);
  if (!parts.length || parts.some(n => !isFinite(n))) return null;
  let sec = 0;
  for (const p of parts) sec = sec * 60 + p;
  return sec;
}
function num(v) {
  if (typeof v === "number" && isFinite(v)) return v;
  if (typeof v === "string" && v.trim() !== "") {
    if (isFinite(+v)) return +v;
    return parseClock(v);
  }
  return null;
}
function pickKV(o, keys) {
  for (const k of keys) if (o && o[k] !== undefined && o[k] !== null && o[k] !== "") return [k, o[k]];
  return [null, undefined];
}
function toSec(k, raw) { let v = num(raw); if (v === null) return null; if (/ms$/i.test(k || "")) v /= 1000; return v; }
function pick(o, keys) { return pickKV(o, keys)[1]; }
function startOf(o) { const kv = pickKV(o, START_KEYS); return toSec(kv[0], kv[1]); }
function endOf(o) { const kv = pickKV(o, END_KEYS); return toSec(kv[0], kv[1]); }
function textOf(o) { if (typeof o === "string") return o; const t = pick(o, TEXT_KEYS); return t == null ? "" : String(t); }

let cues;
if (wordLevel) {
  cues = [];
  const CHUNK = 12;
  for (let i = 0; i < segs.length; i += CHUNK) {
    const grp = segs.slice(i, i + CHUNK);
    const text = grp.map(textOf).join(" ").replace(/\s+/g, " ").trim();
    const starts = grp.map(startOf).filter(v => v != null);
    const ends = grp.map(endOf).filter(v => v != null);
    cues.push({ start: starts.length ? starts[0] : null, end: ends.length ? ends[ends.length - 1] : null, text });
  }
} else {
  cues = segs.map(s => ({ start: startOf(s), end: endOf(s), text: textOf(s).replace(/\s+/g, " ").trim() }))
             .filter(c => c.text.length || c.start != null);
}

const anyStart = cues.some(c => c.start != null);

for (let i = 0; i < cues.length; i++) {
  if (cues[i].start == null) continue;
  if (cues[i].end == null) {
    let ns = null;
    for (let j = i + 1; j < cues.length; j++) if (cues[j].start != null) { ns = cues[j].start; break; }
    cues[i].end = ns != null ? ns : (duration > cues[i].start ? duration : cues[i].start + 4);
  }
  if (cues[i].end <= cues[i].start) cues[i].end = cues[i].start + 0.5;
}

function fmt(t) {
  if (!(t > 0)) t = 0;
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = Math.floor(t % 60);
  const ms = Math.round((t - Math.floor(t)) * 1000);
  const p = (n, w) => String(n).padStart(w, "0");
  return p(h, 2) + ":" + p(m, 2) + ":" + p(s, 2) + "." + p(ms, 3);
}

let out = "WEBVTT\n\n";
if (anyStart) {
  let n = 0;
  for (const c of cues) {
    if (!c.text || c.start == null) continue;
    n++;
    out += n + "\n" + fmt(c.start) + " --> " + fmt(c.end) + "\n" + c.text + "\n\n";
  }
} else {
  const whole = cues.map(c => c.text).filter(Boolean).join(" ").replace(/\s+/g, " ").trim();
  const end = duration > 0 ? duration : Math.max(1, whole.split(/\s+/).length * 0.4);
  out += "NOTE No per-cue timings were present in the source transcript JSON; emitting an untimed transcript.\n\n";
  out += "1\n" + fmt(0) + " --> " + fmt(end) + "\n" + (whole || "(empty transcript)") + "\n\n";
}
fs.writeFileSync(vttPath, out);

let txt = cues.map(c => c.text).filter(Boolean).join("\n").trim();
if (!txt && root && typeof root.text === "string") txt = root.text.trim();
fs.writeFileSync(txtPath, txt + (txt ? "\n" : ""));

console.error("Transcript converted: " + cues.filter(c => c.text).length + " segments -> " + vttPath + (anyStart ? " (timed)" : " (untimed fallback)"));
LOOMVTT
else
  echo "WARN: no transcript JSON found - loom-dl may not have a transcript for this video." >&2
  printf 'WEBVTT\n\nNOTE No transcript was produced by loom-dl for this video.\n' > "$VTT"
  : > "$TXT"
fi

echo "Sampling frames (1 per ${FRAME_INTERVAL_SECONDS}s, max width ${FRAME_MAX_WIDTH_PX}px)..." >&2
ffmpeg -y -i "$OUTPUT_DIR/video.mp4" \
  -vf "fps=1/${FRAME_INTERVAL_SECONDS},scale=${FRAME_MAX_WIDTH_PX}:-1" \
  -q:v "$FRAME_QUALITY" \
  "$OUTPUT_DIR/frame_%03d.jpg" 2>/dev/null

FRAME_COUNT=$(ls "$OUTPUT_DIR"/frame_*.jpg 2>/dev/null | wc -l | tr -d ' ')
# `|| true` keeps ffmpeg's nonzero exit (it has no output file) from tripping
# pipefail; the empty-check supplies the fallback without a stray extra line.
DURATION=$(ffmpeg -i "$OUTPUT_DIR/video.mp4" 2>&1 | grep -m1 Duration | awk '{print $2}' | tr -d ',' || true)
[[ -z "$DURATION" ]] && DURATION="unknown"

echo "Processed: $FRAME_COUNT frames, duration $DURATION" >&2
echo "$OUTPUT_DIR"
