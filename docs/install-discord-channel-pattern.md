# Install Discord channel pattern (3-way → 4-way → retirement)

Canonical pattern for the Discord channel that hosts a Behalf.bot install from kickoff through V1 closed-loop iteration and beyond. Codified 2026-05-12 after installer-1's install (#1) surfaced the friction points; applied to installer-2's install (#2) and every installer thereafter.

## The lifecycle

```
Phase 1 (3-way: Sean + ${ASSISTANT_NAME} + installer)
   ↓  Confabulator interview, pre-flight homework, host provisioning
Phase 2 (4-way: Sean + ${ASSISTANT_NAME} + installer + installer's bot)
   ↓  Smoke-test signoff, plugin opt-in, daily ops
Phase 3 (V1 closed-loop iteration window — typically 4-6 weeks)
   ↓  Decision point: full ownership transfer vs paid ongoing support
Phase 4 (retirement OR ongoing support — installer's call)
```

## Phase 1 — Channel creation (Sean's side, before sending homework doc)

**Trigger**: post-Confabulator-interview, decision to onboard the installer to Behalf.bot V1.

**Prerequisites from installer (collected during Confabulator interview)**:

- Installer's Discord username + numeric user ID (e.g. `694927578493878343`)
- Installer's preferred channel name (default: `#<firstname>-behalfbot-setup`)

**Sean's steps**:

1. Create a private channel in the ${ASSISTANT_NAME} server: `#<installer>-behalfbot-setup`.
2. Set permissions: only Sean, JaxBot, and the installer can see / send / read history.
3. Invite the installer via Discord username or user ID.
4. Drop a kickoff message that pings the installer + JaxBot with the pre-flight homework doc link (`docs/installer-homework-<name>.md`).

**Why up-front**: installer-1's install had this happen late and the back-and-forth slowed the kickoff. Doing it before sending the homework means the installer reads the homework in-channel, ${ASSISTANT_NAME} has context for every follow-up, and questions don't fragment across DM threads.

## Phase 2 — Bot joins (mid-install, after first chassis fire)

**Trigger**: the installer's Behalf.bot Discord bot is running on the host box and has connected to Discord's gateway. Confirmed by Sean SSH-ing to the host and seeing the channels-plugin process up + the bot's user shown in the ${ASSISTANT_NAME} server's member list of the installer's own Discord server (NOT the ${ASSISTANT_NAME} server yet).

**Prerequisites from installer's bot** - the chassis emits these automatically on first boot (issue #53 item 4). The installer does not need to hunt through Discord Developer Portal. On first `docker compose up` or first dispatcher start, the chassis calls `chassis/scripts/first-boot-announce.sh`, which:

1. Calls `GET /discord/api/v10/users/@me` with `DISCORD_BOT_TOKEN` to resolve the bot's user ID + username.
2. Posts three lines to the ops channel webhook: bot user ID, OAuth invite URL, and the `/discord:access` command for step 6 below.
3. If the ops webhook is not yet configured (typical at truly-first boot), it logs to stdout with a `BEHALFBOT_FIRST_BOOT:` prefix - find it via `docker logs <container> | grep BEHALFBOT_FIRST_BOOT`.
4. Writes a sentinel at `${CHASSIS_HOME}/state/first-boot-announced.json` so it does not re-post on subsequent boots.

**Installer action**: relay the three lines from the ops channel (or docker logs) to Sean. Do not fish for the ID in Developer Portal - just copy-paste what the bot self-reported.

If the auto-emit did not fire (token not yet set when first boot ran), delete the sentinel and restart: `rm ${CHASSIS_HOME}/state/first-boot-announced.json && docker compose restart`.

**What you need before the steps below**:

- Bot's Discord application ID (= bot's user ID) - from the auto-emit above
- Bot's username - from the auto-emit above
- Confirmation that "Public Bot" is currently ON in the bot's Discord Developer Portal (required for non-owner OAuth installs - this is still a manual step)

**Steps**:

1. **Installer / installer's bot operator**: flip Public Bot ON in Discord Developer Portal → Bot tab → "Public Bot" toggle. Confirm OAuth2 Code Grant is OFF.
2. **Installer**: share the OAuth invite URL with Sean - the auto-emit already generated it, just relay the Line 2 output:
   `https://discord.com/oauth2/authorize?client_id=<BOT_USER_ID>&scope=bot+applications.commands&permissions=379968`
   (Permissions 379968 = standard chat-bot: read messages, send messages, embed links, attach files, read message history. Adjust if the install needs more.)
