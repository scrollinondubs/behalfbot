# Behalf.bot Install - Pre-flight Homework

> **Read this once.** Hand the rest to your Claude Code instance and let it walk you through.

You're installing Behalf.bot. Sean+${ASSISTANT_NAME} are driving the actual install via SSH (model "b" - see <v1-reference-install> issue #494). Your job is to **provision the accounts + the Linux box + give us SSH access** through Tailscale node sharing. We do everything else.

Total time estimate: **30 to 90 minutes**, mostly waiting for OAuth confirmation emails.

---

## Step 0 - Run the setup wizard (5 min, do this first)

Before you start provisioning accounts, run the setup wizard. It walks you through the decisions that shape your install and pre-fills most of the account setup steps below.

```bash
bash chassis/scripts/setup-wizard/bin/setup-wizard
```

The wizard collects: your installer name, deployment target, primary channel choice, second-brain backend, desired modules. It writes a draft `docs/install-<your-name>-chassis-config.yaml` to your customer branch and generates a pre-flight checklist tailored to your choices.

**If the chassis repo isn't on your machine yet:** Sean+${ASSISTANT_NAME} will run the wizard together with you at the install kickoff call before we SSH in. You can also run it standalone via the hosted Confabulator interview - ask Sean for the link.

Once the wizard completes, the steps below reflect only what's relevant to your specific install. Items the wizard pre-filled are already checked off.

---

## Step 1 - Provision agent-side accounts (separate from your personal accounts)

The dual-identity pattern: all of these are NEW accounts that Behalf.bot will own - not your existing ones. Pre-stage every credential below in your own Bitwarden / 1Password vault (or self-hosted Vaultwarden) so we can pull on install day without fumbling.

### 1a. Google Workspace user

Add a Workspace user under your existing domain - something like `<you>-agent@<your-domain>`. Cost: same per-seat fee as any other Workspace user. This becomes Behalf.bot's email + calendar + drive identity.

After creating:
- Set up 2FA with TOTP (not SMS)
- Generate a Google App Password OR enable OAuth scopes for: Gmail (modify), Calendar (read/write), Drive (read/write)
- Store the credentials in your password manager labeled "Behalf.bot - Google"

> The setup wizard handles most of the OAuth grant flow once we SSH in. What it needs from you: the agent email address and the App Password (or a prepared OAuth client if you prefer the full OAuth path). Manual steps remain below as reference and fallback.

### 1b. GitHub account

New GitHub account, suggested handle: `<you>-agent` or `<you>-bot`. This is the identity Behalf.bot uses for any commits, PRs, repo access on your behalf.

After creating:
- Create a Personal Access Token with scopes: `repo`, `workflow`, `read:org`
- Token never expires (or 1y, your call)
- Store labeled "Behalf.bot - GitHub PAT"

### 1c. Discord - new server

Create a new Discord server for yourself. Suggested name: `<You>'s Behalf.bot` or whatever you want. This is where Behalf.bot's briefings, alerts, and conversation surface land.

