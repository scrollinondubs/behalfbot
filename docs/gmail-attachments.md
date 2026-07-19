# Gmail attachments over IMAP

`chassis/scripts/gmail-attachment.py` lists and downloads attachments from the
install's Gmail mailbox. Standard library only, read-only, no new dependencies.

Ported from `scrollinondubs/new-jaxity#314`; decision record in
`scrollinondubs/new-jaxity#311`.

## Why this exists

The Claude.ai Gmail connector exposes `get_thread`, `get_message`,
`search_threads`, `create_draft` and label operations. It has no attachment
endpoint at all. Every install hits that the first time someone forwards a PDF
and asks the agent to read it. This is a capability gap, not a permissions
problem - no scope, no admin toggle and no reconnect fixes it.

## Why IMAP and not OAuth

The obvious alternative is a self-hosted Gmail MCP server with our own OAuth
client. It was rejected.

An OAuth consent screen left in **External + Testing** issues refresh tokens
that expire after **seven days**. The agent then simply stops being able to
read mail. Nothing errors loudly; the next scheduled run just does less. At
least one install in this fleet is believed to have been sitting in exactly
that state.

Moving the consent screen to Published requires verification for restricted
Gmail scopes, which is a review process, not a checkbox. A service account with
domain-wide delegation avoids the expiry but grants access to every mailbox on
the domain - wildly disproportionate for fetching a PDF.

IMAP with an app password has no consent screen, no Cloud project, and no
expiry cliff. The credential is revocable from one page and scoped to one
mailbox.

The OAuth block in `.env.example` (`GMAIL_OAUTH_PATH` and friends) is separate
and unaffected. It drives the Gmail MCP server for drafts and labels. This
script does not use it.

## Env var contract

Two variables. There is exactly one accepted name for each - no aliases, no
fallbacks.

| Variable | Meaning |
|---|---|
| `GOOGLE_AGENT_EMAIL` | The mailbox to read. Also the IMAP username. |
| `GOOGLE_AGENT_APP_PASSWORD` | A Google app password for that account. |

Both are already in the default Vaultwarden manifest in
`chassis/scripts/hydrate-env-from-vw.sh`, under the item
**"Behalf.bot - Google Workspace agent"** (username field and password field).
Until this script landed nothing in the chassis read them - the manifest
promised a credential that had no consumer.

Single-name-per-value is deliberate. The chassis has been bitten once by
carrying two names for one secret: `NOTION_API_TOKEN` in `.env.example` and the
factory, `NOTION_INTEGRATION_TOKEN` in the hydrator and `.mcp.json.template`.
An installer who followed the wrong one shipped a literal placeholder as a
bearer token and got a 401 on every call.

### Resolution order

1. Process environment.
2. `$CUSTOMER_HOME/.env`, read straight off disk.
3. `$CUSTOMER_HOME/.env.baked`, for LaunchDaemon-style contexts that never
   sourced `.env`.

`CUSTOMER_HOME` resolves per `chassis/scripts/_env.sh` - `CHASSIS_HOME` first
for legacy co-located installs, then `CUSTOMER_HOME`, then `/app/customer`,
then `~/.behalfbot`.

Step 2 exists because credential state goes stale. In the new-jaxity original
the staleness was in the `bw` CLI, which reads a local encrypted cache rather
than the server, so a vault item created minutes earlier was invisible until a
sync - the very first live run failed on exactly that. The chassis never
reaches Vaultwarden at runtime (hydration is bootstrap-time only), so the same
staleness reappears one layer up: the process environment was captured before
the operator hydrated. Reading the file settles it without a restart.

## Operator setup

1. **Turn on 2-Step Verification** for the mailbox account. Google will not
   offer app passwords without it.
2. **Generate an app password** at
   <https://myaccount.google.com/apppasswords>. Google shows it as four
   space-separated groups. Copy it verbatim - the script strips the spaces
   itself, so a paste from the Google UI works.
3. **Store it in Vaultwarden** in an item named
   `Behalf.bot - Google Workspace agent`. Username field: the mailbox address.
   Password field: the app password.
4. **Hydrate**: run `chassis/scripts/hydrate-env-from-vw.sh`. It writes both
   variables into `$CHASSIS_HOME/.env`.
5. **Verify**: `python3 chassis/scripts/gmail-attachment.py check` should print
   `PASS|Gmail IMAP credentials present for <address>`. The same check runs as
   `gmail_attachment_credentials` in `chassis/scripts/smoke-test.sh`.
6. **Confirm end to end** against a real message:
   `python3 chassis/scripts/gmail-attachment.py list --gmail-search 'has:attachment'`

On an install with no Vaultwarden, skip steps 3 and 4 and set the two variables
in `$CUSTOMER_HOME/.env` by hand. Chmod that file 0600.