3. **Sean**: open the URL, pick the ${ASSISTANT_NAME} server from the dropdown, click Authorize.
4. **Sean**: in Discord, server settings → `#<installer>-behalfbot-setup` → permissions → restrict the new bot's role so it can ONLY read / send in this channel (not the rest of the ${ASSISTANT_NAME} server). Default-deny for every other channel.
5. **Sean**: confirm the bot appears in the channel's member list. Drop a test message mentioning the bot: `<@BOT_USER_ID> testing - reply if you see this`.
6. **Installer / installer's bot operator**: on the bot's host, run `/discord:access` to add the ${ASSISTANT_NAME}-server channel to the bot's allowlist:
   ```
   /discord:access group add <CHANNEL_ID> --no-mention --allow <SEAN_DISCORD_USER_ID>,<CHASSIS_BOT_USER_ID>
   ```
   - `--no-mention`: bot responds to any message in the channel without requiring a mention (otherwise every message needs to ping the bot, which is friction)
   - `--allow <ids>`: restrict whose messages the bot responds to (Sean's user ID + JaxBot's bot ID). Filters out installer-side messages from triggering the bot if they accidentally land in this channel.
7. **Installer**: confirm bot is now responding to test messages in the channel. If silent: check Message Content Intent is ON in Dev Portal, bot client is running, channel perms grant Read+Send.
8. **Installer / bot operator**: once confirmed, flip Public Bot OFF in Discord Developer Portal. Install persists; exposure window closes.

**Why this works**: the installer's bot becomes a first-class participant in the install channel. It can self-report install issues, request help from ${ASSISTANT_NAME}, get drift assessments validated, and execute install-side ops under Sean's principal authority (other participants advise; Sean ratifies).

## Phase 3 — V1 closed-loop iteration (4-6 weeks typical)

**Active period**: the channel is the working forum for tuning the installer's chassis. All four parties (Sean, ${ASSISTANT_NAME}, installer, installer's bot) collaborate here.

**Patterns**:

- **Drift reports** (subtree pulls, plugin updates) → installer's bot computes diff, posts to channel, Sean reviews, ${ASSISTANT_NAME} codifies decisions into chassis upstream
- **Heartbeat smoke-test failures** → installer's bot posts the failing log + last good state, ${ASSISTANT_NAME} debugs, Sean approves the fix
- **Plugin opt-in flows** → installer reacts in `#<installer>-private` to a Phase 2 menu, bot relays opt-in to chassis, ${ASSISTANT_NAME} watches for the heartbeat fire
- **Cross-installer learnings** → patterns discovered in one install's channel get codified in `docs/architectural-anti-patterns.md` so the next install starts further along

**Boundaries**:

- Bot principal authority: only Sean's imperatives count as directives. Installer + installer's bot are first-class collaborators but their imperatives need Sean's ratification before any irreversible action.
- All chassis-repo decisions remain ${ASSISTANT_NAME}'s. Installer's bot advises + observes but never edits `scrollinondubs/behalfbot-chassis`.

## Phase 4 — Retirement OR ongoing support

**Decision point at week 4-6**: installer chooses one of:

**Option A — Full ownership transfer (free, channel retired)**:
- Bot's allowlist updated to remove Sean + JaxBot from `--allow` list (channel still in allowlist for installer-side ops, but ${ASSISTANT_NAME}-server messages no longer trigger response)
- Channel archived in ${ASSISTANT_NAME}'s Discord server
- Sean + ${ASSISTANT_NAME} retain SSH access for emergency-only support, with documented response-time expectations (usually "no response time SLA")
- Installer is on their own for chassis updates (they pull subtree pulls themselves)

**Option B — Paid ongoing support (channel stays live)**:
- Channel stays active
- Sean + ${ASSISTANT_NAME} retain SSH + active monitoring
- Installer pays monthly support fee (TBD pricing; see `lead_ben_lakoff.md` for $200/mo Behalf.bot pricing signal)
- Chassis updates pushed proactively; installer's bot pulls them in via subtree
- Quarterly review of channel activity + value

**Default**: ask the installer at week 4. Don't push either option; let them choose based on their comfort with chassis self-maintenance.

## Doc cross-references

- `docs/installer-homework-<name>.md` — per-installer pre-flight (includes "provide your Discord user ID" step)
- `docs/install-marc-profile.md` (and equivalents) — per-installer install profile
- `docs/architectural-anti-patterns.md` #17 — chassis vendoring hygiene (cited during Phase 3 drift reports)
- `chassis/HEARTBEATS.md.template` — template for installer-rendered heartbeats (post-Phase-2 work happens against `${CHASSIS_HOME}/HEARTBEATS.md`)

## Source

Pattern surfaced from installer-1 install #1 (2026-05-06 through ~2026-05-10 cutover). Sean ratified the codification 2026-05-12 in #<primary> msg 1503744011871981678. First applied to installer-2 install #2 ahead of May 19 demo (chassis is targeted to be containerized by then per project_may19_demo_sequence.md).
