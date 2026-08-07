# voice-agent

Talk to the agent out loud. See [docs/voice-agent.md](docs/voice-agent.md) for
what it does, how to set it up, and what it deliberately does not do.

## Why this is here and not in behalfbot-plugins

Shared plugins live in `scrollinondubs/behalfbot-plugins`, fetched by both
chassis rather than vendored (decision record: behalfbot#53). **This one is a
deliberate exception, not an oversight.**

Escalation shells out to `claude -p --resume <session-id>`. The resume is the
feature, not a detail: it is what makes a voice exchange a conversation rather
than a series of unrelated questions, and it is also what makes follow-ups
roughly six times faster than a cold invocation. `opencode` has no equivalent
headless-session flag, so `BehalfBot-open` cannot use this plugin today.

Putting it in the shared repo would mean a plugin only one chassis can enable,
plus a fetch path to maintain, in exchange for nothing.

**It moves when the escalation backend is genuinely pluggable**, which needs one
question answered first: can OpenCode hold a session across separate headless
invocations? If it can, abstract the backend and move the whole thing. If it
cannot, an opencode implementation would be every turn cold with no follow-ups,
which is worse than not shipping it there at all.

Tracked in behalfbot#140. Please read that before moving this directory.

## Also worth knowing

This plugin is **macOS and Apple Silicon only** and refuses to install anywhere
else. That inverts the house assumption in behalfbot-plugins, whose convention
is Linux-first with no brew fallback. Not a bug: Whisper and Kokoro are MLX
builds, and the chassis container is Linux, so this runs on the host under
launchd rather than in the container.
