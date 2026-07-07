# Loom Vision

**See the frames, not just the transcript.** Drop a Loom share URL into your agent session and get full visual + audio context for code reviews, bug repros, walkthroughs, and demos.

## What it does

Loom's auto-transcript captures only what was said aloud. For most technical Loom videos - code walkthroughs, design reviews, bug repros - the screen contents matter more than the audio. Loom Vision closes that gap:

1. Downloads the source video and Loom's auto-transcript.
2. Samples frames at 1 frame per 5 seconds (configurable).
3. Hands both to the agent as multimodal context.

The agent then reads the transcript to anchor timing and browses the frames that fall in the relevant window - IDE state, error popups, on-screen code, design mockups, and UI transitions that the audio narration skipped.

## Example uses

- "Here's a 7-minute Loom of the bug - what did I click on at minute 4 that triggered the 500?"
- "Walk me through this design review and pull out every comment about contrast ratios."
- "Loom of my PR walkthrough - summarize the diff strategy I described."
- "I recorded my onboarding flow - where does the UX get confusing?"

## Install

```bash
clawhub install @<owner>/loom-vision
```

Two CLI dependencies (declared in the skill's install specs, so OpenClaw can install them for you):

- `loom-dl` - `npm install -g loom-dl` (Node-based; not on Homebrew)
- `ffmpeg` - `brew install ffmpeg` (macOS) or your distro's package manager (Linux)

Both are idempotent.

## Usage

The skill triggers automatically when you paste a Loom share URL or ask the agent to process Loom video content. You can also invoke the underlying script directly:

```bash
bash process-loom.sh "https://www.loom.com/share/<32-hex-id>/..."
```

The script prints the output directory path to stdout. The directory contains:

- `video.mp4` - original video (kept for re-sampling at different intervals)
- `transcript.vtt` - Loom's auto-transcript with timestamps
- `frame_NNN.jpg` - sampled frames

Frame N corresponds to timestamp `(N - 1) * FRAME_INTERVAL_SECONDS` from the start.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `FRAME_INTERVAL_SECONDS` | `5` | Lower for fast-changing UI, higher for slow walkthroughs. |
| `FRAME_MAX_WIDTH_PX` | `1280` | Bump up if code fonts get too small to read. |
| `FRAME_QUALITY` | `3` | ffmpeg `-q:v` scale (1-31, lower = better quality). |
| `OUTPUT_ROOT` | `${TMPDIR:-/tmp}/loom-vision` | Where per-video output folders are written. |

## Built by

Loom Vision ships standard with [Behalf.bot](https://behalf.bot) - battery-included AI assistant installs for solo operators. If Loom Vision is useful to you on its own, the rest of the Behalf.bot plugin catalog is probably interesting too.
