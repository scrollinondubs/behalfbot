# Loom Vision

**See the frames, not just the transcript.** Drop a Loom share URL into Claude Code and get full visual + audio context for code reviews, bug repros, walkthroughs, and demos.

## What it does

Loom's auto-transcript captures only what was said aloud. For most technical Loom videos — code walkthroughs, design reviews, bug repros — the screen contents matter more than the audio. Loom Vision closes that gap:

1. Downloads the source video and Loom's auto-transcript.
2. Samples frames at 1 frame per 5 seconds (configurable).
3. Hands both to Claude as multimodal context.

The agent then reads the transcript to anchor timing and browses the frames that fall in the relevant window — IDE state, error popups, on-screen code, design mockups, and UI transitions that the audio narration skipped.

## Example uses

- "Here's a 7-minute Loom of the bug — what did I click on at minute 4 that triggered the 500?"
- "Walk me through this design review and pull out every comment about contrast ratios."
- "Loom of my PR walkthrough — summarize the diff strategy I described."
- "I recorded my onboarding flow — where does the UX get confusing?"

## Install

```bash
# As a chassis plugin (Behalf.bot installs)
# Enable in chassis.config.yaml:
#   modules:
#     loom-vision:
#       enabled: true
# Then re-run chassis/bootstrap.sh — setup.sh runs automatically.

# Standalone (no chassis)
bash plugins/loom-vision/setup.sh
```

Setup installs two CLI dependencies:

- `loom-dl` — `npm install -g loom-dl` (Node-based; not on Homebrew)
- `ffmpeg` — `brew install ffmpeg`

Both are idempotent. Re-running setup.sh on an already-installed system is a no-op.

## Usage

The skill triggers automatically when the user pastes a Loom share URL or asks the agent to process Loom video content. The skill definition is in `skills/loom-vision.md`.

You can also invoke the underlying script directly:

```bash
bash plugins/loom-vision/scripts/process-loom.sh "https://www.loom.com/share/<32-hex-id>/..."
```

The script prints the output directory path to stdout. The directory contains:

- `video.mp4` — original video (kept for re-sampling at different intervals)
- `transcript.vtt` — Loom's auto-transcript with timestamps
- `frame_NNN.jpg` — sampled frames

Frame N corresponds to timestamp `(N - 1) * FRAME_INTERVAL_SECONDS` from the start.

## Configuration

| Knob | Default | Purpose |
|---|---|---|
| `enabled` | `true` | Master toggle for the skill. |
| `frame_interval_seconds` | `5` | Lower for fast-changing UI, higher for slow walkthroughs. |
| `frame_max_width_px` | `1280` | Bump up if code fonts get too small to read. |
| `frame_quality` | `3` | ffmpeg `-q:v` scale (1-31, lower = better quality). |
| `output_root` | `${CHASSIS_HOME}/temp` | Where per-video output folders are written. |

All knobs honored via environment variables. See `openclaw.plugin.json` for the canonical schema.

## Why this is interesting

Plain Loom transcripts miss the visual narrative entirely. Sampling at 1 frame per 5 seconds is the sweet spot for code and UI content — dense enough to catch transient errors and UI transitions, sparse enough that a 10-minute video yields ~120 frames (a manageable batch size for multimodal review).

The token cost of the frames themselves is negligible compared to the value of having the agent see what was actually on screen during a walkthrough. For Claude Code in particular, this lets you treat Loom recordings as first-class context for code review and debugging — drop a URL into your session and continue working with full visual awareness.

## Built by

This plugin ships standard with [Behalf.bot](https://behalf.bot) — battery-included Claude Code installs for operators and Build School students. The full Behalf.bot stack bundles Loom Vision alongside dating-app automation, body-for-life journaling, restaurant booking, reMarkable tablet sync, and more, with a single `bootstrap.sh` that wires everything to your local infrastructure.

If Loom Vision is useful to you on its own, the rest of the Behalf.bot plugin catalog is probably interesting too.

## License

See repo LICENSE.
