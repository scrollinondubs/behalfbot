# Install MVP scope

> **North-star metric for an install: time to the installer's first delightful experience.** That's the morning briefing landing in their Discord at 08:00 their local time, sourced from their second brain (Notion / SiYuan / Obsidian), in a voice that sounds like them.
>
> Everything else is layered on after the installer has tasted that experience. The MVP base install ships fast. Plugins arrive in a second pass.

This doc was authored 2026-05-07 from Sean's voice memo direction (`#<primary>` Discord message 1501881959276744735, ratified 1501885768585580564) plus the empirical findings of installer-1's V1 install (`docs/install-journals/installer-1-2026-05-06.md`). The reasoning is captured here so the design decisions can be revisited or revised when Marc / installer-3 install pressure-tests the model.

## What's in the MVP base install

The minimum surface area required to land a working morning briefing on day one.

| Layer | In MVP base? | Why |
|---|---|---|
| chassis container (image) | ✅ | Foundation — Python 3.12 + uv + zsh + apt deps + chassis source |
| Vaultwarden container | ✅ | Required for credential hydration; self-hostable, works offline |
| Postgres container | ✅ | Per Sean's "Postgres-from-start" call — avoids painful mid-life SQLite→Postgres migration |
| Memory pre-seed | ✅ | Installer-authored `about-me.md` + `my-company.md` + universal feedback files; drives Claude Code voice from day 1 |
| Heartbeat dispatcher | ✅ | Schedule infrastructure for the morning briefing + future heartbeats |
| **Morning briefing heartbeat** | ✅ | THE delightful experience — first message lands at 08:00 installer-local |
| Discord intake + reply helpers | ✅ | Primary surface for installer↔chassis conversation |
| Second-brain adapter | ✅ | One backend (Notion / SiYuan / Obsidian) picked at install time; briefing has somewhere to source data + write to |
| `.env` hydration from Vaultwarden | ✅ | Wires the above without baking secrets into the image |
| Briefing pipeline | ✅ | md → HTML → Tailscale-hosted briefing server (chassis ships the static-site server) |
| Voice transcription helpers | ⚠ optional | Whisper is needed only if the installer wants voice-note intake from Discord; defer |
| Loom processing | ⚠ optional | Same — defer until installer asks for video-note intake |

## What's NOT in the MVP base (post-install plugins)

Each of these is its own opt-in install pass after base is live. They have substantial surface area + their own failure modes; bundling them into base risks the morning-briefing-day-1 promise.

| Plugin | Why post-install |
|---|---|
| BFL (Body for Life) | 30+ scripts, photo-pipeline, vision/FDC/OFF macro choice, schema migrations |
| Dating | Android emulator + ADB + GPS spoofing + verify-match + Hinge/Tinder/Bumble surfaces |
| Angel Protocol | SMS provider OAuth + location provider + emergency-contacts elicitation + Phase 0 cascade |
| WhatsApp / Telegram-monitor | Per-platform bot setup + allowlist policy + chat-ID enumeration |
| Pacman (URL → proposal) | Firecrawl/WebFetch + 4-gate pipeline + SiYuan/Notion integration |
| LP CRM Outreach | Notion CRM schema + Gmail OAuth + draft-and-approve flow |
| Conferences / Events / Sales Safari | Each has its own surface |

Plugin scope is captured during the interview but does NOT gate the base install. The interview records "yes I want BFL" → that becomes a post-install plugin install request, not a base prerequisite.

## Two-phase Confabulator-fork interview

The Confabulator-fork interview that generates a per-installer repo runs in two phases. Phase 1 gates base install. Phase 2 is asynchronous, fires once base is running.

### Phase 1 (gates base install — ~10 min)

1. Deployment target (Linux bare-metal / Cloudflare Containers / Mac Mini / Hetzner)
2. Primary surface (Discord / Slack / Telegram — for V1, Discord is canonical)
3. Second-brain backend (Notion / SiYuan / Obsidian) + one root id (page or notebook)
4. Identity isolation pattern (dual-identity vs single-identity)
5. **Installer-deliverable artifacts:**
   - `about-me.md` (~1-3 pages) — how they work, voice, rules, instructions for Claude
   - `my-company.md` (if applicable) — fund/business context, goals, contrarian thesis
6. Vaultwarden master account email (a separate field from agent identity email; common assumption mismatch - see LESSONS_FROM_V1.md)
7. Linux user on the deployment target (a separate field from Tailscale identity email; common gotcha - see LESSONS_FROM_V1.md)
8. Briefing target time + timezone (default 08:00 installer-local)

### Phase 2 (plugins — fires post-base, asynchronous)

For each plugin the installer indicated in Phase 1, a self-contained sub-interview captures the plugin-specific config. Each Phase 2 interview is independent + can be deferred:

- **BFL:** macro targets (protein/carbs/fat per day), Strava yes/no, Oura yes/no, photo-source path, vision-vs-DB macro source preference
- **Dating:** apps to enable, taste references + hard rules, photo-verification backends, escalation rules, Angel Protocol prerequisite handling
- **Angel Protocol:** emergency contacts (N people, SMS-reachable numbers, relationship tags), location provider preference, duress codeword
- **WhatsApp:** allowlisted groups, monitor cadence
- **Pacman:** target proposal location in second brain
- **LP CRM Outreach:** Notion CRM database structure, Gmail OAuth scope confirmation, voice/tone calibration

