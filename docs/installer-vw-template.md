# Behalf.bot - Canonical Vaultwarden Item-Name Template

> **Contract between installer and chassis.** The names in this document are the exact item names the chassis hydration script (`chassis/scripts/hydrate-env-from-vw.sh`) looks up via `rbw`. Rename an item in VW after install and hydration breaks silently - it treats the missing item as a partial hydration (exit code 3) and writes no value for that env var.

This template is installer-agnostic. Per-installer docs (e.g. `docs/installer-homework-marc.md`) reference this file for the item list and only describe installer-specific values or opt-ins.

The downstream Playwright wizard (issue #29) will write to these exact item names. That is the reason the names are locked here as a canonical contract rather than left to per-install convention.

---

## How to use this

1. Open your Vaultwarden instance in a browser.
2. Work through each section below. Create one VW item per row.
3. Use the exact item name from the **Item name** column - copy-paste is safest.
4. Put the value in the field specified under **VW field** (password / username / notes / custom field).
5. Skip optional items you're not using - the hydration script treats missing items as warnings, not fatal errors.

---

## Conventions

- **Item name** - the label you see in the VW web UI. Must be exact - the chassis does a string match.
- **VW field** - where the value lives: `password` (the main credential field), `username`, `notes` (multiline), or a custom field name (you add this under "Custom Fields" in the VW item editor).
- **Env var(s)** - what ends up in `.env` after hydration. Listed for cross-reference; the installer never sets these directly.
- **Format / example** - what the value looks like. Placeholders use `<angle-bracket>` style.
- **Required** - `yes` = chassis won't start without it; `plugin:<name>` = only required when that plugin is enabled; `optional` = nice-to-have, graceful degradation if absent.

---

## Section 1 - Always-on chassis core

These items are required for every install regardless of which plugins are enabled.

### Identity + runtime

| Item name | VW field | Env var(s) | Format / example | Required |
|---|---|---|---|---|
| `Behalf.bot - instance name` | `password` | `INSTANCE_NAME` | Short uppercase label, no spaces. `OZZY` or `MARC`. Used as Discord display name + webhook-name prefix. | yes |
| `Behalf.bot - installer name` | `password` | `INSTALLER_NAME` | Full name as the agent should use it in messages. `Alex Smith` | yes |

### Second brain

One of the following depending on which second-brain backend the installer chose in the onboarding interview.

| Item name | VW field | Env var(s) | Format / example | Required |
|---|---|---|---|---|
| `Behalf.bot - Notion integration token` | `password` | `NOTION_INTEGRATION_TOKEN` | Starts `ntn_` or `secret_`. From Notion → Settings → Integrations. | plugin:notion |
| `Behalf.bot - Notion DB IDs` | `notes` (multi-line) | `NOTION_PROJECT_TRACKER_DB_ID`, `NOTION_READING_LIST_DB_ID`, `NOTION_MEMORY_PAGE_ID` | One `KEY=value` per line. Example: `NOTION_PROJECT_TRACKER_DB_ID=<32-hex-uuid>`. Parsed by hydrate-env-from-vw.sh as a dotenv block. | plugin:notion |

> SiYuan and Obsidian backends are configured via file path / MCP config rather than VW items - no VW entry needed for those.

### Google identity (agent-side)

| Item name | VW field | Env var(s) | Format / example | Required |
|---|---|---|---|---|
| `Behalf.bot - Google Workspace agent` | `username` = agent email, `password` = App Password | `GOOGLE_AGENT_EMAIL`, `GOOGLE_AGENT_APP_PASSWORD` | Email: `<you>-agent@<your-domain>`. App Password: 16-char string from Google Account → Security → App Passwords. NOT the login password. | yes |
| `Behalf.bot - Google OAuth client` | `username` = CLIENT_ID, `password` = CLIENT_SECRET | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | From Google Cloud Console → APIs & Services → Credentials → OAuth 2.0 Client ID. Client ID ends in `.apps.googleusercontent.com`. | yes |

### GitHub (agent-side)

| Item name | VW field | Env var(s) | Format / example | Required |
|---|---|---|---|---|
| `Behalf.bot - GitHub PAT` | `password` | `GITHUB_PAT` | Personal Access Token with scopes: `repo`, `workflow`, `read:org`. Starts `ghp_` or `github_pat_`. | yes |

### Communication channels

The chassis routes output through Discord webhooks. Each webhook URL is the full URL from Discord channel settings → Integrations → Webhooks.

| Item name | VW field | Env var(s) | Format / example | Required |
|---|---|---|---|---|
| `Behalf.bot - Discord bot token` | `password` | `DISCORD_BOT_TOKEN` | From Discord Developer Portal → Bot → Reset Token. Long alphanumeric string. | yes |
| `Behalf.bot - Discord briefings webhook` | `password` | `BRIEFINGS_WEBHOOK_URL` | `https://discord.com/api/webhooks/<id>/<token>` | yes |
| `Behalf.bot - Discord ops webhook` | `password` | `OPS_WEBHOOK_URL` | `https://discord.com/api/webhooks/<id>/<token>` | yes |
| `Behalf.bot - Discord leads webhook` | `password` | `LEADS_WEBHOOK_URL` | `https://discord.com/api/webhooks/<id>/<token>` | plugin:crm |
| `Behalf.bot - Discord social webhook` | `password` | `SOCIAL_WEBHOOK_URL` | `https://discord.com/api/webhooks/<id>/<token>` | plugin:dating |
| `Behalf.bot - Discord installer user_id` | `password` | `INSTALLER_DISCORD_USER_ID` | Installer's numeric user ID. Enable Developer Mode (Discord Settings → Advanced), then right-click your username → Copy User ID. `<18-digit-number>` | yes |

### AI / TTS

| Item name | VW field | Env var(s) | Format / example | Required |
|---|---|---|---|---|
| `Behalf.bot - chassis OpenAI key` | `password` | `OPENAI_API_KEY` | Starts `sk-`. Used ONLY for TTS via `text-to-speech.sh`. **Do NOT add `ANTHROPIC_API_KEY` to VW or `.env` if the installer is on a Claude subscription** - the dispatcher relies on OAuth billing when this var is absent. Exporting an Anthropic API key causes the dispatcher to charge API credits instead of subscription, which can be expensive. See `chassis/scheduled-tasks/heartbeat-dispatcher.sh` for the guard. | yes |

### Infrastructure

| Item name | VW field | Env var(s) | Format / example | Required |
|---|---|---|---|---|
| `Behalf.bot - Postgres password` | `password` | `POSTGRES_PASSWORD` | Generated at install time: `openssl rand -base64 32`. Also stored here so the installer has a recovery copy. | yes |
| `Behalf.bot - Vaultwarden API token` | `password` | `RBW_VW_API_TOKEN` | Generated by VW web UI → Settings → API Key after first login. Read-only scope. Replaces the master-password-based pull after first install. | yes |
| `Behalf.bot - Tailscale auth key` | `password` | `TAILSCALE_AUTHKEY` | Reusable 1-year key from Tailscale admin → Settings → Keys → Auth Keys. Only needed if the chassis container itself joins the tailnet (not required in V1 - the host's Tailscale is sufficient). | optional |

---

## Section 2 - Telegram (primary channel installs)

Required when Telegram is the installer's primary surface (`chassis.config.yaml` surface: `telegram`).

| Item name | VW field | Env var(s) | Format / example | Required |
|---|---|---|---|---|
| `Behalf.bot - Telegram bot token` | `password` | `TELEGRAM_BOT_TOKEN` | From @BotFather → /newbot. Format: `<digits>:AAEE<rest-of-token>` | plugin:telegram |
| `Behalf.bot - Telegram installer user_id` | `password` | `INSTALLER_TELEGRAM_USER_ID` | Installer's numeric Telegram user ID. Get from @userinfobot. `<9-or-10-digit-number>` | plugin:telegram |

---

## Section 3 - Slack (secondary channel installs)

Required when Slack is the installer's secondary surface.

| Item name | VW field | Env var(s) | Format / example | Required |
|---|---|---|---|---|
| `Behalf.bot - Slack bot token` | `password` | `SLACK_BOT_TOKEN` | Starts `xoxb-`. From Slack App → OAuth & Permissions → Bot User OAuth Token. | plugin:slack |
| `Behalf.bot - Slack workspace_id` | `password` | `SLACK_WORKSPACE_ID` | Team ID. Starts `T`. From Slack workspace settings or API calls. `T0XXXXXXX` | plugin:slack |
| `Behalf.bot - Slack installer user_id` | `password` | `INSTALLER_SLACK_USER_ID` | Installer's user ID in their workspace. Starts `U`. `U0XXXXXXX` | plugin:slack |

---

## Section 4 - Banking (card-scope only)

Installer-optional. Enables the chassis to monitor pre-loaded card transactions and book appointments on the installer's behalf. Not full bank access - card-scope only.

| Item name | VW field | Env var(s) | Format / example | Required |
|---|---|---|---|---|
| `Behalf.bot - digital debit card` | `notes` (multi-line) | `BANKING_CARD_PROVIDER`, `BANKING_CARD_NUMBER`, `BANKING_CARD_CVV`, `BANKING_CARD_EXPIRY`, `BANKING_CARD_DAILY_CAP_EUR` | One `KEY=value` per line. Example: `BANKING_CARD_PROVIDER=Mercury`. Hydrated as a dotenv block. | plugin:banking |
| `Behalf.bot - card API credentials` | `username` = API key or client_id, `password` = API secret or bearer token | `BANKING_API_TOKEN`, `BANKING_API_SECRET` | Provider-specific. Mercury uses a bearer token (username field unused). Revolut/Wise use client_id + client_secret. | plugin:banking |

> Note: `BANKING_API_SECRET` is not currently in the hydration script's DEFAULT_MANIFEST - the `card API credentials` item only maps the `password` field to `BANKING_API_TOKEN`. If the installer's card provider needs a second credential (e.g. Revolut client_id), set `username` as a custom VW item and update `vw-items.json` accordingly.

---

## Section 5 - Plugin: BFL (Body for Life)

Required when `chassis.config.yaml modules.bfl.enabled: true`. Provision during the installer homework phase; populate in VW before install day.

| Item name | VW field | Env var(s) | Format / example | Required |
|---|---|---|---|---|
| `Behalf.bot - FDC API key` | `password` | `USDA_FDC_API_KEY` | Free from api.nal.usda.gov/fdc → Sign Up. `DEMO_KEY` works at low volume. | plugin:bfl |
| `Behalf.bot - Strava OAuth` | `username` = client_id, `password` = client_secret | `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET` | From developers.strava.com → My API Application. Full OAuth dance happens at install time with Sean+${ASSISTANT_NAME}. | plugin:bfl (if strava_oura_reconcile enabled) |
| `Behalf.bot - Oura PAT` | `password` | `OURA_PERSONAL_ACCESS_TOKEN` | From cloud.ouraring.com → Personal Access Tokens. Skip if installer is not on Oura. | plugin:bfl (if on Oura) |

---

## Section 6 - Plugin: Admin (Twilio + restaurant/appointments)

Required when `chassis.config.yaml modules.admin.enabled: true`.

| Item name | VW field | Env var(s) | Format / example | Required |
|---|---|---|---|---|
| `Behalf.bot - Twilio account` | `username` = ACCOUNT_SID, `password` = AUTH_TOKEN | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | From console.twilio.com → Account Info. SID starts `AC`. | plugin:admin |
| `Behalf.bot - Twilio phone number` | `password` | `TWILIO_PHONE_NUMBER` | E.164 format. `+15551234567`. Outbound caller ID for voice calls. | plugin:admin |
| `Behalf.bot - OpenTable credentials` | `username` = email, `password` = password | `OPENTABLE_USERNAME`, `OPENTABLE_PASSWORD` | Installer's OpenTable (or The Fork) account if using web-automation route over phone-call route. Optional - phone-call route is the V1 default. | plugin:admin (web-automation path only) |

---

## Section 7 - Plugin: Angel Protocol

Required when `chassis.config.yaml modules.angel_protocol.enabled: true`. Deliberate opt-in plugin - personal safety system.

| Item name | VW field | Env var(s) | Format / example | Required |
|---|---|---|---|---|
| `Behalf.bot - Angel webhook URL` | `password` | `ANGEL_WEBHOOK_URL` | Discord webhook URL for silent safety-cascade ops channel. Must be a channel the installer monitors. | plugin:angel-protocol |

> Twilio credentials for the Angel Protocol SMS cascade reuse `Behalf.bot - Twilio account` and `Behalf.bot - Twilio phone number` from Section 6. If Admin plugin is NOT enabled, provision those two items anyway for Angel Protocol alone.

> The duress codeword and emergency-contact list are NOT stored in VW - they're set at install time into gitignored local files (`chassis.config.yaml duress_codeword` and `plugins/angel-protocol/data/emergency-contacts.json`). Storing them in VW would expose them to anyone who gets VW read access.

---

## Section 8 - Plugin: Dating

Required when `chassis.config.yaml modules.dating.enabled: true`. The dating plugin has no credentials of its own in VW - it reuses `SOCIAL_WEBHOOK_URL` from Section 1 (already required when dating is enabled). No additional VW items needed.

The Android emulator + GPS-spoofing setup is handled by Sean+${ASSISTANT_NAME} at install time; the installer has no pre-install homework for this plugin beyond confirming which apps to enable.

---

## What is NOT stored in VW

- **Master password** - installer keeps in their own personal password manager. VW is unlocked by the master password; it cannot store itself.
- **SSH private keys** - installer keeps locally. Only public keys go to the agent box.
- **Duress codeword** - stored in `chassis.config.yaml` locally, never in VW (see Section 7 note).
- **Emergency contacts** - stored in `plugins/angel-protocol/data/emergency-contacts.json` (gitignored), never in VW.
- **Tailscale OAuth credentials** - installer uses their own Tailscale account from their browser; chassis only needs the auth key (Section 1).
- **`ANTHROPIC_API_KEY`** - explicitly never stored here. See the OpenAI key note in Section 1.
- **Discord channel IDs** (briefings, ops, leads, social, health) - these come from `chassis.config.yaml surfaces.channel_topology`, not VW. Channel IDs are not secrets.

---

## Machine-readable companion

The hydration script's DEFAULT_MANIFEST (embedded in `chassis/scripts/hydrate-env-from-vw.sh`) is the source of truth for which VW item names map to which env vars. This doc is the human-readable version of that same mapping. If the two ever disagree, the script wins - and the disagreement should be filed as a bug.

Per-installer installs may supply a custom `vw-items.json` at `$CHASSIS_HOME/vw-items.json` to override or extend the DEFAULT_MANIFEST (e.g. to add a provider-specific second credential field). See the script's `VW_MANIFEST` env var description for how the override is loaded.

---

## Hydration order at install kickoff

1. Installer creates VW master account in browser at `https://vault.<tailscale-host>.ts.net` (or whatever the VW URL is for this install).
2. Installer populates Section 1 (chassis core) + any enabled plugin sections (~15 to 30 min with this doc open).
3. Installer generates a VW API token (VW web UI → Settings → API Key) and adds it to `Behalf.bot - Vaultwarden API token`.
4. Sean+${ASSISTANT_NAME} run `docker compose run -e RBW_EMAIL=<vw-email> -e RBW_URL=<vw-url> -e RBW_MASTER_PASS=<one-time> chassis hydrate-env`.
5. `chassis/scripts/hydrate-env-from-vw.sh` walks this item list and writes `$CHASSIS_HOME/.env`.
6. `docker compose up -d chassis` brings the dispatcher loop up.
7. First heartbeat fires in the briefings channel within 15 min.

Plugin-specific items (Sections 4-8) are populated later as each plugin's Phase-2 install fires.

---

## References

- Hydration runbook: `docs/hydration.md`
- Hydration script: `chassis/scripts/hydrate-env-from-vw.sh`
- Generic installer homework template: `docs/installer-homework.md`
- installer-2's install homework (V2, containerized): `docs/installer-homework-marc.md`
- Marc's per-installer VW overlay: `docs/install-marc-vw-items.md` (customer/installer-2 branch)
- Playwright credential wizard (issues #29): the wizard writes to the exact item names in this doc
- Issue #53 item 2: the tracker issue this doc closes
