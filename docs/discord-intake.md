# Discord Intake

Discord is the chassis's primary surface. Inbound: voice notes, Loom video links, custom trigger keywords, plain text. Outbound: webhooks for briefing notifications, ops alerts, lead signals, etc.

This document covers the chassis-side helper scripts that process inbound Discord activity. The Discord plugin / channel-allowlisting + the trigger-keyword pattern itself are documented separately (see `chassis/CLAUDE.md.template`'s "Discord triggers" section in any installer's CLAUDE.md).

## Inbound architecture: channels plugin (canonical) vs heartbeat polling (legacy)

**Canonical: Claude Code's `--channels plugin:discord@claude-plugins-official` feature.** A long-running `claude` process (typically in `tmux`) holds a Discord WebSocket connection. Inbound messages push from Discord → arrive in the running session as `<channel source="plugin:discord:discord">` events → Claude reacts in real-time → the reply tool posts back. Reply latency: seconds, not minutes. Reference: https://code.claude.com/docs/en/channels.

**Legacy: heartbeat-polling Discord.** A scheduled gather script calls `mcp__discord__fetch_messages` every N minutes, diff against a watermark, hands new messages off to a trigger handler. Higher latency (bounded by heartbeat tick), more code per installer, and only useful for installers who don't have access to the channels plugin.

**Use the channels plugin for any installer that supports it.** Reserve heartbeat polling for:
- Scheduled work (daily morning briefing, hourly data pulls, cron-style jobs)
- Installers running on platforms where a long-running `claude --channels` process isn't viable
- Channel-monitoring patterns where the chassis wants to react to messages NOT directed at the bot (e.g. scanning a Slack channel for keyword hits)

**Why the split:** the channels plugin is the right primitive for "respond to the user". The heartbeat dispatcher is the right primitive for "do scheduled work". They're complementary; both run side-by-side in a typical install. The chassis architecture in this repo evolved from heartbeat-only because the channels feature shipped post-V1; new installers should default to channels for interactive surface + heartbeat for scheduled.

### Setup pattern (channels plugin in tmux)

