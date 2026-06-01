# Behalf.bot Install Runbook

> **Audience:** the operator (Sean+${ASSISTANT_NAME}) driving an install via SSH.
> The installer (the person getting Behalf.bot) does Step 0 and hands over.
> Everything else is SSH-driven. See `docs/installer-homework.md` for what
> the installer must do before kickoff day.

This runbook supersedes the scattered notes across `docs/hydration.md`,
`bootstrap.sh`, and `docs/containerization.md`. Each step has a done-when
criterion. Steps must be executed in order; an installer can re-enter from
any completed step.

---

## Pre-kickoff checklist (installer's homework)

Before booking the install call, confirm with the installer:

- [ ] Vaultwarden running; exact URL + port noted (e.g. `http://fatboy:8222`)
- [ ] VW master account email confirmed - NOT assumed from agent identity
- [ ] All VW items named per `docs/installer-vw-template.md` (exact names matter)
- [ ] Discord bot created; `MESSAGE CONTENT INTENT` toggled ON
      Direct link: https://discord.com/developers/applications -> your app -> Bot
      -> Privileged Gateway Intents -> MESSAGE CONTENT INTENT -> Save Changes
- [ ] Tailscale node shared with Sean+${ASSISTANT_NAME}; node reachable from ${ASSISTANT_NAME} tailnet
- [ ] SSH public key added to authorized_keys; Linux username confirmed (`whoami`)
- [ ] `#<installer>-setup` Discord channel created with installer + Sean + ${ASSISTANT_NAME}-bot

---

## Step 1 - Connect + probe the box

```bash
ssh <user>@<hostname>.taila....ts.net
```

Run the pre-kickoff sanity check (baked into chassis):

```bash
bash chassis/scripts/verify-tailscale.sh
```

Expected: `7/7 checks passed - all green.`

If the chassis repo isn't on the box yet, ship it as a tarball
(no auth needed, no bot account required at this stage):

```bash
# Operator-side: from your local chassis clone
git archive --format=tar.gz --prefix=behalfbot/ origin/main \
  | ssh <user>@<host> "mkdir -p ~/behalfbot && tar xz -C ~"
```

**Done when:** SSH works, `~/behalfbot/` exists on the box.

---

## Step 2 - Install Docker (if not present)

```bash
# Debian / Ubuntu
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker   # or log out + back in
docker info     # confirm
```

**Done when:** `docker info` returns without error and no sudo needed.

---

## Step 3 - Scaffold the customer directory

Copy `docker-compose.yml` + `.env.example` from the chassis repo onto the box:

```bash
cp ~/behalfbot/docker-compose.yml ~/behalfbot-compose.yml  # keep handy
cp ~/behalfbot/.env.example ~/behalfbot/.env.compose       # will edit next
```

Actually the working directory for the Compose stack IS `~/behalfbot/`.
That directory already has `docker-compose.yml` from the tarball. Edit
`.env.compose` (which becomes `.env` in the Compose sense - see note):

> **Two `.env` files:** `docker-compose.yml` reads a Compose-level `.env`
> from its own directory for infrastructure vars (`CUSTOMER_HOME`,
> `POSTGRES_PASSWORD`, etc.). The chassis container reads
> `${CUSTOMER_HOME}/.env` for Claude/Discord/plugin secrets. Keep them
> separate - never merge them.

Create `~/behalfbot/.env` (Compose-level):

```bash
cat > ~/behalfbot/.env <<'EOF'
CUSTOMER_HOME=/home/<USER>/behalfbot
CUSTOMER_CLAUDE_DIR=/home/<USER>/.claude

# Match the host UID/GID so bind-mount files aren't root-owned
INSTALLER_UID=<uid from: id -u>
INSTALLER_GID=<gid from: id -g>

CHASSIS_IMAGE=ghcr.io/scrollinondubs/behalfbot-chassis:latest
DISPATCHER_INTERVAL_SECONDS=900

# Generate both with: openssl rand -base64 32
POSTGRES_PASSWORD=<strong random>
VAULTWARDEN_ADMIN_TOKEN=<strong random>

VAULTWARDEN_HOST_PORT=8222
VAULTWARDEN_DOMAIN=http://localhost:8222
EOF
```

**Done when:** `~/behalfbot/.env` has no `CHANGEME` values; all UIDs correct.

---

## Step 4 - Pull image + start Postgres + Vaultwarden

```bash
cd ~/behalfbot
docker compose pull
docker compose up -d postgres vaultwarden
```

Wait ~30 seconds for Postgres to initialize, then check:

```bash
docker compose ps
docker compose logs postgres --tail 20
```

Expected: both services `Up (healthy)`.

