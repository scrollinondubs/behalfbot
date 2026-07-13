# Installer homework

> Audience: the human installing a chassis instance, before `bootstrap.sh` runs.
> Companion to `docs/hydration.md` (what bootstrap does with what you stage here)
> and `docs/mcp-setup.md` (per-MCP wiring).

Everything here is work `bootstrap.sh` deliberately does NOT do, because it needs
a human with a browser, a credit card, or an account you own. Do it before install
day and the install is 30-45 minutes. Do it during, and it is an afternoon.

---

## 1. Machine

- A Linux box (Ubuntu 22.04+ recommended) or a Mac. 8GB RAM is the realistic floor.
- Docker + Docker Compose installed and working as your user, not just as root.
- Tailscale installed, node joined, shared with whoever is helping you install.
- SSH key handoff done, if someone else is driving the install.

## 2. Agent-side accounts

The chassis runs as an agent that is NOT you. That separation is the whole security
model: when you fire the agent, you revoke its accounts, not yours.

Provision a separate account for each surface you plan to enable - GitHub, Google,
Discord, and your second brain (Notion or SiYuan). Do not hand the chassis your
personal credentials.

## 3. Credentials to stage

Stage these in Vaultwarden (or the password manager of your choice) before install.
The item names below are matched as exact strings by
`chassis/scripts/hydrate-env-from-vw.sh` - a single typo is a silent hydration
failure, so copy them literally.

| Vaultwarden item | Needed when |
|---|---|
| `Behalf.bot - GitHub PAT` | Always. Scopes: `repo`, `workflow`, `read:org`. |
| `Behalf.bot - chassis OpenAI key` | Always. |
| `Behalf.bot - Postgres password` | Always. `openssl rand -base64 32`. |
| `Behalf.bot - Vaultwarden API token` | Always. |
| `Behalf.bot - Google OAuth client` | Gmail or Calendar. See section 4. |
| `Behalf.bot - Notion integration token` | Second brain = Notion. |
| `Behalf.bot - Telegram bot token` | Telegram surface. |
| `Behalf.bot - Slack bot token` | Slack surface. |
| `Behalf.bot - Tailscale auth key` | Headless / unattended Tailscale join. |

The Discord bot token is wired through Claude Code's plugin layer, not `.env` -
run `claude /discord:configure` after install.

---

## 4. Google: Gmail and Calendar

**Read this section in full before you start clicking. The consent step is where
every installer gets stuck, and the fix is to know about it in advance.**

### Do you need this at all?

Only if `chassis.config.yaml` sets `modules.google.gmail` or
`modules.google.calendar` to `true`.

| Your install | What to do |
|---|---|
| **Headless** (Linux box, VPS, anything you only reach over SSH) | You need this. It is the only path by which Google ever works on your box. |
| **Desktop** (a Mac you sit in front of) | Optional. Claude's hosted Google connectors are easier - click through them instead. Come back here only if you want Gmail attachment downloads, which the hosted connector cannot do. |

Claude's hosted connectors need a browser and a human at the keyboard. They do not
complete over SSH. That is the entire reason these two MCP servers exist.

### Why not a service account?

Because it cannot read Gmail. A Google service account reads a normal user's mailbox
only with **domain-wide delegation**, which requires a Google Workspace admin and is
flatly impossible on a personal `@gmail.com` account. Calendar alone would work with
one, but then you would run two different credential mechanisms for two Google
services. You create one OAuth client instead, and consent once per server.

### 4a. In the Google Cloud Console (browser, any machine)

1. Create a project, or pick an existing one.
2. **APIs & Services > Library**: enable **Gmail API** and **Google Calendar API**.
   Enable only the one you actually plan to turn on if you are only doing one.
3. **APIs & Services > OAuth consent screen**:
   - User type **External** is correct for a personal Gmail account.
   - Add the agent's Google address as a **Test user**. This is not optional. Consent
     will refuse to proceed until it propagates, which takes a few minutes.
4. **APIs & Services > Credentials > Create Credentials > OAuth client ID**:
   - Application type: **Desktop app**. Not "Web application" - Desktop gets you a
     loopback redirect, which is what makes the consent step below work.
   - Download the JSON. Rename it `gcp-oauth.keys.json`.

That one file is the OAuth **client**. Both servers read it. Do not create two.

### 4b. Scopes

You do not type these in anywhere - each MCP server requests its own. Listed so you
know what you are consenting to when the screen appears:

- Gmail: full mailbox access, **including send**. The server exposes `send_email`.
  The chassis policy that email is drafted and never auto-sent (`trust_line.email:
  read_and_draft`) is a rule in the agent's prompt, not a limit on the token. If
  that distinction matters to you, use a dedicated agent-side Gmail account.
- Calendar: read-write. `trust_line.calendar` controls which calendar *tools* the
  agent is handed, not which scope the token holds - see section 4d.

### 4c. Consent, on a headless box

The consent flow opens a browser and redirects to `localhost`. Your VPS has neither.
Two ways out. **The first one is the recommended path.**

