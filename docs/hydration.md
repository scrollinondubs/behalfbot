# Hydration — turning artifacts into a running chassis

> Audience: the Claude Code instance (or human) doing the install. Companion to `bootstrap.sh` (the executable orchestrator) and `docs/installer-homework.md` (what the installer pre-stages).

The chassis ships as a generic skeleton. Hydration is the process of turning two installer-specific artifacts — `INSTALL_PROFILE.md` and `chassis.config.yaml` — plus the installer's pre-staged credentials into a working personal AI assistant. This doc is the human-readable walkthrough; `bootstrap.sh` is the executable version (currently a 12-step skeleton with TODOs filling in).

The two artifacts are NOT generated here — they're authored upstream:

- **`INSTALL_PROFILE.md`** — narrative, hand-authored from the installer's onboarding interview (Confabulator-fork V2; hand-drafted for V1 case studies). Contains identity, target environment, channels, tool integrations, use cases, customizations, second-brain choice, identity isolation, V1 install procedure decisions, memory pre-seed list, open questions.
- **`chassis.config.yaml`** — machine-readable companion. Drives toggles + module enables.

If both artifacts aren't present + ratified by Sean, hydration is not safe. **Stop. Ask.**

---

## The 12 steps

What `bootstrap.sh` orchestrates, with the human-readable rationale + the decision points the operator (Claude Code or human) walks through.

### 1. Validate environment + tool prerequisites

Confirm: `CHASSIS_HOME` set; the chassis repo is checked out at it; OS is supported (Ubuntu 22.04+ or macOS); `git`, `python3 >= 3.12`, `node >= 20`, `jq`, `curl`, `ffmpeg`, `sqlite3` are installed; for any plugin that needs Docker (e.g. self-hosted Vaultwarden) Docker is running.

Decision: if a tool is missing, can the operator install it without the installer's hands? Linux: `apt install` is fine. macOS: Homebrew. Mark the install command in the bootstrap log so installer #2 (the next case study) sees the same transcript.

### 2. Hydrate `.env` from password manager

The chassis NEVER reads credentials from the repo. They flow from the installer's password manager (Vaultwarden / 1Password / Bitwarden) into `${CHASSIS_HOME}/.env` at install time.

For each plugin enabled in `chassis.config.yaml.modules`, hydrate the env vars its `openclaw.plugin.json` `configSchema` declares as required. Examples (V1 reference plugin set):

