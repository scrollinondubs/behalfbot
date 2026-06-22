# Disaster recovery

> "My hardware died. How do I get my install running again on new hardware?"

This guide walks you through rebuilding a Behalf.bot install from scratch on a fresh machine using only:

1. Your **install repo** on GitHub (the per-customer repo described in [`per-customer-repo-pattern.md`](per-customer-repo-pattern.md))
2. The **chassis repo** on GitHub (`scrollinondubs/behalfbot`)
3. Your **secret store** (Vaultwarden, 1Password, Bitwarden, etc.)
4. A **postgres backup** (from your routine `pg_dump` / S3 backup target)

If you have these four pieces, your install is recoverable. If you don't, parts of state will be irretrievable. The inventory table at the bottom tells you exactly what each piece holds.

This document covers the **catastrophic hardware-loss scenario** (Mac mini bricked, laptop stolen, cloud VM terminated). For non-destructive scenarios (chassis upstream repo renamed, in-place chassis re-anchor), see [`SELF_INSTALL.md` § "Migrating an existing install to a re-anchored chassis"](SELF_INSTALL.md#migrating-an-existing-install-to-a-re-anchored-chassis).

---

## What gets rebuilt vs what you restore

| Layer | Rebuild source | Restoration | Lost if missing |
|-------|---------------|-------------|-----------------|
| **Customizations** (`HEARTBEATS.md`, `CLAUDE.md`, `skills/`, `scripts/`, `plugins/`, `scheduled-tasks/`, `chassis.config.yaml`) | Your install repo on GitHub | `git clone` | History since last push |
| **Chassis code** (`chassis/`) | `scrollinondubs/behalfbot` on GitHub | `git clone` or `git subtree pull` | — (it's public, always recoverable) |
| **`.env` secrets** | Your secret store | Manual restore from Vaultwarden / 1Password / etc. | If you don't have a secret store: everything (API keys, DSN passwords, OAuth tokens, webhook URLs, Vaultwarden admin token) |
| **`.mcp.json`** | Your install repo on GitHub | `git clone` (it lives at repo root) | History since last push |
| **Postgres data** (chassis state, vector index, plugin state, BFL data, dating profiles, briefings index) | Your postgres backup target (`pg_dump` to S3, scheduled `pg-backup.sh`, etc.) | `pg_restore` into the new postgres container | Everything since last backup tick |
| **Vaultwarden data** (if you use the chassis-bundled VW; skip if you externalize like Jax does to `vault.grid7.com`) | Vaultwarden backup target | Volume restore | Everything since last backup tick |
| **Claude Code OAuth credentials** (`~/.claude/`) | Re-authenticate on the new machine | `claude login` after install | — (you just log in again) |
| **`state/`, `logs/`, `briefings/`, `memory/`, `backups/`** | Gitignored, not in repos | Whatever backup target you wired (typically none) | History; rebuild progressively from new heartbeat ticks |

**Rule of thumb:** GitHub holds your code and config history. Your secret store holds your secrets. Your postgres backup holds your operational state. Anything you didn't back up to one of those three is gone.

---

## Step-by-step rebuild

### 0. Prerequisites on the new machine

- Docker (Docker Desktop on macOS, Docker Engine + Compose plugin on Linux)
- `git` + `gh` (GitHub CLI)
- Your secret store CLI (e.g. `rbw` for Vaultwarden, `op` for 1Password, `bw` for Bitwarden)
- Claude Code CLI (`claude`)

### 1. Clone your install repo

```bash
# Adjust the destination dir to match where your old install lived
# (Jax: ~/.behalfbot; typical Linux installer: ~/<install-name>)
git clone https://github.com/<your-namespace>/<install-name>.git ~/<install-name>
cd ~/<install-name>
```

### 2. (Overlay-mount layout only) Clone the chassis repo separately

The default install layout (Lakoff/Marc/Toby) vendors chassis inside the install repo at `chassis/` via git subtree — skip this step.

The **overlay-mount layout** (Jax #136) bind-mounts a separate chassis clone read-only over the customer dir. If your old install used this layout, clone the chassis next to your install:

```bash
git clone https://github.com/scrollinondubs/behalfbot.git ~/behalfbot
```

Check `chassis-compose.override.yml` in your install repo — if it contains a `${HOME}/behalfbot:/app/customer/chassis:ro` volume line, you're on the overlay-mount layout.

### 3. Restore `.env` from your secret store

The `.env` file is the single most sensitive artifact in an install. Restore it from your secret store, NOT from any GitHub repo (it's gitignored for good reason).

```bash
# Vaultwarden example
rbw get <install-name>-env | base64 -d > .env

# 1Password example
op document get <install-name>-env > .env

# Pasted from secret-store UI
$EDITOR .env
```

Validate that critical env vars are populated:

```bash
grep -E "^(POSTGRES_PASSWORD|VAULTWARDEN_ADMIN_TOKEN|INSTALLER_UID|CUSTOMER_HOME|CUSTOMER_CLAUDE_DIR)" .env
```

Then bake the literal version the container reads:

```bash
bash chassis/scripts/bake-env.sh   # produces .env.baked from .env
```

### 4. Restore postgres data

How you restore postgres depends on what backup target you wired. Three common scenarios:

**A. `pg-backup.sh` to S3 (chassis-shipped):**

```bash
aws s3 cp s3://<your-backup-bucket>/postgres/<install-name>/latest.dump.gz ~/restore.dump.gz
gunzip ~/restore.dump.gz
docker compose up -d postgres                      # start postgres alone first
docker compose exec postgres psql -U chassis -d chassis -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
docker compose exec -T postgres pg_restore -U chassis -d chassis < ~/restore.dump
```

**B. Local `pg_dump` to disk (if you had one):**

```bash
docker compose up -d postgres
docker compose exec -T postgres psql -U chassis -d chassis < ~/path/to/your.dump
```

**C. No backup at all:** start with an empty database. Rebuild state progressively. Heartbeats and plugins re-populate operational state over time, but historical content (briefings index, ingested second-brain content, BFL history, etc.) is lost.

### 5. Restore Vaultwarden data (skip if you externalize VW)

If your install uses the chassis-bundled Vaultwarden:

```bash
# Restore from whatever backup target you wired
docker volume create behalfbot_vaultwarden-data
docker run --rm -v behalfbot_vaultwarden-data:/restore -v ~/vw-backup:/backup busybox \
    sh -c "cd /restore && tar xzf /backup/vaultwarden-latest.tgz"
```

If you externalize VW (Jax's setup uses `vault.grid7.com`), update `chassis-compose.override.yml` to scale the VW service to 0 (likely already there from your old install).

### 6. Run bootstrap

```bash
CHASSIS_HOME=$(pwd) bash bootstrap.sh
```

This re-renders LaunchAgent plists (macOS), systemd units (Linux), and customer-side scripts against the new hostname. It does NOT touch your `.env` or git state.

### 7. Start the stack

```bash
docker compose up -d
```

If you're on the overlay-mount layout:

```bash
docker compose -f docker-compose.yml -f chassis-compose.override.yml up -d
```

### 8. Re-authenticate Claude Code

```bash
claude login   # follow the OAuth flow
```

For containerized installs, the Claude credentials sync from `${CUSTOMER_CLAUDE_DIR}` (bind-mounted at `/home/chassis/.claude` inside the container). Verify the bridge sync LaunchAgents picked up the new machine (`launchctl list | grep claude-bridge`).

### 9. Smoke test

```bash
# Dry-run the dispatcher to see which heartbeats would fire
DRY_RUN=true bash chassis/scheduled-tasks/heartbeat-dispatcher.sh

# Watch container logs
docker compose logs -f chassis
```

The first real dispatcher tick should land in your ops channel (`DISCORD_OPS_CHANNEL_ID` from your `.env`) within 15 minutes.

### 10. Re-key any tokens that crossed a trust boundary

If your old machine was potentially compromised (stolen, sold without wipe, etc.), rotate every secret it held:

- GitHub PATs and SSH keys (`gh auth refresh`; revoke old keys at github.com/settings/keys)
- API keys for every third-party service (Anthropic, OpenAI, Notion, Discord webhook URLs, Tailscale auth keys)
- Postgres passwords (rotate via `ALTER USER chassis WITH PASSWORD '...'` + update `.env`)
- Vaultwarden admin token (rotate per VW docs)
- Discord bot token (regenerate in Discord Developer Portal)

If the old machine was lost / failed cleanly (hardware death, no compromise), this step is optional but still a hygiene win.

---

## What you should be doing TODAY so this works tomorrow

Audit your install against this checklist. If any row is "no", fix it before you need to recover:

- [ ] **Install repo pushed to GitHub** — Run `git status -uno` in your install dir. If commits ahead of origin, push.
- [ ] **`.env` stored in your secret store** — Open Vaultwarden / 1Password / etc. and confirm there's an `<install-name>-env` entry that matches the live `.env`.
- [ ] **Postgres backup running** — Check `HEARTBEATS.md` for a `pg-backup` entry. Confirm last successful backup timestamp.
- [ ] **Vaultwarden externalized OR its volume backed up** — Either you don't use the chassis VW (like Jax), or you have a backup target for `vaultwarden-data` volume.
- [ ] **You know which install layout you're on** — Vendored-subtree or overlay-mount. Documented somewhere (this doc, your install's `docs/`, or your memory).
- [ ] **You can find the bootstrap command without grep** — `CHASSIS_HOME=$(pwd) bash bootstrap.sh` from your install repo root.

---

## Cross-references

- [`per-customer-repo-pattern.md`](per-customer-repo-pattern.md) — the customer repo pattern (initial bootstrap)
- [`SELF_INSTALL.md`](SELF_INSTALL.md) — full self-install walkthrough (non-destructive re-anchor in § "Migrating an existing install to a re-anchored chassis")
- [`containerization.md`](containerization.md) — chassis container architecture (what's bind-mounted vs baked)
- [`credential-bake.md`](credential-bake.md) — `.env` → `.env.baked` mechanics
