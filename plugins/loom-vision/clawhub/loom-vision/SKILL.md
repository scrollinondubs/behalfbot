---
name: loom-vision
description: Process a Loom share URL into multimodal context - downloaded video, sampled frames at one frame per 5 seconds, and the auto-generated transcript. Triggers when the user shares a Loom URL, asks what is happening in a Loom video, or wants visual context beyond the transcript.
version: 1.0.0
homepage: https://behalf.bot
metadata:
  openclaw:
    emoji: "🎥"
    homepage: https://behalf.bot
    requires:
      bins:
        - loom-dl
        - ffmpeg
    install:
      - id: node-loom-dl
        kind: node
        package: loom-dl
        bins: [loom-dl]
        label: Install loom-dl (npm)
      - id: brew-ffmpeg
        kind: brew
        formula: ffmpeg
        bins: [ffmpeg]
        label: Install ffmpeg (brew)
    envVars:
      - name: OUTPUT_ROOT
        required: false
        description: Directory where per-video output folders are written. Defaults to a loom-vision folder under the system temp directory.
      - name: FRAME_INTERVAL_SECONDS
        required: false
        description: Seconds between sampled frames. Default 5. Lower for fast-changing UI, higher for slow walkthroughs.
      - name: FRAME_MAX_WIDTH_PX
        required: false
        description: Max width in pixels for sampled frames. Default 1280.
      - name: FRAME_QUALITY
        required: false
        description: ffmpeg -q:v JPEG quality, 1-31, lower is better. Default 3.
---

# Loom Vision

Use this skill whenever:

- The user pastes a Loom share URL (`https://www.loom.com/share/<32-hex-id>` or similar).
- The user asks you to "watch", "review", "summarize", or "explain" a Loom video.
- The user references a Loom video they recorded and asks a question that needs visual context (e.g. "what was the error on screen at minute 3", "did I click the right button", "what's the diff in that PR walkthrough").

The skill exists because Loom's auto-transcript captures only what was said aloud - not what was on screen. For code walkthroughs, design reviews, and bug repros, the screen contents are often more important than the audio.

## How to invoke

Run the processor script with the Loom share URL. It downloads the mp4, samples frames, and prints the output directory path.

```bash
bash {baseDir}/process-loom.sh "<loom-share-url>"
```

The script prints the output directory path to stdout. Inside that directory:

- `video.mp4` - the original video (kept in case you want to re-process at a different frame interval)
- `transcript.vtt` - Loom's auto-transcript with timestamps
- `frame_001.jpg`, `frame_002.jpg`, ... - sampled frames at 1 frame per N seconds (default 5)

## What to do with the output

Read the transcript first to anchor timing, then browse the frames that fall in the relevant time window. For a 10-minute video at 5-second sampling that's 120 frames - review them in batches or jump to the specific window the user asked about.

Frame N corresponds to timestamp `(N - 1) * FRAME_INTERVAL_SECONDS` from the start of the video. Default interval is 5 seconds, so frame_007.jpg is at the 30-second mark.

## Working with the output

Common patterns:

- **Summarize the whole video.** Read the full transcript, then sample every 6th frame (one per 30 seconds) for the visual narrative. Combine.
- **Answer a specific question.** Search the transcript for the relevant keyword, find the timestamp, look at the frame(s) within plus or minus 10 seconds.
- **Spot what's missing from the transcript.** Errors, popup dialogs, UI states, and on-screen code rarely make it into the spoken audio - go directly to the frames.

## Configuration

All knobs are environment variables, set before invoking the script:

- `FRAME_INTERVAL_SECONDS` - default 5. Lower for densely visual content (animations, fast UI changes), higher for slow walkthroughs.
- `FRAME_MAX_WIDTH_PX` - default 1280. Bump up for tiny-font code review videos.
- `FRAME_QUALITY` - default 3 (ffmpeg `-q:v` scale, 1-31, lower = better).
- `OUTPUT_ROOT` - default `${TMPDIR:-/tmp}/loom-vision`.

## Dependencies

Two system CLIs are required (declared in the install specs above so OpenClaw can offer to install them):

- `loom-dl` - install with `npm install -g loom-dl` (Node CLI, not on Homebrew)
- `ffmpeg` - install with `brew install ffmpeg` (macOS) or your distro's package manager (Linux)

Both installs are idempotent.

## Why this exists

Most Loom-style integrations treat the video as audio-with-pictures: they ingest the transcript and stop. That loses the entire visual half of a walkthrough - IDE state, error popups, on-screen code, design mockups, the actual change in a PR review.

Sampling at 1 frame per 5 seconds is the sweet spot for code/UI content: dense enough to catch transient errors and UI transitions, sparse enough that a 10-minute video yields 120 frames (manageable batch size for multimodal review). The transcript provides the audio narrative; the frames provide everything else.