- `behalfbot-bfl` → `FDC_API_KEY`, `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `OURA_PAT`, `BFL_ARCHIVE_DIR`, `HEALTH_CHANNEL_ID`
- `behalfbot-dating` → `SOCIAL_CHANNEL_ID` + photo-verification API keys (TinEye optional, PimEyes if paid)
- `behalfbot-angel-protocol` (when enabled) → `ANGEL_WEBHOOK_URL`, `INSTALLER_DISCORD_USER_ID`, `ANGEL_VAULT_PATH`, `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_PHONE_NUMBER` (if `sms_provider: twilio`)
- `behalfbot-whatsapp` (when enabled) → no env vars; allowlist file at plugin's `data/whatsapp-allowlist.json`

Always-on chassis core:

- `DISCORD_BOT_TOKEN`, `BRIEFINGS_WEBHOOK_URL`, `OPS_WEBHOOK_URL`, `LEADS_WEBHOOK_URL`, `SOCIAL_WEBHOOK_URL`
- `OPENAI_API_KEY` (TTS via `text-to-speech.sh`)
- `INSTANCE_NAME` (e.g. `OZZY`, `MARC`) - used as Discord display name + as a webhook-name prefix
- `INSTALLER_NAME` (e.g. `Alex Smith`) - used in cascade-message bodies

CRITICAL: do NOT export `ANTHROPIC_API_KEY` if the installer is on a Claude subscription. The dispatcher relies on `claude -p` falling back to OAuth (subscription billing) when this var is unset. See `chassis/scheduled-tasks/heartbeat-dispatcher.sh` lines 67-80 for the exact reason — V1 incident: an exported key auto-recharged $50 of API credits before anyone noticed.

### 3. Validate `INSTALL_PROFILE.md` + `chassis.config.yaml` consistency

Cross-check:
- Every `modules.<plugin>.enabled: true` in `chassis.config.yaml` corresponds to a plugin directory at `plugins/<plugin>/` with a valid `openclaw.plugin.json` (run `jq empty plugins/*/openclaw.plugin.json`)
- Every channel referenced in `chassis.config.yaml.surfaces.channel_topology` has a matching webhook URL in `.env` (`<INSTANCE_PREFIX>_<KEY>_WEBHOOK_URL`)
- The `second_brain.backend` value matches an MCP server template under `chassis/mcp-templates/` or `docs/mcp-setup.md`
- The `state_storage` + `database` choices are coherent (don't accept `database: postgres` with `state_storage: local_fs` if there's no local Postgres set up — fail loud)

If anything fails, **stop and surface to the installer**. Don't continue past mismatched config.

### 4. Hydrate `.mcp.json` from template

`chassis/.mcp.json.template` is the chassis-default MCP server set. Hydrate it with `.env` values where placeholders exist, write to `${CHASSIS_HOME}/.mcp.json`. Per-plugin MCP servers are appended based on `modules.<plugin>.enabled`. See `docs/mcp-setup.md` for the per-MCP setup runbook.

Default MCP set (chassis core):
- `memory` (knowledge graph — `@modelcontextprotocol/server-memory`)
- `github` (issue queue + PR ops)
- `discord` (channel intake + reply)
- Whatever the second-brain backend is (notion / siyuan / obsidian)

Plugin-added MCPs:
- BFL plugin: nothing chassis-side; the script set is direct
- Dating plugin: Playwright (verify-match scraping)
- Angel Protocol: Twilio SDK (SMS provider) if enabled

### 5. Hydrate `CLAUDE.md` from template

`chassis/CLAUDE.md.template` is the operating-contract spine. Walk it section-by-section, replacing `{{INSTANCE_NAME}}`, `{{INSTALLER_NAME}}`, `{{INSTALLER_PRIMARY_EMAIL}}`, `{{INSTANCE_DOMAIN}}` with the values from `INSTALL_PROFILE.md`. Append per-plugin sections from each enabled plugin's plugin-specific `CLAUDE.md` block (when it ships one — dating plugin has one; others append their `skills/<name>.md` reference).

Write the result to `${CHASSIS_HOME}/CLAUDE.md`. This file is the chassis-side identity document Claude Code reads on every session.

### 6. Initialize `HEARTBEATS.md`

Copy `chassis/HEARTBEATS.md.template` to `${CHASSIS_HOME}/HEARTBEATS.md`. Append the chassis-default heartbeats:

- `morning-briefing` (daily 08:00, opus, budget 5)
- `github-issue-triage` (every 30m, sonnet, budget 2)
- `daily-log` (daily 23:00, sonnet, budget 1)

Then for each enabled plugin, append the heartbeats it declares in `contracts.heartbeats`:

- `behalfbot-bfl` → `bfl-ingest` (every 15m)
- `behalfbot-dating` → `dating-swipe-1`, `dating-swipe-2` (10:00 + 14:00)
- (Other plugins as their manifests declare.)

### 7. Activate enabled plugins

For each plugin in `plugins/` whose `modules.<plugin>.enabled` is true:

1. Read its `openclaw.plugin.json`
2. Source its activation hook (if `contracts.hooks` declares one — e.g. WhatsApp's `activate_whatsapp_safe_wrapper`)
3. Export the env vars the plugin's runtime depends on (e.g. `BFL_ARCHIVE_DIR`)
4. Merge any plugin-declared `contracts.triggers` into `chassis/triggers.yaml` via `chassis/scripts/merge-plugin-triggers.sh`
5. Run any plugin-specific first-run setup (e.g. SmsProvider's `setup()` — verifies Twilio creds via `/Accounts/{sid}.json`)

Write `${CHASSIS_HOME}/chassis-env.sh` — the env file the launchd / systemd unit sources before invoking the dispatcher. This is THE place the dispatcher's environment is set; not `.env` directly. (`.env` is for human / interactive use; `chassis-env.sh` is for the daemon.)

### 8. Seed memory entries

Per `docs/memory-seeding.md` and `INSTALL_PROFILE.md` § Memory pre-seeding:

Always-on chassis defaults (every install):
- `feedback_never_deceive.md` — Prime Directive
- `feedback_never_commit_coords.md` — privacy guardrail
- `feedback_humanize_copy.md` — public-facing copy quality
- `feedback_no_em_dash.md` — AI-tell scrub

Installer-derived (parsed from `INSTALL_PROFILE.md` § 10):
- `user:<installer-bio>` — what we know about the installer
- `feedback:<installer>-<convention>` — known conventions (e.g. PARA mirror in Notion, dating calibration notes)
- `topic:<installer>-<focus-area>` — current focus areas (e.g. LP pipeline)
- `reference:emergency_contacts` (file path only — actual contacts gitignored)

The seed entries land in the memory MCP's storage (typically `${CHASSIS_HOME}/memory/memory.jsonl`). NEVER seed entries that fabricate facts not in the INSTALL_PROFILE — only what the installer confirmed in the interview.

### 9. Install OS-level dependencies

Install whatever `chassis.config.yaml.deployment` + enabled-plugin manifests require. Per V1 reference: Python 3.12+, Node 20+, ffmpeg, sqlite3 (always); Postgres (if `database: postgres`); Docker (if Vaultwarden self-hosted); Ollama (if `local_models: true`); Android SDK + emulator (if dating plugin + `platforms.hinge.enabled` etc.).

This is the slowest step. ~15-30 minutes depending on the box's network. The bootstrap log captures every package install command so installer #2's bootstrap is a near-identical transcript.

### 10. Set up dispatcher launchd / systemd unit

Linux: write `${HOME}/.config/systemd/user/behalfbot-heartbeat-dispatcher.service` + `.timer` (runs every 15min). `systemctl --user daemon-reload && systemctl --user enable --now behalfbot-heartbeat-dispatcher.timer`.

macOS: write `${HOME}/Library/LaunchAgents/com.behalfbot.heartbeat-dispatcher.plist`. `launchctl load -w ~/Library/LaunchAgents/com.behalfbot.heartbeat-dispatcher.plist`.

Verify the timer/agent is active: `systemctl --user list-timers` (Linux) or `launchctl list | grep behalfbot` (macOS).

### 11. Run smoke tests

For each enabled plugin, verify its basic functionality:

- `discord-ping`: post to `<INSTANCE>_OPS_WEBHOOK_URL` via `chassis/scripts/post-to-channel.sh ops "smoke test from $(hostname)"`. Confirm message lands.
- `gmail-draft-create`: invoke whatever Gmail MCP method the chassis uses to create a draft to the installer's address; confirm the draft appears in their drafts folder.
- `notion-read`: read one page from the configured Notion workspace via the Notion MCP; confirm response.
- Per-plugin smoke tests (BFL: `bfl-ingest --dry-run`; dating: `verify-match.py --self-test`; etc.)

If any smoke test fails, **stop and surface**. The first-heartbeat success criterion (3 consecutive clean briefings) doesn't start counting until smoke tests pass.

### 12. First-heartbeat watch

Hand off to the dispatcher. The first scheduled `morning-briefing` heartbeat fires in N hours. At that time the operator should be ready to debug if it doesn't post — most failures here are channel-misconfig (webhook URL wrong) or memory-MCP-not-initialized (jsonl file empty + read fails).

Once 3 consecutive mornings post a clean briefing to `<INSTANCE>_BRIEFINGS_WEBHOOK_URL`, the install is considered green and ownership transfer (or extended SSH access through V1 iteration) is on the table per the installer's `INSTALL_PROFILE.md` § 9 V1 install procedure decision.

---

## Decision points where the operator stops + asks the installer

These are the moments where Claude Code (or whoever's driving) cannot proceed without the installer:

1. **Step 1 — missing OS-level tool the operator can't install autonomously** (e.g. closed-source SDK, paid software). Surface + wait.
2. **Step 2 — credential not in password manager.** Operator does not invent. Surface + wait.
3. **Step 3 — INSTALL_PROFILE.md and chassis.config.yaml mismatch.** Operator does not silently reconcile. Surface, propose a fix, wait for ratification.
4. **Step 7 — plugin's `setup()` returns non-zero.** That's the plugin's pre-flight validation failing (e.g. Twilio creds invalid). Surface + wait.
5. **Step 11 — any smoke test fails.** Don't continue past this gate to step 12.

The chassis is designed to fail loud. When in doubt, the operator stops and asks. This is the spirit of `docs/architectural-anti-patterns.md` § "no silent dormancy" + § "fail closed on uncertain state."

---

## Re-running hydration

`bootstrap.sh` is idempotent. Re-running with the same `INSTALL_PROFILE.md` + `chassis.config.yaml` + `.env` should produce an identical chassis state. Use this when:

- A plugin gets enabled / disabled in `chassis.config.yaml` (re-runs steps 4-7 to wire / unwire the new module)
- A credential rotates (re-run step 2)
- The chassis itself updates (re-run all steps; the dispatcher's launchd unit gets regenerated to match any new schedule)

The bootstrap log shows what was already done vs. what's freshly applied, so operators can confirm the diff is intentional.

---

## What this document is NOT

- **Not a substitute for `INSTALL_PROFILE.md`.** Hydration without a profile is undefined. Stop.
- **Not the place to add installer-specific configuration.** Per-installer config lives in `chassis.config.yaml`.
- **Not a guarantee.** First-heartbeat success criterion is the actual gate; bootstrap exiting 0 is necessary but not sufficient.

---

## References

- `bootstrap.sh` — the executable orchestrator
- `INSTALL_PROFILE.md` (per-installer) — narrative input
- `chassis.config.yaml` (per-installer) — machine-readable input
- `docs/installer-homework.md` — what the installer pre-stages before bootstrap runs
- `docs/architectural-anti-patterns.md` — what the chassis explicitly avoids (silent dormancy, ANTHROPIC_API_KEY export, etc.)
- `docs/heartbeat-dispatcher.md` — how the daemon decides when to fire `claude -p`
- `docs/discord-intake.md` — voice/Loom helper docs + trigger-dispatch framework
- `docs/security.md` — chassis-shipped guardrails (hook layer, hard limits)
- `docs/mcp-setup.md` — per-MCP wiring runbook
- `docs/memory-seeding.md` — memory pre-seed pattern
- `chassis/CLAUDE.md.template` — operating-contract spine
- `chassis/HEARTBEATS.md.template` — heartbeat registry format (template; renders to `${CHASSIS_HOME}/HEARTBEATS.md` at install)
- `chassis/scripts/dispatch-trigger.sh` — Discord trigger-keyword dispatcher
- `chassis/scheduled-tasks/heartbeat-dispatcher.sh` — gather-first dispatcher
- `LESSONS_FROM_V1.md` — the 30+ lessons baked into the chassis from Sean's V1 install
