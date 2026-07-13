# Credential bake — `.env` → `.env.baked` for the chassis container

How the chassis container gets its secrets at runtime, why the canonical pattern is what it is, and how to re-bake `.env.baked` when credentials rotate.

(Companion to `docs/hydration.md` — that doc covers install-time bootstrap of `INSTALL_PROFILE.md` + `chassis.config.yaml`; this one covers credential hydration into the runtime container.)

## The two patterns

The chassis lives at the intersection of two competing constraints:

1. **Vaultwarden-as-source-of-truth.** Per the chassis security model, credentials live in the installer's Vaultwarden instance. They are pulled into runtime env on demand, never long-lived on disk in plaintext.

2. **Container runtime reliability.** The chassis dispatcher fires gather scripts every 15 minutes. Each invocation can't afford a 5-15s Vaultwarden round-trip per secret. The container can't crash every time Vaultwarden restarts or rotates a session token.

Two patterns emerge with different trade-offs.

### Pattern A — host-side, RAM-only

The host's `.env` ends with a sourced hydration block that calls `scripts/hydrate-from-vaultwarden.sh` (or installer-equivalent). Any process that `source`s `.env` triggers the hydration → `bw get item <name>` calls land secrets in that shell's process env → secrets evaporate when the process exits.

```
$CHASSIS_HOME/.env  (literal config + sourced hydration block)
    │
    ├── (top of file)         literal KEY=value lines
    └── (bottom of file)      if [[ ${BW_HYDRATE:-1} != "0" ]]; then
                                  source scripts/hydrate-from-vaultwarden.sh
                              fi
```

Win: zero VW-sourced secrets at rest in `.env` itself. Loses (vs Pattern B): every caller pays VW unlock latency + reliability tax.

### Pattern B — container-side, baked at install

A separate file `.env.baked` is generated ONCE (at install bootstrap and on each credential rotation) by sourcing the hydration-aware `.env` and capturing the resulting env vars as literal `KEY=value` lines. The chassis container reads `.env.baked` at boot via compose's `env_file:` directive.

```
$CHASSIS_HOME/.env  ─[bake-env.sh]─►  $CHASSIS_HOME/.env.baked  ─[docker compose env_file:]─►  container process env
   (host, hydrating)                     (literal)                                            (read once at boot)
```

Win: container never hits Vaultwarden at runtime. Loses (vs Pattern A): literal secrets at rest in `.env.baked`.

## Which pattern does the chassis use?

**Both, on different sides of the host/container boundary.**