After cutover (installer's Claude Code OAuth pointed at their own account), on the installer's chassis host:

```
# Install the plugin (one time, requires Bun on the host)
claude
/plugin install discord@claude-plugins-official
source ~/behalfbot/.env
/discord:configure $DISCORD_BOT_TOKEN
/exit

# Persistent tmux session that holds the WebSocket
tmux new -d -s ozzy-discord 'cd ~/behalfbot && source .env && claude --channels plugin:discord@claude-plugins-official --dangerously-skip-permissions'

# Pair the installer's Discord account
tmux attach -t ozzy-discord
# Inside Claude:
/discord:access pair <code-from-discord>
/discord:access policy allowlist
# Detach: Ctrl+B then D
```

The `--dangerously-skip-permissions` flag is necessary so the long-running session doesn't hang on permission prompts when the installer is away. Per the install architecture's hard-limits hooks at `chassis/.claude/hooks/guardrails.sh`, dangerous operations are still blocked at the tool-call layer regardless of this flag.

The pre-staged `~/.claude/channels/discord/access.json` (see chassis hydration step) carries the installer's Discord user_id allowlist, so the pair flow is fast.

---

## Helper scripts

All four are at `chassis/scripts/`:

### `transcribe-voice.sh <audio-file>`

Local-first voice transcription via [whisper.cpp](https://github.com/ggerganov/whisper.cpp). Accepts ogg / mp3 / wav / flac / m4a; converts non-WAV to 16kHz mono via ffmpeg before passing to `whisper-cli`. Returns plain text on stdout.

Environment overrides:
- `WHISPER_MODEL_PATH` — defaults to `/opt/homebrew/share/whisper-cpp/ggml-small.bin` (macOS Homebrew path). Set explicitly on Linux installers.
- `WHISPER_CLI` — override the binary if it's installed somewhere non-standard.

Pattern: when a Discord message has an audio attachment (content type starts with `audio/`), the chassis downloads the attachment, runs this script, treats the transcribed text as the typed message body. The user does NOT see "I transcribed your voice note as: ..." — the transcription is a transparent input transformation.

### `text-to-speech.sh "<text>" [output.mp3]`

OpenAI TTS via the `/v1/audio/speech` endpoint. Canonical voice = **Onyx** (per the V1 reference install + memory). The chassis does NOT fall back to macOS `say` — Onyx is the chassis voice for any installer that activates voice replies. Failing loud beats shipping a robotic-sounding fallback.

Hard limit: OpenAI rejects inputs >4096 chars. Caller chunks before calling.

Environment:
- `OPENAI_API_KEY` (required) — hydrate from password manager
- `OPENAI_TTS_VOICE` (default "onyx") — override only if installer explicitly chose a different voice
- `OPENAI_TTS_MODEL` (default "tts-1-hd") — `tts-1` is half the cost, lower fidelity

Pattern: ONLY generate a voice reply when the user explicitly asks ("read it back", "voice reply", "audio"). Default reply is text. When generating, attach the MP3 to the Discord message via the bot, AND include the text reply alongside.

### `process-loom.sh <loom-share-url>`

Loom share URL → output directory containing:
- `video.mp4` — full download
- `frame_001.jpg` ... `frame_NNN.jpg` — keyframes every 5 seconds, 1280px wide
- Loom's built-in transcript JSON (when available)

Output directory rooted at `${CHASSIS_HOME}/temp/loom-<video-id>/`. Caller reads what it needs.

Dependencies: `loom-dl` (npm install or your equivalent) + ffmpeg.

Pattern: when a Discord message contains a `loom.com/share/...` URL, the chassis reacts with 🎬, runs this script, then reads the transcript + key frames to understand what the user is showing. Treat it like a voice note — execute the task being demonstrated, don't echo back what was shown.

### `post-to-channel.sh <channel-key> "<message>"`

Generic Discord webhook poster with channel-key resolution. Channel keys (`ops`, `briefings`, `leads`, `social`, plus any installer-defined custom keys) map to webhook env vars. Resolution:

```
INSTANCE_NAME=OZZY, channel-key=ops  →  reads OZZY_OPS_WEBHOOK_URL
INSTANCE_NAME unset, channel-key=ops  →  reads OPS_WEBHOOK_URL
```

`INSTANCE_NAME` is also displayed as the message sender in the webhook payload (`username` field). Defaults to "Behalf.bot" when unset.

Pattern: every chassis-side heartbeat / hook / plugin that needs to surface signal to the installer routes through this script. Avoids each script re-implementing webhook auth, payload shape, fallback logic.

---

## Trigger-keyword pattern

The chassis supports the trigger-keyword pattern (e.g. `Pacman <url>`, `Backfill: protein shake 9am`) two ways: the LLM-driven dispatch documented in `chassis/CLAUDE.md.template`'s "Discord triggers" section, and the deterministic dispatch framework documented in the next section. The two are complementary — installers can use either or both.

In both modes, each trigger:

1. Detects its keyword case-insensitively at the start of an inbound Discord message in a known channel
2. Parses the message body per the trigger's grammar
3. Reacts with an emoji to acknowledge receipt
4. Hands off to the right script / heartbeat / worker / plugin

The chassis ships ZERO triggers by default — installers add the ones their use case demands. Plugins ship triggers in their `openclaw.plugin.json` `contracts.triggers` array; the bootstrap merge step writes them into `chassis/triggers.yaml` for the deterministic dispatcher.

---

## Trigger-dispatch framework (deterministic path)

Two scripts and a registry file:

- **`chassis/scripts/dispatch-trigger.sh <channel-id> <message-id> <body>`** — invoked per inbound message. Pattern-matches against the registry; on first match, runs the trigger's parser, optionally reacts to the source message, then invokes the trigger's handler. Emits a single JSON object on stdout describing the outcome.
- **`chassis/scripts/merge-plugin-triggers.sh`** — bootstrap utility. Reads `chassis.config.yaml` to find enabled plugins, pulls `contracts.triggers` from each plugin's `openclaw.plugin.json`, writes the merged set to `chassis/triggers.yaml`. Re-run any time a plugin is enabled/disabled or its manifest changes.
- **`chassis/triggers.yaml`** — the registry. Generated; do not hand-edit unless you're adding an installer-specific (non-plugin) trigger. The chassis ships `chassis/triggers.yaml.template` as the no-op default.

### Why use this in addition to LLM-driven dispatch?

1. **Determinism.** Known-shape messages (`Backfill:`, `Pacman <url>`) match against a regex registry in milliseconds. No LLM tokens spent on patterns the chassis already knows.
2. **Plugin distribution.** When a plugin ships a trigger (e.g. BFL ships `Backfill:`), the chassis merges it into the registry at install time. The installer never has to hand-edit CLAUDE.md to add it.

LLM-driven dispatch still wins for ambiguous or evolving triggers (where the parser would need real language understanding) and for triggers that interact with other context the LLM is already considering. The deterministic path is for the cheap obvious wins.

### Schema (one entry per `- name:` block under `triggers:`)

| Field | Required | Default | Purpose |
|---|---|---|---|
| `name` | yes | — | Unique trigger identifier (kebab-case) |
| `plugin` | yes | — | Plugin id (e.g. `behalfbot-bfl`) or `core` |
| `keyword_regex` | yes | — | Case-insensitive Python `re` pattern matched against message body |
| `channel_filter` | no | `*` | Exact channel id, `*` for any. Supports `${ENV_VAR}` expansion |
| `parser` | no | `passthrough` | Bare name (resolves to `chassis/scripts/parsers/<name>.sh`) or absolute path |
| `handler` | yes | — | Absolute path; supports `${ENV_VAR}` |
| `react_emoji` | no | `""` | Unicode emoji to react with on match (skipped if empty or no DISCORD_BOT_TOKEN) |

### How handlers receive args

Handlers run as separate processes with these env vars set:

- `TRIGGER_NAME`, `TRIGGER_PLUGIN`
- `TRIGGER_CHANNEL_ID`, `TRIGGER_MESSAGE_ID`, `TRIGGER_MESSAGE_BODY`
- `TRIGGER_PARSED_ARGS_JSON` — the parser's stdout (a JSON object). E.g. for `url-extract`, `{"urls": [...], "url_count": N, "raw": "<body>"}`.

Handler stdout flows back through the dispatcher's JSON output as `handler_stdout`; same for stderr. The handler's exit code lands as `exit_code`.

### Built-in parsers

Both ship at `chassis/scripts/parsers/<name>.sh`; both read message body on stdin and emit a JSON object on stdout.

- **`passthrough`** — `{"raw": "<body>"}`. Use when the handler does its own natural-language parsing.
- **`url-extract`** — `{"urls": [...], "url_count": N, "raw": "<body>"}`. Use for URL-introducing triggers (Pacman-style).

Plugins can ship their own parsers under `plugins/<plugin>/scripts/parsers/` and reference them by absolute path in their manifest.

### Reactions

If `DISCORD_BOT_TOKEN` is set in the chassis `.env` and `chassis/scripts/discord-react.py` is executable, the dispatcher reacts to the source message with the trigger's `react_emoji` before invoking the handler. Without the bot token, `react_status: "emit_only"` lands in the JSON output and the calling heartbeat is responsible for reacting (e.g. via the discord MCP from a `claude -p` prompt).

### Plugin author guide

To declare a trigger from a plugin, add a `contracts.triggers` array to your `openclaw.plugin.json`:

```json
{
  "id": "behalfbot-bfl",
  "contracts": {
    "triggers": [
      {
        "name": "backfill",
        "keyword_regex": "^Backfill[\\s:]+",
        "channel_filter": "${HEALTH_CHANNEL_ID}",
        "parser": "bfl-natural-language-meal",
        "handler": "${CHASSIS_HOME}/plugins/bfl/triggers/backfill.sh",
        "react_emoji": "🍴"
      }
    ]
  }
}
```

The bootstrap merge step writes that into `chassis/triggers.yaml`. `${HEALTH_CHANNEL_ID}` and `${CHASSIS_HOME}` expand against the runtime environment at dispatch time.

Ship your handler at `plugins/<plugin>/triggers/<name>.sh` and use `${CHASSIS_HOME}` so the path is portable across installers. For parsers, either use a built-in or ship your own at `plugins/<plugin>/scripts/parsers/<name>.sh` and reference by absolute path.

---

## Lessons baked in

- **#1** — Discord channels are the right inbound primitive. Don't reach for Anthropic Dispatch or `/voice`; build on this layer.
- **#16** — `UserPromptSubmit` hooks don't fire on Discord-channel inbound. If you need to track channel activity, query the channel directly via `mcp__discord__fetch_messages` from a gather script. This script-set assumes that pattern.
- **#30** — privacy boundaries are surface-specific. Voice notes, Loom videos, and direct text from external participants all carry privacy expectations. Treat them per-surface.

---

## Integration with the channels plugin (canonical) + heartbeat dispatcher (scheduled work)

These helper scripts are NOT heartbeats themselves. They're sub-utilities the installer's chassis calls from BOTH the long-running channels-plugin tmux session AND the heartbeat dispatcher.

**From the channels-plugin tmux session (interactive, real-time):**

- Inbound message arrives via `<channel source="plugin:discord:discord">` event
- Claude (running in the tmux session) inspects the message inline
- If attachment is audio → shell out to `transcribe-voice.sh`; if URL is Loom → `process-loom.sh`; if text matches a trigger keyword → dispatch via `chassis/scripts/dispatch-trigger.sh`
- Reply via the discord plugin's `reply` tool (NOT `post-to-channel.sh` — `reply` carries thread context)

**From the heartbeat dispatcher (scheduled, batch):**

- Per-installer scheduled gather scripts (e.g. `gather-cohort-digest.sh`) run on cron tick
- Output structured JSON; if condition fires → invoke `claude -p` with the gathered context
- `claude -p` posts results to the relevant channel via `post-to-channel.sh` (webhook-based, no thread context needed for scheduled posts like a daily briefing)

**Anti-pattern:** running a "Discord-poll heartbeat" that polls fetch_messages every N minutes for INTERACTIVE responses. Use the channels plugin for that. Polling is appropriate ONLY for: (a) channel-scanning where the bot isn't a participant (e.g. scanning a separate Slack workspace's channel for mentions), (b) installs that can't run a long-running `claude --channels` session, (c) historical-window data ingestion (e.g. nightly fetch-last-24h-messages-from-archive-channel).

---

## Cross-references

- `chassis/scripts/transcribe-voice.sh` `text-to-speech.sh` `process-loom.sh` `post-to-channel.sh`
- `chassis/CLAUDE.md.template` "Discord triggers" section (per-installer keyword definitions)
- `docs/heartbeat-dispatcher.md` — how heartbeats use these helpers
- `docs/LESSONS_FROM_V1.md` — full lesson list, especially #1, #16, #30