### Workspace admin note

A Google Workspace administrator can disable app passwords for the entire
organisation (Admin console > Security > Authentication > Less secure apps and
app passwords). If that setting is off, step 2 will not offer the option at
all and no amount of debugging on this side will help. Check it first.

## Usage

```
# What is attached to the message matching a search
python3 chassis/scripts/gmail-attachment.py list --gmail-search 'subject:"Invoice"'

# Fetch by filename substring, or by the index `list` printed
python3 chassis/scripts/gmail-attachment.py fetch --gmail-search '...' --name Invoice -o ~/Downloads
python3 chassis/scripts/gmail-attachment.py fetch --gmail-search '...' --index 0 -o ~/Downloads

# Everything on one message
python3 chassis/scripts/gmail-attachment.py fetch-all --message-id '<abc@mail.example>' -o ~/Downloads

# Config check - env only, no network
python3 chassis/scripts/gmail-attachment.py check
```

Selectors, in the order to reach for them:

- `--gmail-search` - Gmail search-box syntax via the `X-GM-RAW` IMAP
  extension. Far stronger than plain IMAP SEARCH. This is the default choice.
- `--message-id` - exact RFC 5322 Message-ID, with or without angle brackets.
- `--subject` - substring of the Subject header.
- `--search` - raw IMAP SEARCH criteria, space separated.
- `--uid` - exact IMAP UID, skipping the search step entirely.

### Message selection

A search that matches several messages does **not** take the most recent one.
A subject search returns a whole thread, and the attachment is almost always on
the oldest message while the replies are newest. The script scans newest-first
and stops at the first message that actually carries an attachment, printing
which UID it chose. Pin a different one with `--uid`.

## Behaviour worth knowing

**Read-only.** Mailboxes are selected with `readonly=True` and messages fetched
with `BODY.PEEK[]`. Nothing is flagged, moved, or marked read.

**Default mailbox** is `[Gmail]/All Mail`, the only folder guaranteed to hold a
message regardless of its labels. Override with `--mailbox`.

**Inline images are excluded.** An HTML mail with an embedded logo carries an
`image/png` part with `Content-Disposition: inline` and a `Content-ID` the body
references. That is page furniture, and listing it buries the real file. Pass
`--include-inline` to get them.

**Forwarded messages are descended into, and attached `.eml` files are
themselves emitted.** Both cases occur and neither subsumes the other: a
forwarded proposal is a PDF inside a `message/rfc822` part, while a covering
note with seven `.eml` attachments has nothing but body text inside each one.
A `message/rfc822` part is emitted as an attachment when it carries
`Content-Disposition: attachment`. Delivery-status bounces embed the original
with no disposition and are correctly not treated as attachments.

**Filenames are sanitised, then the resolved path is re-checked.** Attachment
filenames are attacker-controlled - someone can email a file called
`../../.ssh/authorized_keys`. It lands as `authorized_keys` inside the target
directory. Two independent layers, and the second uses `resolve()` so a symlink
inside the target pointing outward is also rejected.

**25 MB size cap**, matching Gmail's own per-message attachment limit, checked
against the decoded length before any write. Raise with `--max-bytes`.

**Existing files are never clobbered** - a `-1`, `-2` suffix is added.

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `GOOGLE_AGENT_APP_PASSWORD not set` | Never hydrated, or the vault item has no password field | Populate the VW item, re-run `hydrate-env-from-vw.sh` |
| Smoke test `FAIL ... is set but ... is not` | Half-configured: hydration pulled one field, not the other | Check both fields exist on the VW item |
| `IMAP login failed for <address>` | 2FA off, app passwords disabled org-wide, or the password was revoked | Work the list in that order - the org-wide toggle is the one people miss |
| `could not select mailbox` | IMAP disabled in Gmail settings, or a bad `--mailbox` | Gmail Settings > Forwarding and POP/IMAP > Enable IMAP |
| `no message matched that selector` | Search matched nothing in All Mail | Try the same query in the Gmail web UI first |
| Reports no attachments on a message that visibly has one | Should not happen - the `message/rfc822` cases are handled both ways | File it with the raw message; that is the class of bug this walker exists to prevent |

The IMAP login error deliberately does not echo the server's reply. A failed
`LOGIN` response can quote the credential back.

## Tests

`chassis/scripts/tests/test_gmail_attachment.py` - 56 tests. No live mailbox,
no Vaultwarden, no network. MIME trees are built in memory and credential
resolution is pointed at a temp directory.

```
python3 -m pytest chassis/scripts/tests/test_gmail_attachment.py -v
```

Runs in CI via `.github/workflows/python-tests.yml`.
