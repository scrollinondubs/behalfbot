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

## 4. Google: Gmail, Calendar and Sheets

**Read this section in full before you start clicking. The consent step is where
every installer gets stuck, and the fix is to know about it in advance.**

> ### Scope is not access. Share the file.
>
> The one that wastes an afternoon, so it is at the top rather than at the bottom.
>
> Getting Google working takes **two** independent things, and having one does not
> get you the other:
>
> 1. **Scope** - the token must be allowed to touch Sheets at all. Comes from the
>    OAuth client and the consent screen. Sections 4a to 4c.
> 2. **Access to the specific file** - the spreadsheet must be **shared with the
>    agent's Google account**, the same way you would share it with a colleague.
>    Editor if you want the agent to write to it. Section 4g.
>
> The agent is not you. It has its own Google account (section 2). A spreadsheet
> sitting in *your* Drive is invisible to it until you share that spreadsheet with
> *its* address - scope or no scope. Equally, sharing the file with the agent does
> nothing if the token never held the Sheets scope.
>
> If a spreadsheet reads as "not found" when you can plainly see it in your own
> browser, it is almost always this: you have not shared it. "Not found" is what
> Google returns for "exists, but not for you".

### Do you need this at all?

Only if `chassis.config.yaml` sets `modules.google.gmail`, `modules.google.calendar`
or `modules.google.sheets` to `true`.

| Your install | What to do |
|---|---|
| **Headless** (Linux box, VPS, anything you only reach over SSH) | You need this. It is the only path by which Google ever works on your box. |
| **Desktop** (a Mac you sit in front of) | Optional. Claude's hosted Google connectors are easier - click through them instead. Come back here only if you want Gmail attachment downloads or Sheets cell writes, neither of which the hosted connectors do. |

Claude's hosted connectors need a browser and a human at the keyboard. They do not
complete over SSH. That is the entire reason these MCP servers exist.

On Sheets specifically: the hosted Drive connector can *read a spreadsheet as a file*.
That is not the same as *writing a cell*, and writing cells is the thing people
actually ask for. Enable `modules.google.sheets` when you want the agent to update
rows, not just read them.

### Why not a service account?

Because it cannot read Gmail. A Google service account reads a normal user's mailbox
only with **domain-wide delegation**, which requires a Google Workspace admin and is
flatly impossible on a personal `@gmail.com` account. Calendar and Sheets would each
work with one, but then you would run two different credential mechanisms for three
Google services. You create one OAuth client instead, and consent once per server.

### 4a. In the Google Cloud Console (browser, any machine)

1. Create a project, or pick an existing one.
2. **APIs & Services > Library**: enable the API for each server you plan to turn on.
   - Gmail: **Gmail API**
   - Calendar: **Google Calendar API**
   - Sheets: **Google Sheets API** *and* **Google Drive API**. Both. The Sheets
     server uses Drive to look up which folder a spreadsheet lives in, and it fails
     confusingly if the Drive API is off.
3. **APIs & Services > OAuth consent screen**:
   - User type **External** is correct for a personal Gmail account.
   - Add the agent's Google address as a **Test user**. This is not optional. Consent
     will refuse to proceed until it propagates, which takes a few minutes.
4. **APIs & Services > Credentials > Create Credentials > OAuth client ID**:
   - Application type: **Desktop app**. Not "Web application" - Desktop gets you a
     loopback redirect, which is what makes the consent step below work.
   - Download the JSON. Rename it `gcp-oauth.keys.json`.

That one file is the OAuth **client**. All three servers read it. Do not create three.

### 4b. Scopes

You do not type these in anywhere - each MCP server requests its own. Listed so you
know what you are consenting to when the screen appears:

- Gmail: full mailbox access, **including send**. The server exposes `send_email`.
  The chassis policy that email is drafted and never auto-sent (`trust_line.email:
  read_and_draft`) is a rule in the agent's prompt, not a limit on the token. If
  that distinction matters to you, use a dedicated agent-side Gmail account.
- Calendar: read-write. `trust_line.calendar` controls which calendar *tools* the
  agent is handed, not which scope the token holds - see section 4d.
- Sheets: `.../auth/spreadsheets` **and `.../auth/drive`**. Note the second one. It
  is full Drive, not Drive-readonly, and the Sheets server needs it to resolve a
  spreadsheet's parent folder. So consenting the Sheets server hands its token more
  reach than the word "Sheets" suggests. That is stated here rather than buried: if
  it is more than you want to grant, leave `modules.google.sheets` off.

**And none of these scopes gets you access to a specific spreadsheet.** That takes
sharing the file. Section 4g.

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

# Sheets. No `auth` subcommand - this server consents on first use instead.
# Start it, and it opens the browser and writes the token to GOOGLE_OAUTH_TOKENS.
# It prints nothing on success and keeps running: once your browser says
# "authenticated", press Ctrl-C. The token file is already on disk.
GOOGLE_OAUTH_CREDENTIALS="$PWD/gcp-oauth.keys.json" \
GOOGLE_OAUTH_TOKENS="$PWD/sheets-token.json" \
  npx @shivaduke28/google-sheets-mcp
```

Each command opens your browser. Sign in **as the agent's Google account**, not your
personal one, and accept. You consent once per server - three times, if you are
enabling all three - against the one client.

The Sheets server is the odd one out: it authenticates lazily, on the first tool call
rather than at startup. If the browser does not open when you run it, invoke a tool
against it, or simply check that `sheets-token.json` exists after you have completed
the consent screen. That file existing is the success condition.

Then copy the files to the server and lock them down:

```bash
ssh you@your-box 'mkdir -p ~/.behalfbot/secrets/google'
scp gcp-oauth.keys.json gmail-token.json calendar-token.json sheets-token.json \
    you@your-box:~/.behalfbot/secrets/google/
