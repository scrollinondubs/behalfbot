# Behalf.bot Setup Wizard

Automates the credential provisioning homework described in
`docs/installer-vw-template.md`. Runs on the installer's laptop before
the SSH install phase begins.

## What it does

The wizard opens a visible browser for each service. You sign in yourself
(the wizard never touches your login credentials) and then the wizard
navigates the post-login pages, creates the required integrations, and
writes the resulting tokens directly to your Vaultwarden vault.

### Phase 1 - this PR

| Service | What is automated | VW items written |
|---|---|---|
| Notion | Creates the `Behalf.bot - <Name>` integration, captures the token, prompts you to select databases/pages, clicks "Add Connection" on each, captures UUIDs | `Behalf.bot - Notion integration token`, `Behalf.bot - Notion DB IDs` |
| Google Cloud Console | Creates a GCP project, enables Gmail + Calendar + Drive APIs, walks the OAuth consent screen wizard, creates a Desktop OAuth client | `Behalf.bot - Google OAuth client` |

### Phase 2 - follow-up PR (issue #29)

| Service | What is planned |
|---|---|
| FDC (USDA Food Data Central) | Fill signup form, auto-confirm email if Gmail access granted, copy key to VW |
| Oura | Sign in, create PAT, copy to VW |

### Phase 3 - manual (Discord Developer Portal)

The Discord Developer Portal is explicitly NOT automated. Discord's Terms of
Service restrict automated UI interaction, and the Developer Portal bot-token
flow is gray area that risks account suspension. Use the screenshot walkthrough
in `docs/installer-homework.md` instead.

Manual steps for Discord:
1. Go to https://discord.com/developers/applications
2. Click "New Application", name it `Behalf.bot - <Your Name>`
3. Go to the Bot tab, click "Reset Token", copy the token
4. Under Privileged Gateway Intents, enable MESSAGE_CONTENT_INTENT
5. Go to OAuth2 - URL Generator, select scopes: bot + applications.commands
6. Select bot permissions: Send Messages, Read Message History, Add Reactions,
   Embed Links, Attach Files, Manage Webhooks
7. Copy the generated URL and open it to invite the bot to your server
8. Create webhooks for each channel (briefings, ops, leads, social)
9. Add `Behalf.bot - Discord bot token` to VW (password field)
10. Add `Behalf.bot - Discord briefings webhook` etc. per the canonical template

---

## Prerequisites

- Node.js 20+ (`node --version`)
- `rbw` installed (`brew install rbw` or `cargo install rbw`)
  - rbw is the Rust Vaultwarden CLI. The chassis uses it everywhere.
    Do NOT use the `bw` (Bitwarden) CLI - it has HTTPS enforcement issues
    against self-hosted VW instances.
- Your Vaultwarden instance is running and reachable
- You have created a Vaultwarden master account at your vault URL

If `rbw` is not installed, the wizard exits immediately with the install
command. Do not try to work around it.

---

## Running the wizard

```bash
cd chassis/scripts/setup-wizard
npm install
npm run build
npm run wizard
```

The wizard will prompt you for:
- Your full name (used to name the integration: `Behalf.bot - <Name>`)
- Your email address
- Your Vaultwarden URL
- Your Vaultwarden account email
- Which flows to run (Notion, Google)
- Your Vaultwarden master password (typed once, in terminal, never stored on disk)

Then a browser window opens for each service. Sign in yourself when prompted.

---

## Dry-run mode

Test the wizard without launching a browser or writing to VW:

```bash
npm run dry-run
```

Dry-run mode:
- Logs every action it WOULD take
- Shows the VW item names + field mapping it WOULD write
- Confirms the write contracts match `docs/installer-vw-template.md`
- Exits cleanly with the same summary output

Use dry-run to verify the wizard is configured correctly before running
against your real vault.

---

## Storage state (session persistence)

The wizard persists browser session cookies so you do not need to sign in to
each service on subsequent runs. Storage state files live at:

```
~/.behalf-bot-wizard/
  notion-session.json   - Notion login session
  google-session.json   - Google login session
```

These files contain browser cookies. They are:
- Stored only on your local machine (never uploaded anywhere)
- Reused on subsequent wizard runs (saves re-login time)
- Scoped to this wizard only (do not share or commit them)

To force a fresh login and discard the cached session:

```bash
npm run wizard -- --fresh-login
```

Or delete the session files manually:

```bash
rm ~/.behalf-bot-wizard/*.json
```

---

## What is written to Vaultwarden

All item names are exact matches to the canonical template in
`docs/installer-vw-template.md`. The chassis hydration script uses string
matching - a single typo causes silent hydration failure.

| Item name | VW field | Env var |
|---|---|---|
| `Behalf.bot - Notion integration token` | password | `NOTION_INTEGRATION_TOKEN` |
| `Behalf.bot - Notion DB IDs` | notes (KEY=value per line) | `NOTION_PROJECT_TRACKER_DB_ID`, `NOTION_READING_LIST_DB_ID`, `NOTION_MEMORY_PAGE_ID` |
| `Behalf.bot - Google OAuth client` | username = CLIENT_ID, password = CLIENT_SECRET | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` |

The wizard does NOT write `Behalf.bot - Google Workspace agent` (the agent email
+ App Password). That item requires a separate Google Workspace account setup
that is not automatable here. See `docs/installer-vw-template.md` Section 1.

---

## Selector stability note

The Notion and Google Cloud Console UIs change periodically. The wizard uses
semantic selectors (role, label, text) rather than CSS class names wherever
possible, which makes it more stable across UI updates. However:

- **Notion**: The integration creation UI has been stable since 2024. The
  "Add Connection" flow moved from "Share" to a dedicated "Connections" panel
  in late 2024 - the wizard handles both patterns.
- **Google Cloud Console**: Material UI selectors with `formcontrolname`
  attributes are relatively stable but GCP does major UI revisions every
  12-18 months. If the wizard fails at a step, it falls back to prompting
  you to paste the value manually.
- **CAPTCHA on first Google sign-in**: GCP occasionally shows a CAPTCHA or
  "verify it's you" challenge on first sign-in. The wizard waits up to 5
  minutes for you to complete it in the visible browser window.

If a selector fails, the wizard prompts you to paste the value manually rather
than crashing. All fallback paths are marked with a console.warn.

---

## After the wizard completes

Ping Sean + ${ASSISTANT_NAME} in `#behalf-bot-setup` with a message like:

```
Wizard complete. VW items written: Notion integration token, Notion DB IDs,
Google OAuth client. Ready for SSH install phase.
```

Sean + ${ASSISTANT_NAME} will then run `docker compose run chassis hydrate-env` to pull
the values from your VW and write them to the chassis `.env`.
