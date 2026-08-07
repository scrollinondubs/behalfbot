# Voice agent

Talk to your agent out loud. Speech recognition, speech synthesis and the
routing decision all run on your own machine; only escalated questions leave it.

## Read this before enabling

**Apple Silicon only.** Whisper and Kokoro are MLX builds. They do not run on
Intel Macs, on Linux, or in the chassis container. If your install runs anywhere
other than an Apple Silicon Mac, this plugin cannot work and the installer
should not have offered it to you.

**Your host needs Claude Code.** Escalations shell out to `claude -p` using your
install's own `.mcp.json`. A managed or hosted install has no local Claude Code
to call, so escalation has nowhere to go.

**It downloads several GB of models** on first run - Whisper, Kokoro, and
whatever Ollama models you point it at.

## What it actually does

Every spoken turn takes one of three paths.

**Answered here.** A small local model handles it inside the voice loop, about a
second. Nothing leaves the machine.

**Escalated.** The full agent answers, with tools, memory and repo access. About
four seconds when it needs no tools, twenty to thirty when it queries something,
and roughly $0.35 for a tool-using turn. The answer is spoken and also posted to
your chat channel, so walking away does not lose it.

**Refused.** The utterance matched your sensitive-terms list. Nothing is sent
anywhere and the agent says so. You handle it yourself.

That third path is the whole privacy design and it is deliberately the only one
that can stop a turn. See `examples/sensitive-terms.example.json` for why it
refuses rather than redirects.

## Conversations, not questions

Escalations share one session across a conversation, so a follow-up knows what
the previous turn was about. "What about the waitlist for it" resolves "it".

Measured on a three-turn conversation: first turn 26s (cold, queried a
database), second 4.0s, third 4.1s and it correctly recalled turn one. The
continuity is the point; the speed is a side effect of not rebuilding context
from nothing each time.

A conversation is one browser connection. Reconnecting starts clean rather than
resuming something from hours ago.

## Setup

1. Ollama running on the host, with your router and chat models pulled.
2. Copy `examples/aliases.example.json` to `data/aliases.json` and put your own
   vocabulary in it, or leave it empty.
3. Copy `examples/sensitive-terms.example.json` to `data/sensitive-terms.json`
   and **write your own list**. The shipped one is an example from somebody
   else's life.
4. Set `agent_name` and `agent_name_variants`. The variants matter more than they
   look: whisper-base writes "Jack" or "Jacks" for "Jax" most of the time, and
   without them the agent does not know when you are addressing it.
5. `enabled: true`, then install the launchd agent.

## Choosing models

**`stt_model`** is the one worth thinking about. `whisper-base-mlx` is fast and
fine for English commands. For any language where a misheard word changes the
meaning, use `whisper-large-v3-turbo`.

Measured on European Portuguese: base heard *"com o prazo"*, turbo heard *"como
prazo"*. That is "with the deadline" versus "as the deadline", in a sentence
about a deadline. base is cheap in a way that costs you elsewhere.

**`mcp_servers`** is a latency budget, not a wish list. Every server named there
starts before the model answers a word:

| servers | time to a trivial answer |
| --- | --- |
| none | 4s |
| four | 7s |
| fifteen | never returned; killed at 420s |

A browser-automation server on its own was the difference between ten seconds
and never. Name the two or three a spoken question actually reaches for.

## Operating it

`scripts/deploy-voice.sh` fetches, copies what differs, syntax-checks, restarts,
and waits for the port. `--check` reports drift without touching anything.

launchd owns the process, so a reboot brings it back and a crash restarts it.

**The PATH block in the plist is load-bearing.** launchd hands a job a bare PATH.
`run.sh` calls `tailscale serve` under `set -euo pipefail`, so a missing binary
is fatal rather than cosmetic - the first install of it crash-looped twenty times
in nine minutes. Do not remove it.

## What this is not

It is not faster than typing for anything substantial. An escalated question
takes as long as it takes; the speech layer adds to that rather than subtracting.
The win is hands-free, not latency.

It also does not share context with your chat interface. The voice session and
your chat session are separate conversations with the same agent - same files,
same memory, different day. Asking the agent in chat about something you said
out loud will not work.

## Not supported

**Inbound or outbound phone calls.** The routing design assumes one trusted
speaker: spoken overrides are honoured, escalation is the default, and the
confirmation gate authenticates nobody. Every one of those inverts the moment an
untrusted voice is on the line. That is a separate mode and it does not exist.