- Host-side scripts (`scripts/*.sh`, `scripts/*.py` that run as the install's user) source `.env` directly → trigger Pattern A on demand. The host has `bw` CLI configured + an unlocked session OR a `BW_PASSWORD` env var pre-set.
- Container reads `.env.baked` via `env_file:` at boot → Pattern B. The container intentionally has no `bw` CLI configured, no `BW_SESSION`, and can't reach the host's VW socket. By design.

## The `.env.baked` file

### Contents

Literal `KEY=value` lines, one per line. No bash conditionals, no hydration block, no shell logic. The chassis compose stack's `env_file:` directive parses this file at container start — Docker's parser is strict (`KEY=value` only, no logic).

### Generation

Run `chassis/scripts/bake-env.sh` on the HOST whenever:

- Bootstrapping a fresh install (after `.env` is populated)
- A credential rotates (Discord bot token reset, GitHub PAT regenerated, etc.) — `.env.baked` must be re-baked to pick up the new value
- A literal var in `.env` changes (adding a new module config, swapping a webhook URL)
- The container fails to start with a missing-env-var error — usually means `.env.baked` is stale relative to current `.env`

Usage:

```bash
CHASSIS_HOME=/path/to/install bash chassis/scripts/bake-env.sh

# Dry-run (print what would be written, don't touch .env.baked):
CHASSIS_HOME=/path/to/install DRY_RUN=true bash chassis/scripts/bake-env.sh
```

Output: `$CHASSIS_HOME/.env.baked` at mode `0600`.

### Storage + lifecycle

- **Mode**: `0600` (owner-only read/write)
- **Location**: `$CHASSIS_HOME/.env.baked` (host filesystem, not in chassis repo)
- **Gitignore**: matched by `.env.*` glob in the install's `.gitignore` — never committed
- **Backups**: NOT in S3 backups. `chassis/scripts/backup-to-s3.sh` explicitly copies only `.env` (renamed `env.txt`, age-encrypted before upload). `.env.baked` is local-only.
- **Lifetime**: regenerated on each `bake-env.sh` run. Bootstrap should regenerate on install.

## Security posture

`.env.baked` is plaintext secrets at rest. Honest reading of the trade-off:

### What the hardware layer provides (Apple Silicon)

M-series Macs encrypt the internal SSD at the Secure Enclave level regardless of FileVault state. An attacker who steals the Mac Mini and removes the drive gets nothing readable — the disk is paired to the chip. This means the cold-boot / stolen-laptop threat model is largely mitigated below the FileVault layer.

For Linux installs (installer-1's `fatboy`, Ben's Ozzy on baremetal Linux, etc.), the at-rest story is different — distro-level disk encryption (LUKS, etc.) governs. Same Pattern-A-vs-B trade-off applies but the hardware floor isn't automatic.

### What's actually different between A and B in practice

| Threat | Pattern A | Pattern B |
|---|---|---|
| Drive removed + read on another machine (Apple Silicon) | Protected (HW encryption) | Protected (same) |
| Stolen Mac Mini, attacker boots it | Apple login + master pass needed to unlock VW | Apple login needed; baked file readable post-login |
| Attacker with file-read at install user's uid | Master pass + VW blob both at rest | Master pass + VW blob + baked file all at rest |
| Compromised dispatcher process | All secrets in RAM via env | All secrets in RAM via env (no delta) |
| Forensic disk imaging post-shutdown | Encrypted (HW), needs unlock chain | Same |

The delta lives in the "attacker with file-read but no master password" cell. In practice that's a narrow threat — most realistic adversaries (root, RCE, persistent malware) have all of those simultaneously.

### What WOULD genuinely improve the posture

In rough order of leverage:

1. **Enable FileVault** if compatible with the install's headless-reboot constraint. Adds a user-pass gate on top of hardware encryption. Trade-off: FileVault-enabled Macs need user login at boot, which breaks unattended SSH-after-power-loss recovery. Many homelab installs accept this trade for the user-pass-gate benefit. Consumer macOS has no clean "auto-unlock after crash reboot" path.

2. **Don't keep VW master pass at rest** between rotations. Force interactive entry at bake time. Loses Pattern A's "secrets only in RAM" claim if master pass is also at rest.

3. **Bot-account separation.** Required by chassis design — `DISCORD_BOT_TOKEN` is the installer's bot, not the installer's personal account. `GITHUB_PAT` is the installer's bot account. Blast radius of a leaked `.env.baked` is bot identity only.

4. **Rotation cadence.** Bot tokens can rotate in seconds. A leaked token detected within hours is mostly recoverable.

5. **Audit log.** Wrap `bw get` + `.env` read calls to log access trails. Detection > prevention for this threat class.

### Hard rules

- `.env.baked` MUST NOT be committed (the install's `.gitignore` should match `.env.*`)
- `.env.baked` MUST NOT be in S3 backups (chassis backup script does NOT include it)
- `.env.baked` MUST stay mode `0600` (bake script chmods)
- `.env.baked` MUST live under `$CHASSIS_HOME/`

### Dispatcher-toxic var blocklist

`bake-env.sh` explicitly EXCLUDES a small set of vars from the baked output, regardless of whether they appeared in the source `.env` or were exported in the bake-time shell. These are vars that would override Claude Code's OAuth credentials chain or otherwise corrupt dispatcher behavior if present in the container's process env at runtime.

Currently blocked (see `chassis/scripts/bake-env.sh` `TOXIC_VARS_REGEX`):

- `ANTHROPIC_API_KEY` — would route `claude -p` through PAYG billing instead of OAuth/subscription
- `ANTHROPIC_AUTH_TOKEN` — same class
- `ANTHROPIC_BASE_URL` — could redirect API calls to an alternate endpoint
- `ANTHROPIC_BETA` — could enable unstable beta features that change response shapes
- `CLAUDE_CODE_OAUTH_TOKEN` — managed by Claude Code itself in `~/.claude.json`; setting it via env conflicts with that managed state
- `CLAUDE_PROJECT_DIR` — chassis sets this per-invocation; baked value would override and break per-heartbeat cwd handling

If any of these are present at bake time, `bake-env.sh` emits a `WARN: dispatcher-toxic var 'X' present at bake time; excluded from .env.baked` line to stderr. Operators sometimes export these intentionally for a different purpose (e.g. local Claude Code testing); the WARN makes it visible that the bake stripped them rather than silently losing the var.

To bake a var that the blocklist excludes anyway (e.g. you genuinely want a PAYG `ANTHROPIC_API_KEY` for some non-chassis use case), inject it via a different mechanism — directly edit `.env.baked` post-bake, or use a separate `.env.payg` file mounted only into the consuming container.

### Incident reference

Concrete failure mode this blocklist prevents: 2026-05-22, Sean's `scrollinondubs/new-jaxity` chassis install. A stale `ANTHROPIC_API_KEY` (an old PAYG key) had been baked into `.env.baked` at some past bake — origin unclear (not in host shell at audit time, not in any rc file, not in `.env`; most likely leaked from a transient env on a past bake run). After a routine container restart, the dispatcher's `claude -p` invocations started failing with `Invalid API key` (the dispatcher's `unset ANTHROPIC_API_KEY` only affects its own shell, not new `docker exec` sessions or freshly-spawned subprocess env). Each failing `claude -p` consumed ~40 minutes (20-min internal default + 20s retry wait + 20-min retry). 3 sequential heartbeats failed → dispatcher cycle blocked for 2.5 hours → Sean's BFL photos sat unprocessed in `#<health>` for 3 hours.

Manual mitigation was `sed -i '/^ANTHROPIC_API_KEY=/d' .env.baked` + container recreate. Permanent fix is this blocklist.

## Smoke-testing a fresh bake

```bash
# 1. Re-bake (or initial bake)
CHASSIS_HOME=/path/to/install bash chassis/scripts/bake-env.sh

# 2. Verify file exists at mode 0600 + gitignored
ls -la $CHASSIS_HOME/.env.baked
git -C $CHASSIS_HOME check-ignore .env.baked

# 3. Recreate the container so it re-reads the env.
#    Always via the wrapper - it pins --env-file, the project name, CUSTOMER_HOME and
#    the compose-file locations. Bare `docker compose` gets at least one of them wrong.
#    (The old `-f chassis/docker-compose.yml` form is dead: that subtree was dropped in
#    behalfbot#136 and the compose file now lives in the chassis repo.)
bash chassis/scripts/compose.sh up -d --force-recreate chassis

# 4. Verify a critical secret is visible inside the container
docker exec behalfbot sh -c \
  'echo "DISCORD_BOT_TOKEN: ${DISCORD_BOT_TOKEN:+set}${DISCORD_BOT_TOKEN:-MISSING}"'

# 5. Spot-check a heartbeat that depends on the freshly-rotated credential
docker exec -e CHASSIS_HOME=/app/customer behalfbot \
  bash -c 'cd /app/customer && scripts/gather-<name>.sh'
```

## Related

- `chassis/scripts/bake-env.sh` — the bake script this doc explains
- `chassis-compose.override.yml` `env_file:` directive — where the container picks up `.env.baked`
- `chassis/scripts/backup-to-s3.sh` — the encrypted-S3 backup script (`.env` only, NOT `.env.baked`)
- `docs/hydration.md` — install-time bootstrap walkthrough (orthogonal to this doc)
- `<v1-reference-install>#603` — the issue that surfaced the need for this doc
- `<v1-reference-install>#624` — the chassis-compose.override.yml change that moved from "bind-mount .env.baked over /app/customer/.env" (broken on Lima/virtiofs) to compose's `env_file:` directive (works)
