# Containerization - chassis Docker image + Compose stack

> **Why this exists.** The V1 bare-metal install (installer #1, 2026-05-06 to 2026-05-07, journaled in `LESSONS_FROM_V1.md`) took ~6-8 hours of Sean+${ASSISTANT_NAME} SSH-driven hand-tweaking. The friction concentrated in three places: OS-level dep mismatches (Debian's `node` is old, `python3` is 3.11 not 3.12, `bun` isn't packaged), cross-platform script paths (`gtimeout` is macOS-only, `ollama` was assumed at a Homebrew path), and credential plumbing (bw CLI rejects self-hosted Vaultwarden over HTTP). Installer #2 has a 2026-05-18 demo deadline. Hand-tweaking again loses us the demo. This doc is the containerization PR's accompanying runbook.

## North star

A chassis install on a fresh Linux box (or Mac Mini) should be: pull image → write two `.env` files → `docker compose up -d` → wait for postgres → run `docker compose run --rm chassis bootstrap`. Time-to-first-heartbeat: under two hours, most of it waiting for `claude --channels` auth.

## The three-service stack

`docker-compose.yml` ships three services on an internal bridge network. Per the MVP-base scope (`docs/install-mvp-scope.md`), all three are required for day-one delight.

| Service       | Image                                        | Role                                                                          |
|---------------|----------------------------------------------|-------------------------------------------------------------------------------|
| `chassis`     | `ghcr.io/scrollinondubs/behalfbot`   | Dispatcher loop + plugin runtime. Long-running, restart=unless-stopped.       |
| `postgres`    | `postgres:16-alpine`                         | Durable state. Per Sean's "Postgres from start" call - avoids SQLite migration|
| `vaultwarden` | `vaultwarden/server:1.32.5`                  | Credential vault. Localhost-only by default; Tailscale Funnel optional.       |

The customer's `~/behalfbot/` bind-mounts onto `/app/customer` in the chassis container. That directory holds everything customer-specific: `.env`, `.mcp.json`, `chassis.config.yaml`, `INSTALL_PROFILE.md`, `CLAUDE.md`, `HEARTBEATS.md`, `data/`, `state/`, `briefings/`, `logs/`, `memory/`, plugin overlays. The image is generic - no customer secrets, no customer config.

`~/.claude` on the host bind-mounts into the container as well, so the OAuth credential survives container restarts and is shared with any host-side Claude session pointed at the same customer dir.

## Image contents

```text
/app/chassis/       - chassis source (dispatcher, hook layer, skills, scripts) - read-only at runtime
/app/plugins/       - plugin source (bfl, dating, angel-protocol, whatsapp)    - read-only at runtime
/app/docker/        - entrypoint script
/app/customer/      - BIND MOUNT from host - all per-customer state lives here
/home/chassis/.claude - BIND MOUNT from host - OAuth credentials, plugin cache
```

Pre-installed tooling (image-baked):

- Python 3.12 + `uv` + `uvx`
- Node 22 LTS + `npm`
- `bun` (hard prereq for `claude --channels` plugin per V1 install lesson)
- Claude Code CLI (`@anthropic-ai/claude-code`, latest)
- `rbw` (Rust Bitwarden CLI - works over HTTP, unlike `bw` which enforces HTTPS)
- `ffmpeg`, `sqlite3`, `jq`, `curl`, `git`, `zsh`, `tini`, `pinentry-tty`

Non-root: container runs as user `chassis` (uid/gid 1000 by default; override via build arg).

## Vaultwarden secret-injection model

Sean's open question on PR #25 plan: "investigate how we are using Vaultwarden to inject secrets and square that with this approach."

**Decision: VW is a bootstrap-time concern, not a runtime concern.** The dispatcher loop never reaches Vaultwarden. Secrets flow:

1. Installer (or ${ASSISTANT_NAME} SSH-driven) runs `docker compose run --rm chassis hydrate-env` once.
2. That invocation runs `chassis/scripts/hydrate-env-from-vw.sh` inside the container, which uses `rbw` (HTTP-compatible) to pull every required secret from the Vaultwarden service running alongside in the same Compose stack.
3. Output is written to `/app/customer/.env` on the bind-mount.
4. From then on, the dispatcher loop sources `/app/customer/.env` at startup and on every tick. No VW contact at runtime.

Why `rbw` over `bw`:

- `bw` v2026 enforces HTTPS. Self-hosted Vaultwarden on Tailscale is reachable over HTTP for tailnet members; forcing HTTPS adds cert-rotation friction that buys no security on a private network.
- `rbw` reads master password from `RBW_MASTER_PASS` via a pinentry stub. This matches the pattern that already works for Sean's <v1-reference-install> install (`feedback_keys_via_vaultwarden.md`).

Why bootstrap-time only:

- Runtime VW contact would couple every heartbeat to vault availability. A 30-second vault timeout would block a 5-second briefing fire.
- Rotation isn't continuous. Re-hydrate on demand: `docker compose run --rm chassis hydrate-env`. Idempotent - doesn't clobber values not present in vault.
- Dashboard hydration pattern in Sean's <v1-reference-install> (PR #463, `feedback_keys_via_vaultwarden.md`) is hydrate-at-startup. We extend that to hydrate-at-bootstrap. Same principle; coarser cadence.

The compose stack runs Vaultwarden on the internal bridge network. The chassis container reaches it via the service hostname `vaultwarden`. From the host, Vaultwarden is bound to `127.0.0.1:8222` only - never exposed to the internet directly. If remote vault access is needed, the customer fronts it with Tailscale Funnel or Cloudflare Tunnel; the compose stack does not terminate public TLS.

## Heartbeat dispatcher: cron-inside-container

Sean's call on PR #25 plan: "inside [container] for portability as long as we're not shooting ourselves in the foot."

The entrypoint runs the dispatcher in a `while true; do dispatcher; sleep 900; done` loop. No `cron` daemon, no `systemd` inside the container - fewer moving parts, smaller attack surface, identical observable behavior. Each loop iteration touches `/tmp/dispatcher.alive`, which the container healthcheck monitors with a 20-min stale threshold.

Trade-offs we accept:

- **One container = one cadence.** All heartbeats fire on the same 15-min tick. The dispatcher's per-heartbeat schedule (`every 15m`, `daily 08:00`, etc.) handles per-heartbeat cadence within that tick - same as V1.
- **Restart = full re-tick.** If the container crashes mid-tick, `restart: unless-stopped` brings it back; the next tick reads dispatcher state from `/app/customer/scheduled-tasks/heartbeat-state.json` and proceeds.
- **OAuth refresh ≠ dispatcher concern.** The 8h Claude Code OAuth token (LESSONS_FROM_V1.md #3, #9) refreshes via the host-side `~/.claude/.credentials.json` bind-mount - same file Sean's V1 5:30 AM respawn pattern touches. The container doesn't need its own pre-emptive respawn; the host pattern still applies. If the OAuth token dies, the next dispatcher tick fails its `claude -p` invocation, the gather script logs to ops webhook, the installer re-auths.

## `claude --channels` Discord intake: out of scope for v1 image

The interactive `claude --channels` process (bidirectional Discord intake) is **not** auto-started by the chassis container. Reasons:

- It requires a PTY. `script -q -c '...' /dev/null` wraps fine, but supervising it inside the dispatcher container conflicts with the dispatcher's clean SIGTERM semantics.
- V1 install uses a host tmux session pointing at the chassis dir. V2 containerized installs will follow the same pattern initially.
- Containerizing channels is its own design problem (PR follow-up). The MVP for May 19 demo doesn't depend on it - webhook-based Discord posts cover briefing delivery and ops alerts.

Once `claude --channels` lands inside a container, it ships as a second Compose service (`chassis-discord`) sharing the same bind-mounts, with `tty: true` and `stdin_open: true`.

## Build + publish

GitHub Actions workflow at `.github/workflows/docker-publish.yml`:

- Trigger: push to `main`, tag `v*`, PR touching `Dockerfile`/`docker/**`/`chassis/**`/`plugins/**`/`bootstrap.sh`/the workflow itself.
- Platforms: `linux/amd64` + `linux/arm64`. V1 bare-metal install is amd64; Sean's Mac Mini and Cloudflare-based installs are arm64.
- Tags: `latest` on default branch, `vX.Y.Z` + `vX.Y` on semver tags, `sha-<short>` on every build, `pr-<n>` on PR builds (build-only, no push).
- Registry: `ghcr.io/scrollinondubs/behalfbot`.
- Cache: GitHub Actions cache (`type=gha`, mode=max). First build ~10 min cold; subsequent ~2-3 min with cache hits.

## Driving the stack on a live install - always `chassis/scripts/compose.sh`

Once an install is bootstrapped and baked, **never run bare `docker compose` against it.** Use the wrapper:

```bash
bash chassis/scripts/compose.sh up -d --force-recreate chassis
bash chassis/scripts/compose.sh logs -f chassis
bash chassis/scripts/compose.sh ps
bash chassis/scripts/compose.sh config     # dry-run: print the fully resolved stack
```

It resolves the compose files, `--env-file`, project name and `CUSTOMER_HOME` for you, and refuses to run when any of them are wrong. Each guard is there because of a real outage, not a hypothetical:

| Guard | What bare `docker compose` does instead |
|---|---|
| `--env-file=.env.baked` | Reads `.env`, whose Vaultwarden hydration block does not resolve at compose time. The container silently comes up with secrets missing and subsystems fall back to defaults. Burned 2026-05-30: container came up without `OLLAMA_HOST`, the morning briefing quietly degraded, and only a human noticing caught it. |
| Compose file lives in the **chassis repo** | The vendored `chassis/` subtree was dropped in behalfbot#136 - `chassis/` under an install is now an empty bind-mount point. Scripts still looking for `$CUSTOMER_HOME/chassis/docker-compose.yml` just error out. On the reference install that went unnoticed for six weeks, because the container had been running since 2026-06-02 and was only ever restarted, never recreated. It was **not reproducible via the documented path** - had it died, it would not have come back. |
| Pinned `-p` project name | Without `-p`, compose derives the project from the working directory. From the wrong directory, `up --force-recreate` stands up a **second** set of containers next to the live ones instead of replacing them. |
| Exported `CUSTOMER_HOME` | Compose interpolates it into the volume paths, but `bake-env.sh` deliberately strips it from `.env.baked` (it is a host path; inside the container it is authoritatively `/app/customer`). So `--env-file` cannot supply it. Without the export, even `docker compose config` dies with `required variable CUSTOMER_HOME is missing a value`. |
| `.env` newer than `.env.baked` | Means someone edited `.env` and never re-baked. Coming up in that state runs values nobody set. The wrapper refuses; re-run `bake-env.sh` first. |

Everything is derived, so the wrapper normally needs no configuration. Override when you need to:

- `CHASSIS_REPO` - the chassis clone holding `docker-compose.yml`. If set it is **authoritative** (a missing compose file there is a hard error, never a silent fallback to another repo). If unset it is derived from the script's own location, then `CHASSIS_HOME`, then `$HOME/behalfbot`.
- `CUSTOMER_HOME` - per-install state root, resolved by `chassis/scripts/_env.sh`.
- `COMPOSE_PROJECT_NAME` - defaults to `behalfbot`, matching the `name:` field in `docker-compose.yml`. Confirm what a running install actually uses with `docker inspect <container> --format '{{index .Config.Labels "com.docker.compose.project"}}'`.
- `CHASSIS_COMPOSE_OVERRIDE` - path to the per-install override. Defaults to `$CUSTOMER_HOME/chassis-compose.override.yml`. Set it to the empty string to run the bare chassis stack with no override - chassis development and smoke tests only, never a real install.

Installs that still carry the pre-#136 vendored subtree keep working: the wrapper falls back to `$CUSTOMER_HOME/chassis/docker-compose.yml`.

> **Writing an override:** compose resolves relative paths inside a compose file against the **project directory**, which is the directory of the first `-f` file - the chassis repo, not `$CUSTOMER_HOME`. An override saying `env_file: [.env.baked]` therefore looks for it next to `docker-compose.yml` in the chassis repo and dies with `env file ... not found`. Always use absolute or interpolated paths: `env_file: [{path: ${HOME}/.behalfbot/.env.baked, required: true}]`.

## Chassis root resolution - the live tree wins

The image bakes a chassis tree at `/app/chassis`, but that copy is only a fallback. At boot, `docker/entrypoint.sh` runs `chassis/scripts/resolve-chassis-root.sh`, which resolves the EFFECTIVE `CHASSIS_ROOT`:

1. A pre-set `CHASSIS_ROOT` (compose `environment:`, `docker -e`, or the customer `.env`) is an operator override, honoured verbatim. The Dockerfile deliberately does not bake the variable - "set" reliably means an operator set it, the same contract `CHASSIS_PLUGINS_ROOT` uses.
2. A usable live tree at `$CUSTOMER_HOME/chassis/chassis` wins. That path is where both supported layouts put the operator's real chassis code: a bind-mounted canonical clone (`- ${HOME}/behalfbot:/app/customer/chassis:ro` in the per-install override) and a vendored `git subtree` both nest the tree one level down, because the repo root contains `chassis/`. This is the tree `git pull` and `chassis-update.sh` actually mutate - so an update is in effect on the next container restart, without waiting for a GHCR image rebuild. A live tree OLDER than the baked one still wins (that is what a rollback looks like) and logs a WARN.
3. `/app/chassis` otherwise - installs that do not mount a chassis tree behave exactly as before.

**Recommended default: mount the operator's clone at `$CUSTOMER_HOME/chassis`.** Now that the runtime honours the live tree (`- ${HOME}/behalfbot:/app/customer/chassis:ro` in the per-install override, nesting the tree at `$CUSTOMER_HOME/chassis/chassis`), a host-side `git pull` is in effect on the next `compose.sh up -d --force-recreate chassis` without waiting for a GHCR image rebuild. New installs should adopt this layout by default; baked-only installs still work but ship fixes only on an image pull.

Guards, because this whole class of bug is "the command ran but did nothing": a live tree that exists but is torn (missing `VERSION`, `scripts/`, or `scheduled-tasks/heartbeat-dispatcher.sh`), or whose MAJOR version differs from the baked tree's (MAJOR is reserved for image-contract changes - refresh the image instead), forces the baked tree LOUDLY: resolver exit 5, `ERROR: CHASSIS ROOT ASSERTION FAILED` in the boot log, and the error recorded in `$CUSTOMER_HOME/chassis-root.state.json`.

Out-of-band visibility: the resolved root is materialised as a symlink at `$CUSTOMER_HOME/state/chassis-root`. `docker exec` sessions never inherit the entrypoint's exports (they get only image/compose ENV), so `_env.sh` consults that symlink first when `CHASSIS_ROOT` is unset, and `chassis-update.sh`'s post-update healthcheck reads the running version through it. `smoke-test.sh check_chassis_root_resolution` FAILs whenever a usable live tree is present but the runtime is executing a different one.

Two things to remember:

- **Restart to activate.** Resolution happens at entrypoint time. After `git pull` on the host clone, `bash chassis/scripts/compose.sh up -d --force-recreate chassis` (or a plain restart) is what makes the new tree live. Until then the smoke test's drift check is the tripwire.
- **Code newer than the image.** The live tree runs on the baked image's interpreters and pip packages. Chassis changes that need new image-level dependencies must bump `requirements.txt`/Dockerfile in a discrete PR and be flagged in the CHANGELOG; the MAJOR-skew guard is the backstop, not a substitute for that discipline.

## installer-2 install runbook (TLDR)

1. SSH into installer-2's box. Install Docker + Compose plugin (one-liner from get.docker.com).
2. `mkdir ~/behalfbot && cd ~/behalfbot`.
3. `git init` (the chassis is vendored as a subtree per `project_chassis_vendor_pattern.md`; for Marc V1 we ship the docker-compose stack first, vendor the subtree post-bootstrap).
4. Write two files: customer `.env` template (drop into `~/behalfbot/.env`), compose-level `.env` (drop alongside `docker-compose.yml`).
5. Copy `docker-compose.yml` from this repo to `~/behalfbot/docker-compose.yml`.
6. `docker compose pull && docker compose up -d postgres vaultwarden`.
7. Visit `http://localhost:8222/admin`, set up Marc's vault, create a user, log in, populate items.
8. `docker compose run --rm -e RBW_EMAIL=... -e RBW_URL=http://vaultwarden -e RBW_MASTER_PASS=... chassis hydrate-env`.
9. `docker compose run --rm chassis bootstrap` - hydrates `.mcp.json`, `CLAUDE.md`, `HEARTBEATS.md`, seeds memory, runs smoke tests.
10. `docker compose up -d chassis` - dispatcher loop starts; first heartbeat fires within 15 min.

Anything that goes wrong is now reproducible - `docker compose logs chassis` shows the dispatcher's stdout/stderr in one place, and `docker compose run --rm chassis shell` drops Marc (or the SSH-driving operator) into the exact same environment the dispatcher sees.

## Migration path for bare-metal installs

V1 bare-metal installs run the un-containerized chassis directly under systemd-user. Migration is not in this PR (Sean's call: "we'll worry about migrating to it once you have it working for Marc and once we're past the May 19th Behalf.bot unveiling"). When migration time comes:

1. Snapshot the installer's `~/behalfbot/.env`, `chassis.config.yaml`, `data/`, `state/`, `memory/` to a backup directory.
2. Stop `behalfbot-heartbeat-dispatcher.timer` (systemd-user).
3. Copy `docker-compose.yml` + `.env.example` from chassis repo into `~/behalfbot/`.
4. `docker compose up -d postgres vaultwarden`.
5. Restore the snapshot into the bind-mount.
6. `docker compose run --rm chassis bootstrap` (idempotent - recognizes existing state).
7. `docker compose up -d chassis`.
8. Verify three consecutive briefings fire cleanly.
9. Disable the old systemd-user timer permanently. `~/behalfbot/chassis/` (the old in-place subtree) becomes legacy; the new chassis is in the image.

## Not-yet-built (follow-up PRs)

This PR ships the image and the Compose stack. The supporting scripts are stubs:

- `chassis/scripts/hydrate-env-from-vw.sh` - to be implemented in a follow-up. Reads `chassis.config.yaml` + each enabled plugin's `openclaw.plugin.json.configSchema`, walks the required secrets, pulls each via `rbw`, writes `.env`.
- `chassis/scripts/smoke-test.sh` - to be implemented. Runs the discord-ping / gmail-draft / notion-read / per-plugin checks listed in `docs/hydration.md` step 11.
- `bootstrap.sh` end-to-end implementation. The skeleton in this PR is unchanged from main - actual hydration logic lands in a follow-up PR keeping diffs reviewable.
- `scripts/install-container.sh` - host-side one-shot installer that scaffolds a customer's `~/behalfbot/` directory, copies the compose file, and prompts for the first-run env values.

Tracking issue: `<v1-reference-install>#537`.

## What the image eliminates from bare-metal installs

The V1 bare-metal install (2026-05-06/07, documented in `LESSONS_FROM_V1.md`) surfaced a catalog of environment variance. Each item below is a friction source that containerization removes entirely - future Dockerfiles should treat this list as required `RUN` steps:

| Bare-metal friction | How image eliminates it |
|---|---|
| Python 3.11 vs 3.12 system default (Debian 12 ships 3.11; f-string backslash handling differs) | Image bakes Python 3.12 + `uv` — no system Python dependency |
| `uv` not installed; deadsnakes PPA needed for 3.12 | `uv` pre-installed; `uv python install 3.12` in Dockerfile |
| `ffmpeg`, `sqlite3`, `python3-yaml` not on Debian 12 base | All in image `RUN apt-get install` layer |
| PyYAML system-install blocked by PEP 668 on Debian 12 | `uv pip install pyyaml` in image |
| `python3-anthropic` not in Debian 12 repos | `uv pip install anthropic` in image (for scripts using SDK directly) |
| `/opt/homebrew/bin/gtimeout` + `/opt/homebrew/bin/ollama` hardcoded paths in `heartbeat-dispatcher.sh` — invalid on any non-macOS-Homebrew system | Image bakes `coreutils timeout` at `/usr/bin/timeout`; Ollama at a known `/usr/local/bin/ollama`. No `command -v` dance needed. |
| systemd unit PATH doesn't include `~/.local/bin` — `uv run` silently fails in heartbeats | Image sets `ENV PATH` at build time; entrypoint inherits clean PATH |
| `rbw` not installed for Vaultwarden HTTP access; `bw` CLI enforces HTTPS | `rbw` pre-installed in image |

Note: macOS-Homebrew paths (`/opt/homebrew/bin/ollama`, `/opt/homebrew/bin/gtimeout`) became blocker-level bugs for the V1 Linux bare-metal install. They exist in `heartbeat-dispatcher.sh` lines 188, 475, 639. The V1 install applied `command -v gtimeout || command -v timeout` fallbacks locally, but **the patch was never upstreamed to chassis main** per Sean's "defer until containerization" call (2026-05-07). The image Dockerfile's baked paths make the `command -v` dance unnecessary - don't re-add it in the image. Track the bare-metal shim as divergence debt in `project_chassis_jax_divergence_debt.md` until the container is the canonical distribution.

## Cross-references

- `docs/install-mvp-scope.md` - what's in the MVP base install
- `docs/hydration.md` - the 12-step hydration walkthrough
- `docs/LESSONS_FROM_V1.md` - the empirical lessons baked into this image (lessons #31-#37 from installer-1 install)
- `docs/security.md` - hook-layer guardrails (image inherits the chassis hook tree as-is)
- `<v1-reference-install>#537` - containerization tracking issue
- `<v1-reference-install>#538` - installer-2 install ticket (consumer of this PR)