ssh you@your-box 'chmod 600 ~/.behalfbot/secrets/google/*'
```

`secrets/` is gitignored. These files are live credentials - treat every token
file exactly like a password, because that is what it is.

#### Path B - SSH tunnel, consent stays on the server

If you would rather the tokens never touch your laptop. Each server redirects to a
loopback port; forward that port from your laptop and the browser round-trip closes
through the tunnel.

Run the auth command on the server first and **read the port off the URL it prints**
(Gmail and Sheets both use `3000`, so consent them one at a time, not in two shells
at once; do not assume the Calendar server matches either). Then, from your laptop:

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
    sheets: true
trust_line:
  calendar: read_only
  sheets: read_only
```

`$CUSTOMER_HOME/.env` - paths as seen from **inside** the container, where the
customer dir is mounted at `/app/customer`:

```
GMAIL_OAUTH_PATH=/app/customer/secrets/google/gcp-oauth.keys.json
GMAIL_CREDENTIALS_PATH=/app/customer/secrets/google/gmail-token.json
GOOGLE_OAUTH_CREDENTIALS=/app/customer/secrets/google/gcp-oauth.keys.json
GOOGLE_CALENDAR_MCP_TOKEN_PATH=/app/customer/secrets/google/calendar-token.json
GOOGLE_SHEETS_TOKEN_PATH=/app/customer/secrets/google/sheets-token.json
```

All three servers read the same `gcp-oauth.keys.json`. Each keeps its own token.

Only if you set `trust_line.sheets: read_write`, one more - the write allowlist from
section 4g:

```
GOOGLE_SHEETS_ALLOWLIST=/app/customer/secrets/google/sheets-allowlist.json
```

### 4f. Test-mode tokens expire in 7 days

While your OAuth consent screen is in **Testing**, Google expires refresh tokens after
one week and Google will not tell you - the agent just quietly stops seeing your mail.

Fix it once, properly: **OAuth consent screen > Publish app**. You do not need Google
verification for this; an unverified app still works for accounts you listed as test
users, and publishing stops the 7-day clock. If you leave it in Testing, expect to
re-run the auth commands from 4c every week.

### 4g. Sheets: share the file, then decide about writes

#### Share the spreadsheet with the agent. This is the step everyone misses.

Everything in 4a to 4f gets the agent a token with the **scope** to use Sheets. It
does not get the agent into **your spreadsheet**. Those are different things, and a
correctly-scoped token still returns "not found" on a spreadsheet nobody shared.

For each spreadsheet you want the agent to touch:

1. Open it in Google Sheets.
2. **Share**.
3. Add the **agent's Google address** - the account you consented with in 4c, not
   your own.
4. Role:
   - **Viewer** if the agent only needs to read it.
   - **Editor** if the agent needs to write to it. Viewer plus a write scope still
     cannot write. Google checks the file permission too.
5. Send. Sharing is effective immediately - no propagation wait, unlike the test-user
   step in 4a.

If you would rather not share sheet by sheet, share a whole **folder** with the agent
and move the spreadsheets into it. Folder permissions are inherited.

A diagnosis you may be offered and should not believe: *"adding an editor will not
help, the problem is the API scope, not file permissions."* It is both, and they are
independent. Scope without sharing fails. Sharing without scope fails. You need the
two together, and if the agent cannot see a sheet you are looking at right now, the
missing half is nearly always the sharing.

#### Tools the agent gets

Verified against the running server, not copied from a README:

`list-spreadsheets`, `get-spreadsheet`, `get-values`, `update-values`,
`append-values`, `create-from-template`.

#### `trust_line.sheets`

| Value | What the agent can do |
|---|---|
| `read_only` (default) | Read any spreadsheet that has been shared with it. **Every write is refused by the server.** |
| `read_write` | The above, plus write - but only to spreadsheets you name by id in the allowlist below. There is no blanket write tier. |

This works differently from `trust_line.calendar`, and the difference is in your
favour. Calendar hides its write tools from the agent while the token underneath
stays fully capable. The Sheets server instead **refuses the write itself**, at call
time, when the target spreadsheet is not allowlisted for writing. The write tools stay
visible and simply come back denied. Enforcement, rather than concealment.

To allow writes, set `trust_line.sheets: read_write` and create the allowlist file at
the `GOOGLE_SHEETS_ALLOWLIST` path from 4e:

```json
{
  "sheets": {
    "allowedSpreadsheets": [
      {
        "id": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
        "name": "LP prospects",
        "access": "readwrite"
      },
      {
        "id": "1AbCdEfGhIjKlMnOpQrStUvWxYz",
        "name": "Board reporting",
        "access": "readonly"
      }
    ]
  }
}
```

The `id` is the long string in the spreadsheet's URL, between `/d/` and `/edit`. The
file must be writable by the agent: `create-from-template` appends to it. And it is
not a substitute for step 1 of this section - a spreadsheet allowlisted here but never
shared with the agent is still invisible to it.

One honest gap, so you hear it from us rather than discover it: on the `read_only`
floor, `create-from-template` can still **copy** a spreadsheet the agent can read into
a new file in its own Drive. It cannot modify an existing spreadsheet, which is the
thing `read_only` is there to prevent. But "read_only" is not literally zero writes to
Drive, and the server offers no tool filter with which we could make it so.

---

## Cross-references

- `docs/hydration.md` - what bootstrap does with what you staged
- `docs/mcp-setup.md` - per-MCP wiring, including the three Google servers
- `docs/security.md` - the guardrail model these credentials sit behind