Create at least these channels:
- `#<your>-briefings` - daily digest target
- `#<your>-ops` - system alerts, heartbeat failures
- `#<your>-leads` - CRM signals + outreach drafts (if CRM module is enabled)
- `#<your>-social` - dating module surface (if dating module is enabled)
- `#<your>-private` - reserved for sensitive workflows (anything you don't want Behalf.bot writing about)
- **`#<your>-setup`** - shared install-coordination channel. Add both your own Discord account AND Sean+${ASSISTANT_NAME}-bot to this channel. Install work happens here, not via DMs. Pre-creating this channel before homework is done eliminates relay overhead during kickoff.

Create a Discord bot:
- Discord Developer Portal -> New Application -> "Behalf.bot - `<Your Name>`"
- Bot tab -> reset token -> store labeled "Behalf.bot - Discord bot token"
- **Bot tab -> scroll to Privileged Gateway Intents -> toggle ON `MESSAGE CONTENT INTENT` -> Save Changes.** REQUIRED - the chassis's `claude --channels plugin:discord@claude-plugins-official` runtime fails to connect without it. Without the toggle, every DM your bot receives gets dropped silently.
- OAuth2 -> URL Generator -> scopes `bot` + `applications.commands` -> copy the install URL -> use it to add the bot to your server
- Bot permissions needed: Send Messages, Read Messages, Read Message History, Add Reactions, Manage Webhooks, Use Slash Commands

Note your Discord server ID + channel IDs (Discord -> Settings -> Advanced -> Developer Mode -> right-click each channel -> Copy ID).

### 1d. Notion - workspace + integration

Skip if your second-brain backend is SiYuan or Obsidian.

In your Notion workspace (admin):
- Settings & members -> Connections -> Develop or manage integrations -> New integration
- Name: "Behalf.bot - `<Your Name>`"
- Capabilities: Read content, Update content, Insert content, Read user information without email
- Copy the Internal Integration Token -> store labeled "Behalf.bot - Notion integration"
- Share the relevant pages/databases with the integration: at minimum your primary notes area and any CRM database

> **Decide your trust boundary up front.** Most installers want it scoped to specific databases + one memory page, NOT the full Notion workspace. Make that call now: write a list of the specific database UUIDs (right-click a database in Notion -> Copy link -> the 32-char hex string is the UUID) and the memory page ID. Share ONLY those with the integration. Easier to expand later than to explain why it read something sensitive.

> The setup wizard handles the Notion integration token wiring at install time. What it needs from you: the integration token + the database UUIDs and memory page ID you decided above.

### 1e. Telegram bot (optional - only if Telegram module is enabled)

If you want the Telegram-monitor capability:
- Open `@BotFather` on Telegram -> `/newbot` -> name + handle
- Store the bot token labeled "Behalf.bot - Telegram bot"
- Send the bot a `/start` from your personal Telegram so it can DM you
- Identify the chat IDs of the 2-3 high-signal chats you want it to monitor (we'll wire those at install)

If you'd rather defer Telegram until post-install, fine - Discord-only is a perfectly valid V1.

### 1f. (Recommended) Self-hosted Vaultwarden

**Note the URL + port we'll need:** when you stand up Vaultwarden, capture the EXACT URL including port (e.g. `http://your-server:8222` for the default Vaultwarden Docker image). We'll verify reachability with `curl` before any credential pull.

**Note your VW master account email separately from your agent identity email.** These are different things and a common assumption mismatch. Burning retries on the wrong email hits rate-limit (HTTP 429) on the prelogin endpoint.

Stand up a Vaultwarden instance on your Linux box for Behalf.bot's secrets. Keeps the agent's credentials separate from your personal Bitwarden.

Trivial: `docker run -d --name vaultwarden -v ~/vaultwarden-data:/data -p 8222:80 vaultwarden/server:latest`. Reverse-proxy via Caddy or your existing nginx with a TLS cert.

If you'd rather skip this for V1 and use your existing Bitwarden, also fine. We'll wire whatever you have.

**Canonical VW item names:** The chassis hydration script looks up items by exact name. Use `docs/installer-vw-template.md` as your checklist - it lists the exact item name, which field to put the value in, and what ends up in `.env`. Copy-paste the item names; typos silently skip during hydration.

**CLI note:** the official `bw` CLI enforces HTTPS and will refuse to connect to a plain-HTTP Vaultwarden instance. Chassis uses `rbw` (Rust-based, supports HTTP self-hosted VW) for unattended secret pull. We install `rbw` at bootstrap time.

### 1g. Plugin-specific API keys

Based on which modules you enabled in the setup wizard (or in `docs/install-<your-name>-chassis-config.yaml`), provision and stash these:

**BFL plugin (Body for Life - only if you enabled bfl module):**
- **FDC API key (optional - vision-primary is the default).** The chassis BFL pipeline uses Claude vision as the primary macro source. FDC (USDA FoodData Central) was the original enrichment path but has been systematically under-reporting macros vs vision (cooked vs raw ingredient mismatch, portion unit confusion - <v1-reference-install>#513). The key still lives in VW for a future hybrid path, but NOT required at install. If you do want it: free, instant at [api.nal.usda.gov/fdc/](https://api.nal.usda.gov/fdc/). Store labeled "Behalf.bot - FDC API key".
- **Strava API access (opt-in - only if you GPS-track aerobic workouts).** Do you log GPS-tracked running or cycling and want it overlaid on your BFL log? If no (gym-only, no GPS activity): skip Strava entirely - `strava_oura_reconcile: false` in your chassis config is the default. If yes: create a personal API app at [developers.strava.com](https://developers.strava.com/). Note Client ID + Client Secret. Store labeled "Behalf.bot - Strava OAuth".
- **Oura API key** (if you have an Oura ring). Get a personal access token at [cloud.ouraring.com](https://cloud.ouraring.com/personal-access-tokens). Store labeled "Behalf.bot - Oura PAT".

**Dating plugin (only if you enabled dating module):**
- The dating plugin runs phone-side automation against the dating apps you choose. We'll handle the Android emulator + ADB / GPS-spoofing setup at install time. **You don't need to do anything pre-install** other than confirm which apps you want enabled.
- **Photo-verification backends** (safety floor for catfish detection): TinEye and Google Lens are browser-driven (no key needed). PimEyes has a paid tier ($30/mo basic) for a third triangulation source if you want it.

**Ops watchdog (chassis-core, always on):**
- A Discord webhook URL for your `#<your>-ops` channel. Discord channel settings -> Integrations -> Webhooks -> New Webhook -> "Behalf.bot Ops" -> copy URL. Store labeled "Behalf.bot - Discord ops webhook".

**Modules NOT enabled (you can flip these on later - no homework needed now):**
- `angel-protocol` (personal-safety + duress cascade) - disabled by default, deliberate opt-in
- `dealflow`, `health_general`, `banking`, `password_management` - V2+ candidates
- `whatsapp` - disabled by default

---

## Step 2 - Linux box setup

### 2a. OS

Ubuntu 22.04 LTS or 24.04 LTS recommended. Debian 12 also fine. If you're on something more exotic (Alpine, NixOS) - flag it to us before install.

### 2b. Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Confirm the node appears in your Tailscale admin panel at https://login.tailscale.com/admin/machines. Note the node name - typically `<hostname>.<your-tailnet>.ts.net`.

### 2c. Tailscale node share to Sean+${ASSISTANT_NAME}

**Don't invite us to your tailnet.** Use Tailscale's built-in `Share` primitive - your tailnet stays separate from Sean+${ASSISTANT_NAME}'s; we just get scoped access to this one node.

In the Tailscale admin panel:
- Click your Linux box's row
- Click `...` (the three-dot menu) -> **Share**
- Enter Sean's Tailscale identity email: `sean@grid7.com` and ${ASSISTANT_NAME}'s identity if separate (we'll confirm pre-install)
- Click Generate share link -> copy the link -> DM it to Sean

Reference: https://tailscale.com/kb/1084/sharing.

### 2d. SSH key handoff

Add our SSH public keys to your box:

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
# Sean will send the public keys via Discord; paste them into:
nano ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

**Confirm the username we should connect as.** Run on the box: `whoami`. Whatever it prints is the user we SSH in as - NOT necessarily your Tailscale identity email's prefix. This is a common gotcha: verify the actual Linux user and share it with Sean before install day.

### 2e. Verify everything before pinging Sean

After completing Steps 2b-2d, run the pre-kickoff sanity check script:

```bash
bash chassis/scripts/verify-tailscale.sh
```

Expected output when ready: `Tailscale verify: 7/7 checks passed - all green.`

Any red items include fix instructions. Fix them and re-run until all 7 are green. Once green, ping Sean to book the install kickoff.

### 2f. (We'll handle these after we're in)

Don't sweat installing these yourself - leave them to us:
- Python 3.12+
- Node 20+
- **Bun** (required for the Claude Code Discord channels plugin)
- **tmux** (required to host the long-running channels-plugin session that survives SSH disconnects)
- ffmpeg, sqlite3, jq, curl
- Docker (needed if you want the Vaultwarden container or are on the containerized install path)
- Ollama (if local-models module is on)
- Claude Code on the Linux box itself
  - **Auth verification step:** after `claude login` finishes, we run `claude auth status` and confirm the email shown is YOURS, not a Sean+${ASSISTANT_NAME} driver email. The `subscriptionType: max` check alone isn't enough - any Max account passes that, including the wrong one.

---

## Step 3 - Hand off

When steps 1 + 2 are done, drop a message in `#<your>-setup` with:
1. Confirmation that all agent-side accounts are provisioned + credentials are in your password manager
2. The Tailscale share link
3. Your SSH-able hostname and the Linux user name we should SSH in as
4. **Your `about-me.md` and `my-company.md` if you have them.** Short documents you've written describing how you work, your company context, communication preferences, hates list. Authored-by-you docs seed memory with far higher signal than interview reversal. 1-3 pages each is ideal.

Don't paste any credentials in Discord. Sean+${ASSISTANT_NAME} will pull from your password manager during install.

---

## What happens next

We SSH in. We log every command we run into a `bootstrap.sh` so the next installer can run a near-identical transcript. We hydrate your chassis from the profile + config we drafted from your interview (you'll review + ratify both before we touch anything irreversible).

First success criterion: a daily-briefing message lands in your `#<your>-briefings` Discord channel for 3 consecutive mornings. Once that posts cleanly for 3 days, the install is signed off.

After that you have a choice: (a) we transfer ownership + revoke our SSH access, or (b) we stay on as DevOps shadow through the V1 iteration period (~4-6 weeks).

### Security hygiene during install + iteration

- **Don't `cat .mcp.json` or `cat .env` in Claude Code transcripts.** Those files contain live secrets. Inspect specific keys via `python3 -c 'import json; print(list(json.load(open(".mcp.json"))["mcpServers"].keys()))'` instead of dumping the whole file.
- **Plaintext Discord shares of secrets get rotated post-install.** During install you'll occasionally paste credentials in Discord for expediency. We capture them in your VW + then rotate within a week of signoff.
- **Master VW password stays known to Sean+${ASSISTANT_NAME} until V1 transfer signoff (~4-6 weeks).** You can rotate any time after we hand off.

---

## Quick checklist (tear-off)

- [ ] Google Workspace user provisioned + 2FA + OAuth scopes (OR setup wizard pre-filled this)
- [ ] GitHub bot account + PAT
- [ ] Discord server + bot + channels created + bot installed + **Message Content Intent ON** + Developer Mode on for IDs
- [ ] Second-brain integration token + relevant databases shared (Notion) or notebook path confirmed (SiYuan/Obsidian)
- [ ] (Optional) Telegram bot via @BotFather
- [ ] (Optional) Vaultwarden instance up
- [ ] BFL-specific keys (FDC optional, Strava if GPS aerobic, Oura if on Oura ring) - only if bfl module enabled
- [ ] Discord ops webhook for `#<your>-ops`
- [ ] Linux box: Ubuntu/Debian + Tailscale up
- [ ] Tailscale node shared to Sean+${ASSISTANT_NAME} (NOT your tailnet - the node share)
- [ ] Our SSH public keys appended to `~/.ssh/authorized_keys`
- [ ] Linux user name confirmed and sent to Sean
- [ ] All credentials stashed in your password manager

When this list is fully ticked, ping Sean in `#<your>-setup` - we'll book the install kickoff.

---

*Template derived from V1 install experience. Per-installer versions live on customer/<name> branches. Source-of-truth issue: [<v1-reference-install> #494](<v1-reference-install>#494).*