Set up Vaultwarden (one-time):
- Open `http://localhost:8222/admin` in a browser (Tailscale-tunnel if
  accessing from Sean's machine: `http://<host>:8222/admin`)
- Log in with `VAULTWARDEN_ADMIN_TOKEN`
- Create the installer's VW user account (their email, a strong password)
- Log into VW as that user at `http://localhost:8222`
- Populate items per `docs/installer-vw-template.md`

**Done when:** `docker compose ps` shows postgres + vaultwarden both healthy;
VW items populated per template.

---

## Step 5 - Pull secrets from Vaultwarden into .env

This is a one-time pull from VW into `${CUSTOMER_HOME}/.env` (the chassis
instance secrets file, NOT the Compose-level one).

```bash
cd ~/behalfbot
docker compose run --rm \
  -e RBW_EMAIL=<installer VW master email> \
  -e RBW_URL=http://vaultwarden \
  -e RBW_MASTER_PASS=<installer VW master password> \
  chassis hydrate-env
```

This writes `~/behalfbot/.env` (overwriting the Compose-level one if you
used the same filename - use a distinct name for the Compose env or set
`--env-file` explicitly).

> **Naming convention:** avoid the two-`.env`-files footgun. Name the
> Compose-level env file `~/behalfbot/.env.compose` and tell docker compose
> to read it: `docker compose --env-file .env.compose ...`. The chassis
> container reads `/app/customer/.env` (the bind-mounted instance secrets)
> separately. This keeps the two scopes unambiguous.

Re-run `hydrate-env` any time a secret rotates. Idempotent: only overwrites
lines present in VW; doesn't touch lines the manifest doesn't know about.

**Done when:** `~/behalfbot/.env` (the instance secrets file) has
`INSTANCE_NAME`, `DISCORD_BOT_TOKEN`, webhook URLs, `GITHUB_PAT` all set.
Verify: `grep INSTANCE_NAME ~/behalfbot/.env`.

---

## Step 6 - Install Claude Code + authenticate

Claude Code must be installed on the HOST (not inside the container), because
the `~/.claude/` directory on the host is bind-mounted into the container.
Claude Code OAuth credentials live at `~/.claude/.credentials.json` on the
host; the container reads them from the bind-mount.

```bash
# Install Node 20+ first if not present
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo bash -
sudo apt-get install -y nodejs

# Install Claude Code globally
npm install -g @anthropic-ai/claude-code

# Login - this opens a browser auth URL
claude login
```

**Auth verification (REQUIRED - V1 lesson #37):**

```bash
claude auth status
```

Expected output must show the installer's email, NOT Sean's or ${ASSISTANT_NAME}'s:

```
Logged in as: <installer>@<domain>
Subscription: Max
```

If it shows a different email: `claude logout && claude login` and repeat
until it shows the correct identity. Do not proceed past this step with
the wrong account - heartbeat charges will go to the wrong billing account.

**Done when:** `claude auth status` returns the installer's own email.

---

## Step 7 - Bootstrap the chassis

This step hydrates `.mcp.json`, `CLAUDE.md`, `HEARTBEATS.md`, seeds memory,
and validates the install config. Idempotent - safe to re-run.

```bash
cd ~/behalfbot
docker compose run --rm --env-file .env.compose chassis bootstrap
```

Watch the output for any `FAIL:` lines. Common issues:
- `INSTALL_PROFILE.md missing` - you need to author or copy the installer's
  profile first (see `customer/<installer>/` branch in the chassis repo)
- `chassis.config.yaml missing` - same; see `customer/<installer>/`
- `webhook URL not set` - add `<INSTANCE>_<KEY>_WEBHOOK_URL` to instance .env

**Done when:** bootstrap output ends with `Bootstrap complete` and no
`FAIL:` lines.

---

## Step 8 - Start the dispatcher

```bash
cd ~/behalfbot
docker compose --env-file .env.compose up -d chassis
```

Verify first tick:

```bash
# Within ~16 minutes of starting, one tick should run
docker compose logs chassis --tail 50
```

Expected log pattern:

```
[entrypoint HH:MM:SS] dispatcher loop starting - tick=900s, CHASSIS_HOME=/app/customer
[entrypoint HH:MM:SS] first-boot-announce.sh: emitted bot user ID to ops channel
[HH:MM:SS] === Dispatcher tick ===
[HH:MM:SS] No heartbeats registered - HEARTBEATS.md needs at least one ## block
```

The "no heartbeats" message is expected if bootstrap populated only the
template. Register at least the morning-briefing heartbeat in
`~/behalfbot/HEARTBEATS.md` (mounted into the container - edit on host).

**Done when:** `docker compose ps` shows chassis Up; dispatcher ticks appear
in logs every 15 minutes.

---

## Step 9 - Wire the Discord channels surface

The interactive `claude --channels` process runs OUTSIDE the container in a
host tmux session. It requires a PTY; wrapping with `script -q -c` provides
one in non-interactive shells (V1 lesson from installer-1 install).

```bash
# Start a named tmux session on the host
tmux new -d -s <installer>-discord \
  "cd ~/behalfbot && source .env && \
   script -q -c 'claude --channels plugin:discord@claude-plugins-official \
   --dangerously-skip-permissions' /dev/null"
```

Monitor the tmux session to confirm the channels plugin connects:

```bash
tmux attach -t <installer>-discord
# Should see: "channels plugin connected" or similar
# Ctrl-b d to detach
```

> **Note:** `claude --channels` requires `bun` to be installed on the host.
> The chassis Docker image has bun baked in, but the host-side `claude`
> invocation uses the host's bun. Install on the host if missing:
> `curl -fsSL https://bun.sh/install | bash`

Allow the Discord bot in the install channel (run inside the tmux session
or in a new `docker compose run --rm chassis claude` shell):

```
/discord:access group add <channel_id> --no-mention --allow <installer_discord_id>,<sean_discord_id>,<jaxbot_discord_id>
```

**Done when:** sending a test message in `#<installer>-setup` gets a response
from the bot within a few seconds.

---

## Step 10 - Run smoke tests

```bash
cd ~/behalfbot
docker compose run --rm --env-file .env.compose chassis smoke-test
```

Expected: every core check PASS or SKIP. FAIL is a blocker.

Per-plugin checks fire only for enabled plugins. Typical first-install
result: GitHub PASS, memory PASS, dispatcher PASS, discord-webhook PASS,
postgres PASS. Second-brain and plugin checks may SKIP if not fully
configured yet.

**Done when:** `smoke-test` exits 0 (no FAIL lines).

---

## Step 11 - Register morning-briefing heartbeat

Edit `~/behalfbot/HEARTBEATS.md` on the HOST (the container bind-mounts it):

```markdown
## morning-briefing

schedule: daily 08:00 Europe/Lisbon
condition: always
prompt_file: chassis/skills/morning-briefing.md
model: claude-opus-4-5
budget_usd: 0.50
criticality: normal
gather:
  command: echo '{"count":1}'
  parse: .count > 0
```

The container picks up the change on the next 15-minute tick without restart.

**Done when:** the dispatcher log shows `morning-briefing` evaluated (FIRE
or SKIP depending on time of day).

---

## Step 12 - First-heartbeat smoke test (3-day soak)

Success criterion: the morning-briefing message lands in the installer's
`#<installer>-briefings` Discord channel for 3 consecutive mornings, built
from real gathered data.

- Day 1: confirm it fires and the message looks sensible
- Day 2: confirm no auth failures (OAuth token refresh)
- Day 3: sign off smoke test; hand SSH access over to installer

During the 3-day soak:
- Leave Sean+${ASSISTANT_NAME} SSH access in place
- Watch `docker compose logs chassis --tail 100` for errors
- If auth fails mid-day: `claude logout && claude login` on the host, then
  `docker compose restart chassis` (the bind-mounted `~/.claude/` refreshes)

**Done when:** 3 clean morning-briefing fires; `state/first-boot-announced.json`
confirms first-boot announce ran; installer confirms briefings look right.

---

## Common issues

### Dispatcher starts but no heartbeats fire

Check `~/behalfbot/HEARTBEATS.md` has at least one `## <name>` block with
`schedule:`, `condition:`, and `prompt_file:` fields. The dispatcher skips
files that only contain comments or the template header.

### `claude --channels` exits immediately

1. Confirm `MESSAGE CONTENT INTENT` is ON in Discord Developer Portal (Step 0)
2. Confirm `bun` is installed on the HOST (`bun --version`)
3. Run via `script -q -c` wrapper - plain `tmux` without PTY wrapping causes
   silent exit on some Linux kernels
4. Check `DISCORD_BOT_TOKEN` is set in `~/behalfbot/.env` (instance secrets)

### Hydrate-env: "VW login failed"

- Verify VW is reachable from inside the container: `docker compose exec vaultwarden curl -s localhost:80/` should return a VW HTML page
- Verify the VW master account email matches exactly - NOT the agent identity email
- Check VW host port mapping: `VAULTWARDEN_HOST_PORT` in `.env.compose`
- `rbw` uses HTTP without cert verification against the internal Compose network; if you moved VW behind HTTPS, set `RBW_URL=https://...` accordingly

### Wrong Claude account after `claude login`

Run `claude auth status`. If it shows Sean's or ${ASSISTANT_NAME}'s email: the OAuth flow
opened on the wrong device. Run `claude logout && claude login` and ensure
the browser that opens is the installer's browser (not Sean's forwarded
display). The installer's email must own the subscription billing.

### Container bind-mount permission denied

Match `INSTALLER_UID` / `INSTALLER_GID` in `.env.compose` to the result of
`id -u && id -g` on the host for the user who owns `~/behalfbot/`. Mismatch
causes the container's `chassis` user to create root-owned files in the
bind-mount that the host user can't read.

---

## References

- `docs/installer-homework.md` - installer pre-staging checklist
- `docs/containerization.md` - architecture + VW hydration model
- `docs/discord-intake.md` - channels plugin vs heartbeat polling
- `docs/mcp-setup.md` - per-MCP wiring reference
- `docs/installer-vw-template.md` - canonical VW item names
- `docs/install-journals/` - per-installer learnings + baked-vs-runbook index
- `docs/LESSONS_FROM_V1.md` - full 37-lesson empirical record
- `<v1-reference-install>#537` - containerization tracking issue
- `<v1-reference-install>#538` - installer-2 install ticket
