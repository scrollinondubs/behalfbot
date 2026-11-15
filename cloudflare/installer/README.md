# behalfbot-installer

Cloudflare Worker that runs managed Behalf.bot installs (behalfbot#265). A paid
or admin-started install gets a private Discord channel, a homework list, a
start at the confirmed slot, and a 14-day garbage collector that closes the
channel and removes our SSH key from the customer's box.

**Deployed 2026-11-15** at `https://behalfbot-installer.sean-8a1.workers.dev`
(Sean-approved, from PR 271), with all six secrets set. Redeploys and secret
changes still need Sean's explicit approval.

First live end-to-end run, 2026-11-15: an admin install opened a private
channel, created a cx23 in a test Hetzner project, and key-auth SSH with the
cloud-init pinned host key worked on the first probe (`true`, `uname -a`). Close
removed our key; a rescue-mode boot confirmed `authorized_keys` no longer held
it. The server was then deleted by hand through the Hetzner API.

The two bearers are in Vaultwarden as `behalfbot-installer-site-secret` and
`behalfbot-installer-admin-secret`.

The spec is `CONTRACT.md` in the managed-installs plan (new-jaxity
`drafts/behalfbot/executor-managed-installs/`). SSH from a Cloudflare container
was proven in `SPIKE.md` next to it.

## What runs where

```
Behalfbot-site (Vercel)
  -> POST/PATCH/GET /installs          Bearer INSTALLER_SITE_SECRET
    -> Worker (src/index.ts, src/router.ts)
      -> InstallRegistry DO "registry"  idempotency + install list
      -> InstallDO per install_id       install / homework / journal tables + alarm
        -> Discord REST                 channel, posts, customer-message polling
        -> SshBox container per install ssh-keyscan + ssh to the pinned box
```

| File | Role |
|---|---|
| `src/index.ts` | Thin shell: DO classes, container class, Worker entry. The only file that imports Cloudflare runtime modules. |
| `src/router.ts` | Routes and auth. |
| `src/install.ts` | One install's state machine: create, slot, start rule, caps, GC, close, the `exec` seam. |
| `src/registry.ts` | Cross-install index for idempotency and `GET /installs`. |
| `src/rules.ts` | Pure timing and cap rules (start rule, GC, $40 / 4h caps). |
| `src/transport.ts` | `BoxTransport` (CONTRACT.md Interface 3) and the SSH implementation's Worker side. |
| `src/redact.ts` | Redaction applied to every journal row and Discord post. |
| `src/sshkey.ts` | Per-install ed25519 key, serialised for OpenSSH. |
| `src/discord.ts` | Discord REST helpers. |
| `container/ssh.mjs` | Host key pinning and the ssh call, inside the container. |

## Routes

| Route | Auth | What |
|---|---|---|
| `GET /healthz` | none | `{"ok":true}` |
| `POST /installs` | site | Create an install (Interface 1). `201` new, `200` repeat. |
| `GET /installs?status=open` | site | List installs. `status` is `open` (default), `all`, or one status. |
| `PATCH /installs/:id` | site | `{"scheduled_at": <unix>}` sets the slot. |
| `GET /installs/:id/journal` | admin | Install row, homework and the full journal. Linked from escalations. |
| `POST /installs/:id/homework` | admin | `{"item", "state": "pending" or "green"}`. Stand-in until the key form exists. |
| `PUT /installs/:id/hetzner-token` | admin | `{"token"}` saves the customer's Hetzner project token in this install's DO storage and turns that homework item green. Stand-in until the key form exists. |
| `POST /installs/:id/hetzner-server` | admin | Creates the customer's server in their project (see below) and pins it. |
| `POST /installs/:id/server` | admin | `{"server_ip", "host_key_fp"}` pins a box created some other way. |
| `POST /installs/:id/probe` | admin | `{"check": "true" or "uname" or "installer_key_present"}` runs one fixed command on the pinned box. There is no route for arbitrary commands. |
| `POST /installs/:id/close` | admin | Closes now: what GC does at day 14. |
| `POST /installs/:id/finish` | admin | `{"status": "done" or "failed"}` starts the GC clock. Stand-in until the runbook agent exists. |

Both bearers use a constant-time compare, and an unset secret matches nothing.

## Behaviour

- **Idempotency.** On `purchase_id`, and on `discord_user_id` for `source=admin` while that install is not closed. If Discord fails during create, the reply is `502` and the next POST for the same purchase retries on the same install.
- **Start rule.** The alarm fires at `scheduled_at`. If every homework item is green, the run starts. If not, the bot posts once in the channel, rechecks every 15 minutes, and escalates 2h after the slot. A `needs_human` install whose run never started can be rebooked with PATCH.
- **Caps.** Spend is $40 per install (`spend_usd_cents`) and the wall clock is 4h from run start. Hitting either sets `needs_human` and escalates.
- **Escalation.** Posts to `DISCORD_ESCALATION_CHANNEL_ID` with the install, step, attempts and journal link. The customer is told there is nothing for them to do, unless they are the blocker.
- **GC.** This only applies once an install is `done` or `failed`. The bot warns at day 11 and closes at day 14 after the later of the finish time and the customer's last message. Any message from the customer resets the clock. If the channel cannot be read, nothing closes and it retries in an hour.
- **Close.** Removes every `authorized_keys` line carrying our key blob over the transport, and fails if the blob is still there afterwards. Deletes the private key and any Hetzner token from DO storage, even if the removal failed, and in that case tells ops. Then deletes the channel. There is no Hetzner API call anywhere in this Worker, so a close can never touch the customer's server.
- **Secrets.** A secret only goes to the box as `run(cmd, { stdin })`. A command whose text contains a known secret is refused. Output is redacted before it is journaled or returned. All journal rows and Discord posts pass through the same redactor: exact stdin values in raw, base64 and URL-encoded form, plus common token shapes.
- **Server create.** Uses the customer's own token, in their own project: `cx23`, `nbg1`, `ubuntu-24.04`, IPv4 and IPv6. Our per-install public key goes in through a project SSH key object, which also stops Hetzner emailing a root password, and that object is deleted right after create. The host key is an ed25519 pair generated in the Worker and installed by cloud-init (`ssh_keys`), so `host_key_fp` is pinned before the box first answers: verified by construction, not trust on first use. The host private key goes only to Hetzner's user_data; it is not stored or journaled. The token stays in DO storage until close (TODO: move it to the box vault once that exists).
- **Host pinning.** `server_ip` must be a literal IP and `host_key_fp` a `SHA256:` fingerprint. The container keyscans the host and refuses unless its ed25519 key matches the pin. It then connects with that key as the only known host and `StrictHostKeyChecking=yes`. The Worker also refuses any answer that does not confirm the pinned fingerprint.

## Browser autopilot (`src/autopilot.ts`)

Relay between the Behalf.bot Autopilot Chrome extension (`chrome-ext/` at the
repo root) and a driver (Jax's `autopilot` MCP today). One `AutopilotDO`
instance named `relay` holds every code and session, with hibernatable
WebSockets. Runbooks live in `runbooks/` at the repo root and fix each
session's site allowlist; `local-test` only pairs when the Worker runs with
`AUTOPILOT_DEV_RUNBOOKS=1`.

| Route | Auth | What |
|---|---|---|
| `POST /installs/:id/autopilot/code` | admin | `{"runbook"}` (default `google-places`). Returns a single-use `XXX-XXX` code, 10 minutes. |
| `GET /installs/:id/autopilot` | admin | Sessions for the install: id, connected, allowlist, tab URL. |
| `POST /autopilot/pair` | the code | `{"code"}` returns the session token and allowlist. Over 20 failed attempts in a minute returns 429. |
| `GET /autopilot/ws?token=` | the token | The extension's socket. One per session. |
| `POST /autopilot/:session/cmd` | admin | A `RelayCommand` plus `wait_ms` (max 25000). Returns the reply, or `202 {"pending"}` to poll. |
| `GET /autopilot/:session/cmd/:id?wait_ms=` | admin | Polls a pending reply (a "Your turn" step). |
| `POST /autopilot/:session/secret` | admin | `{"item"}` returns a captured value once, as `text/plain`, then deletes it. |
| `DELETE /autopilot/:session` | admin | Ends the session now. |

- **Secrets.** A `save_secret` reply's value is held in the DO's storage for at
  most 5 minutes and leaves only through the secret route, once. The cmd reply
  says `captured`. Every other reply passes the redactor, which includes held
  values and Google API key shapes, so a snapshot of a page showing the key
  does not carry it.
- **Journal.** Every labelled command, pairing, connect, disconnect and secret
  capture is a `note` row with `step_name = autopilot` in that install's
  journal, through `InstallDO.autopilotNote`.
- **Teardown.** Socket close or error deletes the session, its token hash,
  held secrets and pending commands. Pairings whose socket never opens are
  swept after 10 minutes.

## Not built yet (TODO stubs in the code)

- **Runbook agent** (build step 5): per-step `claude -p` runs with `exec` as the only tool. `startRun` currently escalates to a human, and `chargeSpend` / `finish` are ready for it.
- **Key form** (build step 4): the per-install page that writes secrets through to the box vault and turns homework green. Until then, use `POST /installs/:id/homework`.
- **Stripe installs with no Discord id**: the channel is created bot-only if `discord_username` does not resolve. Adding the customer later is not wired yet.

## Secrets

The secret set is closed. Customer secrets live in per-install DO storage, never in Worker secrets.

| Name | Use |
|---|---|
| `DISCORD_INSTALLER_BOT_TOKEN` | Installer bot. Needs Manage Channels and Manage Roles in the guild (writing permission overwrites needs Manage Roles), plus View Channel, Send Messages and Read Message History. |
| `DISCORD_GUILD_ID` | Guild the install channels live in. |
| `DISCORD_ESCALATION_CHANNEL_ID` | `#installer-escalations`. |
| `ANTHROPIC_API_KEY` | Installer-only key so spend is visible. Unused until the runbook agent lands. |
| `INSTALLER_SITE_SECRET` | Bearer shared with the Behalfbot-site Vercel app. |
| `INSTALLER_ADMIN_SECRET` | Bearer for the admin routes and the journal link. |

Mint the two bearers fresh (32+ random bytes) and store all six in Vaultwarden.

## Develop

```bash
cd cloudflare/installer
npm install
npm run typecheck
npm test          # node --test, no network: Discord and the container are faked
```

Needs Node 23.6 or newer: the tests import `.ts` files directly (type stripping) and back the DO SQLite with `node:sqlite`, so the real schema runs. Verified on Node 26.

## Deploy (Sean's approval required)

```bash
export CLOUDFLARE_API_TOKEN=...      # "behalfbot-fable-cf-containers" in Vaultwarden
npx wrangler whoami                  # must show account 8a119b24123c444dddea567ffde1a405
for s in DISCORD_INSTALLER_BOT_TOKEN DISCORD_GUILD_ID DISCORD_ESCALATION_CHANNEL_ID \
         ANTHROPIC_API_KEY INSTALLER_SITE_SECRET INSTALLER_ADMIN_SECRET; do
  npx wrangler secret put "$s"
done
./deploy.sh                          # typecheck + tests, then wrangler deploy
curl -s https://behalfbot-installer.<subdomain>.workers.dev/healthz
```

End-to-end check after a deploy: create an admin install, `PUT` a test
project's Hetzner token, `POST /installs/:id/hetzner-server`, then
`POST /installs/:id/probe` with `true` and `uname`. Close with
`POST /installs/:id/close` and delete the test server through the Hetzner API
yourself: the Worker never deletes servers.