#### Path A - consent on your laptop, copy the tokens up (recommended)

Run the auth flow where a browser already exists, then move the resulting token files
to the server. The tokens are portable; nothing in them is machine-specific.

On your **laptop**, with `gcp-oauth.keys.json` in the current directory:

```bash
mkdir -p ~/bb-google && cd ~/bb-google
# put gcp-oauth.keys.json here first

# Gmail. Writes the token to GMAIL_CREDENTIALS_PATH.
GMAIL_OAUTH_PATH="$PWD/gcp-oauth.keys.json" \
GMAIL_CREDENTIALS_PATH="$PWD/gmail-token.json" \
  npx @gongrzhe/server-gmail-autoauth-mcp auth

# Calendar. Writes the token to GOOGLE_CALENDAR_MCP_TOKEN_PATH.
GOOGLE_OAUTH_CREDENTIALS="$PWD/gcp-oauth.keys.json" \
GOOGLE_CALENDAR_MCP_TOKEN_PATH="$PWD/calendar-token.json" \
  npx @cocal/google-calendar-mcp auth
```

Each command opens your browser. Sign in **as the agent's Google account**, not your
personal one, and accept. You consent twice - once per server - against the one client.

Then copy all three files to the server and lock them down:

```bash
ssh you@your-box 'mkdir -p ~/.behalfbot/secrets/google'
scp gcp-oauth.keys.json gmail-token.json calendar-token.json \
    you@your-box:~/.behalfbot/secrets/google/
ssh you@your-box 'chmod 600 ~/.behalfbot/secrets/google/*'
```

`secrets/` is gitignored. These files are live credentials - treat the two token
files exactly like passwords, because that is what they are.

#### Path B - SSH tunnel, consent stays on the server

If you would rather the tokens never touch your laptop. Both servers redirect to a
loopback port; forward that port from your laptop and the browser round-trip closes
through the tunnel.

Run the auth command on the server first and **read the port off the URL it prints**
(the Gmail server uses `3000`; do not assume the Calendar server matches). Then, from
your laptop:

```bash
ssh -L 3000:localhost:3000 you@your-box   # swap 3000 for the port it printed
```

Re-run the auth command inside that SSH session, and open the printed URL in your
laptop's browser. The redirect lands back on the server through the tunnel.

Path B is fiddlier and the port is the part people get wrong. If it fights you, use
Path A.

### 4d. Read-only by default

`trust_line.calendar` in `chassis.config.yaml`:

| Value | Calendar tools the agent gets |
|---|---|
| `read_only` (default) | `list-calendars`, `list-events`, `search-events`, `get-event`, `get-freebusy`, `get-current-time`, `list-colors` |
| `read_write` | the above, plus `create-event`, `update-event`, `delete-event`, `respond-to-event` |

Raise it to `read_write` only when you actually want the agent booking things. An
agent that cannot write a calendar cannot double-book you in front of a client.

Two honest caveats:

- This is **tool-gating, not scope-gating**. The token on disk holds a read-write
  scope either way. Withholding the write tools removes the agent's means, not the
  token's power. For a scope-level guarantee, mint the token by hand against a
  read-only scope.
- The `manage-accounts` tool is exposed regardless of this setting - the calendar
  server always ships it, because it is how re-authentication happens. It manages
  which Google accounts are connected. It cannot touch an event.

Gmail has no equivalent tool filter. It is all-or-nothing: `modules.google.gmail:
true` hands the agent the full mailbox surface, send included.

### 4e. Wire it up

`chassis.config.yaml`:

```yaml
modules:
  google:
    gmail: true
    calendar: true
trust_line:
  calendar: read_only
```

`$CUSTOMER_HOME/.env` - paths as seen from **inside** the container, where the
customer dir is mounted at `/app/customer`:

```
GMAIL_OAUTH_PATH=/app/customer/secrets/google/gcp-oauth.keys.json
GMAIL_CREDENTIALS_PATH=/app/customer/secrets/google/gmail-token.json
GOOGLE_OAUTH_CREDENTIALS=/app/customer/secrets/google/gcp-oauth.keys.json
GOOGLE_CALENDAR_MCP_TOKEN_PATH=/app/customer/secrets/google/calendar-token.json
```

Both servers read the same `gcp-oauth.keys.json`. Each keeps its own token.

### 4f. Test-mode tokens expire in 7 days

While your OAuth consent screen is in **Testing**, Google expires refresh tokens after
one week and Google will not tell you - the agent just quietly stops seeing your mail.

Fix it once, properly: **OAuth consent screen > Publish app**. You do not need Google
verification for this; an unverified app still works for accounts you listed as test
users, and publishing stops the 7-day clock. If you leave it in Testing, expect to
re-run the auth commands from 4c every week.

---

## Cross-references

- `docs/hydration.md` - what bootstrap does with what you staged
- `docs/mcp-setup.md` - per-MCP wiring, including the two Google servers
- `docs/security.md` - the guardrail model these credentials sit behind
