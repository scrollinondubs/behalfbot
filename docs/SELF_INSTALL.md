# Self-install guide

This is the do-it-yourself path. You'll clone the chassis, fill in two config files, populate `.env`, run `bootstrap.sh`, and fire your first heartbeat. Time-to-delight: a few hours, mostly waiting on OAuth confirmation emails.

If you'd rather have a wizard generate the config files for you (or have a maintainer drive the steps over SSH), use one of the other two paths at https://behalf.bot. Both produce the same artifacts as this guide.

---

## Prerequisites

Before you start:

- **Linux or macOS host** with Docker + Compose plugin installed
- **Claude Code CLI** installed and logged in (`claude --version` should print a version)
- **A second-brain backend account** (Notion, SiYuan, or Obsidian) - you'll point chassis at it later
- **Discord, Telegram, or Slack workspace, or a Matrix homeserver** where the chassis can talk to you
- **A bot identity** distinct from your personal accounts:
  - GitHub user (for the agent's commits, separate from yours)
  - Google Workspace user (for Gmail / Calendar read+write, separate from yours)
  - Discord bot (with MESSAGE CONTENT INTENT enabled)
- **API keys** for the third-party services you'll use (Anthropic, OpenAI, Notion, etc.)

The bot-identity pattern matters - the agent acts on your behalf using accounts you own but that are NOT your personal Apple ID / GitHub / Google account. This is the trust boundary.

---

## Step 1 - Scaffold your install repo

The chassis is a vendored dependency, not a fork. You create your own private repo and vendor the chassis at `chassis/` via git subtree.

```bash
# Create your install repo (private)
gh repo create <your-namespace>/<install-name> --private \
    --description "<install-name>'s Behalf.bot install"

# Clone it
git clone https://github.com/<your-namespace>/<install-name>.git
cd <install-name>

# Vendor the chassis
git subtree add --prefix=chassis \
    https://github.com/scrollinondubs/behalfbot.git main --squash

# Initial commit
git add -A
git commit -m "bootstrap: vendor chassis @ main"
git push -u origin main
```

You now have a `chassis/` directory inside your repo with everything the chassis ships. Treat it as read-only. Future upstream pulls land via `git subtree pull --prefix=chassis ...`.

---

## Step 2 - Fill in the install profile

Copy the two sample templates to your install root and fill them in:

```bash
cp chassis/INSTALL_PROFILE.md ./install-profile.md
cp chassis/chassis.config.yaml ./chassis.config.yaml
```

Edit both with your identity, target environment, channel choice, modules to enable, etc. The placeholders are angle-bracketed (`<your-name>`, `<city>`, etc.); each one has an example value in the comment next to it.

If you'd rather not author these by hand, the interview wizard at https://behalf.bot generates both files for you from a guided conversation. Drop the generated files into your install root and skip this step.

---

## Step 3 - Populate `.env`

The chassis reads runtime secrets from `.env` (gitignored). Three approaches, pick one:

| Approach | When to use |
|---|---|
| **Plain `.env` file** | Simplest. Edit `.env` directly; restart chassis when it changes. |
| **Vaultwarden hydration** | If you already run a Vaultwarden instance, `chassis/scripts/hydrate-env-from-vw.sh` pulls items by name and writes `.env`. See `docs/credential-bake.md`. |
| **Your existing secret manager** | 1Password, AWS Secrets Manager, etc. Write your own `hydrate-env.sh` that produces a `.env` file. |

Whatever path you choose, the resulting `.env` should contain (at minimum):

- `ANTHROPIC_API_KEY` (or rely on Claude Code's OAuth login)
- `OPENAI_API_KEY`
- `GITHUB_PAT` (for the agent's bot account)
- Google OAuth credentials (`GOOGLE_OAUTH_*` set)
- `DISCORD_BOT_TOKEN` (or equivalent for Telegram / Slack; a Matrix bot's token goes into the plugin's own `.env` instead, see [Matrix as a chat surface](#matrix-as-a-chat-surface))
- Per-plugin keys for the modules you enabled in `chassis.config.yaml`

See `docs/hydration.md` for the full env-var map.

---

## Step 4 - Run `bootstrap.sh`

```bash
export CHASSIS_HOME=$(pwd)
bash chassis/bootstrap.sh
```

`bootstrap.sh` walks through:

1. Environment + tool prerequisite validation
2. Hydrate `.env` if it doesn't exist (interactive prompts)
3. Validate `INSTALL_PROFILE.md` + `chassis.config.yaml`
4. Render `.mcp.json` from template + `.env`
5. Render `CLAUDE.md` from template + `INSTALL_PROFILE.md`
6. Initialize `HEARTBEATS.md` from chassis-defaults + plugin-registered heartbeats
7. Activate enabled plugins per `chassis.config.yaml.modules`
8. Seed memory entries from `INSTALL_PROFILE.md` (model: `chassis/memory/MEMORY_MODEL.md`; an install from before 2026-10 migrates with `docs/memory-architecture-migration.md`)
9. Install OS-level dependencies
10. Set up the dispatcher unit (launchd on macOS, systemd on Linux)
11. Run plugin smoke tests
12. Report status

It's idempotent. Re-run after a partial install and it picks up where it left off. Every command run gets logged to `${CHASSIS_HOME}/logs/bootstrap-<date>.log`.

---

## Step 5 - First heartbeat

After `bootstrap.sh` completes successfully, the dispatcher unit fires every 15 minutes. The first morning briefing should land in your `<install>-briefings` Discord channel at your configured briefing time the next morning.

Success criterion: **3 consecutive clean morning briefings**. If you hit that, your install is healthy.

If something fails, check:

```bash
tail -f "${CHASSIS_HOME}/logs/dispatcher-$(date +%Y-%m-%d).log"
```

---

## What you have now

After Step 5 completes:

- A persistent agent that fires daily briefings to your channel
- A heartbeat dispatcher that can run any number of scheduled "gather-first" tasks
- A plugin system - flip flags in `chassis.config.yaml` to enable BFL, dating, restaurant-booking, etc.
- Memory continuity across sessions (`~/.claude/projects/<install>/memory/`)
- Discord/Telegram/Slack/Matrix intake for ad-hoc tasks
- A dispatcher that lives in a container, runs under launchd/systemd, and survives reboots

---

## Matrix as a chat surface

The chassis ships a Claude Code channel plugin for Matrix at
[`claude-plugins/matrix-channel/`](../claude-plugins/matrix-channel/README.md).
It gives the session the same tools as the Discord plugin (`reply`, `react`,
`edit_message`, `fetch_messages`, `download_attachment`) over any homeserver.
Set `surfaces.primary: matrix` (or list it as a secondary) in
`chassis.config.yaml` and `bootstrap.sh` prints these steps for you.

1. **Bot account.** Register a dedicated Matrix user for the agent and get its
   access token (the plugin README shows the login call). Keep the token in
   your password manager.
2. **Allow the channel.** `matrix` is not on Anthropic's default channel
   allowlist. In `managed-settings.json` on the host that runs the
   `claude --channels` session (macOS: `/Library/Application Support/ClaudeCode/`,
   Linux: `/etc/claude-code/`) set `"channelsEnabled": true` and an
   `allowedChannelPlugins` list that names
   `{"marketplace": "behalfbot", "plugin": "matrix"}`. That list replaces
   the default one, so also name
   `{"marketplace": "claude-plugins-official", "plugin": "discord"}` if you
   keep Discord.
3. **Install the plugin.**
   ```
   claude plugin marketplace add scrollinondubs/behalfbot
   claude plugin install matrix@behalfbot
   ```
4. **Configure it** from a Claude Code session started in your install directory:
   ```
   /matrix:configure MATRIX_HOMESERVER_URL=https://matrix.example.org MATRIX_BOT_USER_ID=@yourbot:example.org MATRIX_ACCESS_TOKEN=<token>
   /matrix:access add @you:example.org
   ```
   The token lands in `~/.claude/channels/matrix/.env` (mode 600), not in the
   install's `.env`.
5. **Rooms.** Create a room from your own account, leave encryption off, and
   invite the bot; it auto-joins invites from allowlisted users. The plugin
   skips encrypted events, so an encrypted room reads as silence.
6. **Start the session with it.** Re-render the restart script with
   `CLAUDE_CHANNELS` set in the environment, space-separated when you run
   more than one (the renderer reads it from the environment, not from `.env`):
   ```
   CLAUDE_CHANNELS="plugin:discord@claude-plugins-official plugin:matrix@behalfbot" \
   CUSTOMER_HOME=~/.behalfbot CHASSIS_HOME=~/behalfbot BOT_NAME=<bot> \
       bash chassis/scripts/bootstrap-customer-scripts.sh
   ```

Matrix carries the conversation only. There is no `post-to-matrix.sh` yet, so
briefings and ops alerts still go out through Discord, Telegram or Slack, and
`principal-policy-hook.sh` does not yet recognise Matrix messages.

## Going further

- **Plugin docs:** `docs/plugins/` (per-plugin overrides + skill files)
- **Architecture:** `docs/architectural-anti-patterns.md` (the dos and don'ts the chassis was built on)
- **Lessons:** `docs/LESSONS_FROM_V1.md` (30+ lessons from the V1 reference install)
- **OpenClaw plugins:** the chassis is OpenClaw-compatible - any plugin from https://clawhub.ai drops in cleanly

If you get stuck, the managed-install option at https://behalf.bot has a maintainer drive the steps over SSH. Hand them your install repo, they finish the bootstrap, you take ownership at signoff.

---

## Migrating an existing install to a re-anchored chassis

If you already have a running install and need to re-anchor the chassis subtree (e.g. the upstream chassis repo was renamed or moved), the canonical sequence is:

```bash
# 1. Tar up the old clone as a complete backup.
tar czf ~/install-backup-$(date +%Y%m%d).tgz -C $HOME <your-install-name>

# 2. Move the old clone aside (do NOT delete it yet - the restore step needs it).
mv ~/<your-install-name> ~/<your-install-name>.bak

# 3. Re-clone your install repo (the one that vendors the chassis subtree).
git clone https://github.com/<your-namespace>/<your-install-name>.git
cd ~/<your-install-name>

# 4. Re-add the chassis subtree at the new remote URL if it changed.
#    Skip this step if only your install repo moved and the chassis remote is the same.
git subtree pull --prefix=chassis https://github.com/scrollinondubs/behalfbot.git main --squash

# 5. Restore customer files from the backup using the helper script. It uses
#    an INVERSE allowlist so anything the new chassis subtree provides is
#    preserved, and everything else (.env, .mcp.json, scheduled-tasks/,
#    skills/, plugins/, scripts/, data/, state/, briefings/, memory/, etc.)
#    is restored from the backup. Always start with --dry-run.
bash chassis/scripts/migrate-from-old-clone.sh \
    --backup-dir ~/<your-install-name>.bak \
    --target-dir ~/<your-install-name> \
    --dry-run

# Review the dry-run output, then run for real:
bash chassis/scripts/migrate-from-old-clone.sh \
    --backup-dir ~/<your-install-name>.bak \
    --target-dir ~/<your-install-name>

# 6. Re-run bootstrap.sh so customer-side scripts (launchd plists, watchdogs)
#    pick up the new chassis paths.
CHASSIS_HOME=$(pwd) bash bootstrap.sh

# 7. Smoke-test the dispatcher in dry-run before re-enabling the launchd
#    unit:
DRY_RUN=true bash chassis/scheduled-tasks/heartbeat-dispatcher.sh
```

Once the first dispatcher tick after re-enable lands cleanly in your ops channel, delete the `.bak` backup. The migration script is idempotent - re-running it after a partial restore is safe.

For the catastrophic hardware-loss scenario (Mac mini dies, laptop stolen, cloud VM terminated) rather than an in-place chassis re-anchor, see [`disaster-recovery.md`](disaster-recovery.md). It walks through rebuilding from your install repo + chassis remote + secret store + postgres backup on a fresh machine.

---

## Discord channel access + `requireMention` toggle

The Discord plugin at `~/.claude/channels/discord/access.json` controls which channels the bot reads from and whether it ignores messages that do not @-tag it. The chassis populates this file automatically during `bootstrap.sh` via `chassis/scripts/bootstrap-discord-access.sh` using the `DISCORD_*_CHANNEL_ID` env vars + `INSTALLER_DISCORD_USER_ID`. Default per-channel behavior:

| Channel key (env var) | `requireMention` |
|---|---|
| `DISCORD_PRIMARY_CHANNEL_ID` | `false` - natural conversation, no @-tag needed |
| `DISCORD_ALERTS_CHANNEL_ID` | `true` |
| `DISCORD_OPS_CHANNEL_ID` | `true` |
| `DISCORD_BRIEFINGS_CHANNEL_ID` | `true` |
| `DISCORD_LEADS_CHANNEL_ID` | `true` |
| `DISCORD_SOCIAL_CHANNEL_ID` | `true` |

The primary channel intentionally defaults to `requireMention: false` because that is where the installer expects to talk to the bot conversationally. Every other channel defaults to `true` so the bot stays quiet unless explicitly addressed - the safer choice for shared, multi-participant channels.

If you want to flip a channel between the two modes after bootstrap, edit `~/.claude/channels/discord/access.json` directly or use the plugin's slash command from a running Claude Code session:

```
/discord:access group add <channel_id> --no-mention --allow <user_id>   # requireMention=false
/discord:access group add <channel_id> --mention    --allow <user_id>   # requireMention=true
```

After editing, restart any long-running `claude --channels` tmux session so it picks up the new state.

---

## macOS host prereq: Full Disk Access

**macOS installs only.** Linux + Windows installs can skip this section.

The chassis dispatcher runs inside a long-lived `tmux` session and reads from `$CUSTOMER_HOME` (under your home dir) on every tick. Recent macOS releases prompt for Full Disk Access the first time a binary in a tmux pane reads from a path the system considers protected. The prompt fires on every fresh tmux session, which can interrupt the install flow into a permission-popup loop.

Fix it once, before you start the install:

1. Open **System Settings -> Privacy & Security -> Full Disk Access**.
2. Click the **+** button and grant Full Disk Access to:
   - **Terminal.app** (or iTerm2 / Ghostty / whichever terminal you actually use)
   - **tmux** - browse to the binary directly. Homebrew on Apple Silicon ships it at `/opt/homebrew/bin/tmux`; on Intel Macs it's `/usr/local/bin/tmux`. Use `which tmux` to confirm the path on your host.
3. Restart the terminal app so the change takes effect.

Granting Full Disk Access to your terminal + tmux suppresses the popup loop for the entire install flow. Without these grants, the install still proceeds but the popups appear every time `bootstrap.sh`, `tmux`, or the dispatcher reads a config file under `~/.behalfbot/` or `~/.claude/`.

This is a macOS-only prereq. The chassis itself does not require Full Disk Access on any other OS.
