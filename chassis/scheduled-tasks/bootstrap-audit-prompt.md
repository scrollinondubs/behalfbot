# Bootstrap audit triage

The chassis `bootstrap-audit.sh` routine has flagged one or more failing checks on this install. Each failure represents a silent install gap that, left unfixed, will surface later as a behavior bug the installer notices and Jax has to chase down.

## What you have

Read the audit transcript at the path passed in via the gather output (`transcript_path` field). It is the full output of the audit including which gaps failed and the concrete fix commands suggested.

The transcript walks five known install gaps:

1. **HEARTBEATS.md backup row** - a regular backup heartbeat is registered. Failure means scheduled backups aren't firing.
2. **Customer GitHub remote** - `origin` points at the customer's own private repo, not the chassis template. Failure means changes here only exist on this machine.
3. **Memory MCP** - the knowledge graph MCP is wired into `.mcp.json` and its `MEMORY_FILE_PATH` is writable. Failure means narrative memory across sessions is broken.
4. **LaunchDaemons** - `com.behalfbot.<bot>-discord-restart` and `-discord-watchdog` are loaded in the system domain. Failure means the Discord bridge has no persistent `claude` process to route into.
5. **Tmux session** - the bot's tmux session exists. A failure here is a warning unless paired with a failing LaunchDaemon check (the daemon creates the session at 05:00 + RunAtLoad).

## What to do

1. **Read the transcript file** to see exactly which checks failed and what fix commands the audit suggested.
2. **Triage by severity.** A missing `origin` or missing backup row is recoverable any time. A non-loaded LaunchDaemon means the bot has been silent on Discord since the failure started - higher priority.
3. **Propose a fix plan** in a Discord message to `#jax-ops`. Include:
   - Which gap failed
   - The fix command from the transcript
   - Whether you need Sean's `sudo` (LaunchDaemon installs do; backup row does not)
   - An estimate of any ongoing impact (e.g. "Discord has been routing to a dead session for N days")
4. **Do not auto-execute** a `sudo` command. Surface for Sean's approval first. Non-sudo fixes (HEARTBEATS.md append, `git remote set-url`) can be done via PR following the normal workflow.

## Important

- The audit ran on a recurring weekly schedule. Same failure two weeks in a row means the first triage didn't stick. Read the prior week's discord message (search `#jax-ops` for `bootstrap-audit`) before suggesting the same fix again.
- If you can't make sense of a failure, ask Sean in `#jax-ops` rather than guessing. Better to escalate than auto-fix the wrong thing.

## Out of scope

- Implementing missing features the audit doesn't check (e.g. signup interview depth, schema validation). Those are separate workstreams. Stay narrow on the 5 gaps the audit covers.