Phase 2 interviews can be deferred indefinitely — installer says "I want to think about it" and the plugin install just doesn't queue. The base install keeps working. No blocking.

## Plugin install pattern

Plugins are NOT installed by Sean+${ASSISTANT_NAME} in the loop (V1 closed-loop pattern is for the BASE install only). Plugin installs are chassis-driven, idempotent, validation-checked.

**Pattern per plugin:**

1. Chassis ships each plugin under `plugins/<name>/` with:
   - `openclaw.plugin.json` frontmatter declaring deps, secrets-needed, MCP additions, schedule additions
   - `install.sh` self-contained installer script
   - `validate.sh` self-contained post-install validation
   - `prompt.md` for any plugin-interview questions
2. Plugin install command: `docker compose run chassis install-plugin <name>`
3. The script:
   - Reads plugin config from `chassis.config.yaml.modules.<name>`
   - Pulls plugin-specific secrets from Vaultwarden via `rbw`
   - Adds plugin's MCP servers to `.mcp.json`
   - Adds plugin's heartbeats to `HEARTBEATS.md`
   - Runs schema migrations (Postgres) if any
   - Runs `validate.sh` to confirm install
4. On validate-pass: plugin is live. On validate-fail: plugin install rolls back, error reported via Discord ops webhook.

## Plugin opt-in via Discord-react pattern

Once base is running, the chassis can prompt the installer in Discord asynchronously:

```
🤖 Behalf.bot Phase 2 ready
Want to install one of these?
  🍳 BFL (Body for Life — daily macros + workout log)
  💘 Dating (Hinge automation)
  🆘 Angel Protocol (personal-safety duress cascade)
  📱 WhatsApp group monitor
React with the emoji to start. Skip = no plugin queues.
```

Installer reacts → chassis fires `install-plugin <name>` → reports completion (or rollback) via Discord. Reduces friction, no terminal access needed for plugin opt-in.

## V1 closed-loop implications (installer #1 / Marc / Toby)

The first three installs use Sean+${ASSISTANT_NAME} SSH-driven base install. The MVP-base scope split applies to them too:

- **Installer #1 (bare-metal, in flight):** base install effectively complete (chassis on host, heartbeat dispatcher live, smoke-test counter ticking 2026-05-07). Plugins layer in week 2 as Phase 2 sub-installs.
- **Marc (installer #2, ~2-3 weeks out):** base install lands first (1-2 hours instead of V1's 6-8 hours). Marc gets a working morning briefing on day 1. Plugins layer in week 2-3.
- **Toby (installer #3, ~4-6 weeks out):** base install pattern is now battle-tested + documented; targeting near-zero Sean+${ASSISTANT_NAME} in-loop time for base install. Plugins layer in same week-2-3 cadence.

After Toby, the V2 README-driven hydration goal (no Sean+${ASSISTANT_NAME} in loop, installer's Claude Code reads README + drives) becomes feasible because the base install is empirically reduced + the per-plugin installs are already chassis-driven not human-driven.

## Chassis image variant question (open)

`MVP base` could map to a separate chassis image variant — `behalfbot/chassis:base-vX.Y.Z` vs `behalfbot/chassis:full-vX.Y.Z`. Lean: **single image with all plugins available but only those listed in `chassis.config.yaml.modules.*` enabled at runtime.** Lighter cognitive overhead than maintaining two images. The plugin scripts only execute if their config entry is `enabled: true`. The chassis dispatcher only fires plugin heartbeats listed in HEARTBEATS.md (which the plugin install adds). Net effect: an "MVP base only" install + a "BFL+dating+angel" install run the same image, differing only in their `chassis.config.yaml` and `.env`.

## Implementation roadmap going into installer-2 install

1. **Containerize the chassis** (Sean directive 2026-05-07) - deferred during V1 install per "moot if we containerize" call. Lands as the first PR after V1 smoke test signs off. Chassis image baked from current `main` + the cross-platform fixes that V1 bare-metal currently has as a local patch (see LESSONS_FROM_V1.md).
2. **Phase 1 interview spec** - extract from V1 install journal (`docs/install-journals/installer-1-2026-05-06.md`). Every install-time correction V1 install required = a Phase 1 question. Output: `docs/confabulator-fork-phase1-spec.md`.
3. **Plugin install scripts** — refactor existing `plugins/<name>/` directories to ship `install.sh` + `validate.sh` per the pattern above. BFL + dating + angel-protocol + whatsapp first; outreach + pacman later.
4. **`docker compose run chassis install-plugin` orchestrator** — one chassis script that reads `chassis.config.yaml.modules.<name>`, drives the plugin install, reports via Discord ops webhook.
5. **Discord-react opt-in flow** — chassis script that posts the Phase 2 menu + listens for reactions + dispatches plugin installs. Probably a heartbeat on its own (`phase-2-prompt`) that fires N days after base install + listens for installer reaction.

## Cross-references

- Discord conversation: `#<primary>` 2026-05-07 messages 1501881959276744735 → 1501885768585580564 (Sean's voice memo + ratification)
- `docs/install-journals/installer-1-2026-05-06.md` — empirical input
- <v1-reference-install> issue #494 — installer-1 install source-of-truth
- <v1-reference-install> issue #365 — Behalf.bot V1 beta plan
- Memory: `project_lakoff_install_state.md` (V1 install state - informs the cross-installer comparison)
