# Behalf.bot — Streamlined Install Runbook (V2)

> **Status:** Ratified by Sean 2026-05-18 during Toby's Phase-2 install (case study #3). Supersedes the linear pattern in `installer-homework.md` + `bootstrap.sh` for V2+ installs starting with Marc.

> **Audience:** Operator (Sean+${ASSISTANT_NAME} SSH session). Installer-facing instructions still live in `installer-homework.md` — this runbook orchestrates what the operator does AFTER the installer has done their browser-only prep.

---

## Why this exists

V1 install pattern (installer-1, Marc, Toby) was linear:
1. Installer completes all homework (~30-90 min browser-only work)
2. Installer pings operator
3. Operator runs full bootstrap end-to-end (~2-3h)
4. First-heartbeat smoke test = signoff

Two problems with that pattern:
- **Single big serial block.** Installer waits, then operator waits. No parallelism.
- **All-or-nothing failure modes.** A missing VW secret blocks the whole install. The chassis won't boot until every required item is populated.

V2 streamlined pattern parallelizes operator and installer work + degrades gracefully on missing plugin secrets.

---

## V2 install sequence

### Stage 1 — Preflight (installer, ~15 min browser-only)

Operator gives installer these prelim instructions before any SSH session:

1. **Tailscale share** — Install Tailscale on the target box (macOS app or Linux CLI), sign in with installer's identity, share the node to `sean@grid7.com` via Tailscale admin → Machines → `...` → Share. Drop the share link in install Discord channel.
2. **Homebrew install** (macOS) or apt update (Linux) — installer runs ONE platform-specific package-manager install in their local Terminal. Requires their sudo password; can't be driven over SSH. macOS: `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"`.
3. **Operator SSH pubkey** — operator posts ${ASSISTANT_NAME}'s SSH pubkey in install channel. Installer runs:
   ```bash
   mkdir -p ~/.ssh && chmod 700 ~/.ssh
   echo '<paste-<v1-reference-install>-pubkey>' >> ~/.ssh/authorized_keys
   chmod 600 ~/.ssh/authorized_keys
   ```
4. **Remote Login** — macOS: System Settings → General → Sharing → toggle Remote Login ON, "Only these users" + add own user. Linux: SSH usually already on.
5. **Installer pings "ready"** in install channel with `whoami` output as the SSH user.

**Stage-1 exit criterion:** Operator can SSH into the box and runs `whoami && uname -a && sw_vers` (mac) or `lsb_release -a` (linux) successfully.

---

### Stage 2 — Confirm SSH + install dependencies (operator, parallel-friendly, ~15-20 min)

Operator works via SSH; installer is free to step away during this stage.

1. Verify SSH access works + capture environment (read-only probe per LESSONS_FROM_V1 #36):
   ```bash
   ssh installer@host "whoami && uname -a && sw_vers"
   ssh installer@host "for tool in brew docker python3 node git; do which $tool && $tool --version 2>&1 | head -1; done"
   ```
2. Install brew prerequisites (mac) or apt prerequisites (linux):
   ```bash
   # macOS via Homebrew
   ssh installer@host "HOMEBREW_NO_AUTO_UPDATE=1 NONINTERACTIVE=1 /opt/homebrew/bin/brew install \
     colima docker docker-buildx tmux ffmpeg jq postgresql@16 node@20 ollama rbw bitwarden-cli pinentry-mac"
   ```
3. Install `uv` for Python 3.12 (chassis-canonical per LESSONS_FROM_V1 #31):
   ```bash
   ssh installer@host "curl -LsSf https://astral.sh/uv/install.sh | sh && /Users/<user>/.local/bin/uv python install 3.12"
   ```
4. Install Claude Code CLI via npm:
   ```bash
   ssh installer@host "/opt/homebrew/bin/npm install -g @anthropic-ai/claude-code"
   ```
5. Initialize Postgres (Homebrew on mac creates a cluster on install; just need to start + createdb):
   ```bash
   ssh installer@host "/opt/homebrew/bin/brew services start postgresql@16"
   ssh installer@host "/opt/homebrew/opt/postgresql@16/bin/createdb behalfbot"
   ```
6. Start colima (Docker daemon, no Docker Desktop GUI required):
   ```bash
   ssh installer@host "/opt/homebrew/bin/colima start --cpu 4 --memory 8 --disk 60"
   ```
7. Fix Docker credsStore if it references Docker Desktop (it does by default on macOS):
   ```bash
   ssh installer@host "/opt/homebrew/bin/jq 'del(.credsStore)' ~/.docker/config.json > ~/.docker/config.json.new && mv ~/.docker/config.json.new ~/.docker/config.json"
   ```
8. Install Playwright runtime for Stage 5b (operator-driven provisioning):
   ```bash
   ssh installer@host "/opt/homebrew/bin/npm install -g playwright && /opt/homebrew/bin/playwright install chromium"
   ```

**Stage-2 exit criterion:** all brew packages installed, postgres reachable, colima up, Docker pulling images works, Playwright Chromium ready.

---

### Stage 3 — Vaultwarden spinup + Tailscale ACME cert (operator, ~10 min)

Per LESSONS_FROM_V1 #38, Vaultwarden is spun up by the operator during the SSH session, NOT pre-installed by the installer.

1. **Installer enables HTTPS Certificates in their Tailscale admin panel.** This is a one-toggle thing at https://login.tailscale.com/admin/dns under "HTTPS Certificates." Without this, `tailscale cert` returns "your Tailscale account does not support getting TLS certs" and you fall back to self-signed (which trips rbw in the chassis container — avoid). **This is the only Stage-3 step that requires the installer; everything else operator-driven.**
2. Generate Let's Encrypt cert via Tailscale's built-in ACME:
   ```bash
   ssh installer@host "mkdir -p ~/vaultwarden-data/ssl && cd ~/vaultwarden-data/ssl && /Applications/Tailscale.app/Contents/MacOS/Tailscale cert <hostname>.<tailnet>.ts.net"
   ```
   Outputs `<hostname>.<tailnet>.ts.net.crt` and `.key`. Symlink to `cert.pem` + `key.pem` for chassis-expected names.
3. Generate VW admin token:
   ```bash
   ADMIN_TOKEN=$(openssl rand -hex 32)
   ```
4. Pull + run Vaultwarden container with the Tailscale-issued cert:
   ```bash
   ssh installer@host "docker pull vaultwarden/server:latest"
   ssh installer@host "docker run -d --name vaultwarden --restart unless-stopped \
     -v /Users/<user>/vaultwarden-data:/data \
     -p 8222:80 \
     -e SIGNUPS_ALLOWED=true \
     -e ADMIN_TOKEN=$ADMIN_TOKEN \
     -e DOMAIN=https://<hostname>.<tailnet>.ts.net:8222 \
     -e ROCKET_TLS='{certs=\"/data/ssl/cert.pem\",key=\"/data/ssl/key.pem\"}' \
     vaultwarden/server:latest"
   ```
5. Verify:
   ```bash
   ssh installer@host "curl -sS -o /dev/null -w 'HTTPS %{http_code}\n' https://<hostname>.<tailnet>.ts.net:8222/alive"
   ```
   Expect HTTPS 200. No `-k` needed since LE cert is real.
6. Hand off VW URL to installer in install channel:
   ```
   VW is live at https://<hostname>.<tailnet>.ts.net:8222
   No browser cert warning - it's a real LE cert.
   Create your master account: click "Create Account" → set master password (write it down, no recovery) → DM me the master password ONE TIME (rotated at signoff per LESSONS_FROM_V1 #38).
   ```

**Stage-3 exit criterion:** Installer has created VW master account + DM'd master password to operator. Operator captures master_email + master_password + admin_token to local gitignored stash file.

---

### Stage 4 — Base chassis spinup (operator, parallel with Stage 5/5b; ~15-20 min)

Operator and installer work in parallel:
- Operator pulls chassis image + spins up the base chassis container (this stage)
- Installer + operator together do Playwright-driven account provisioning (Stage 5b, preferred) or installer alone does manual VW population (Stage 5, fallback)

1. Pull chassis image:
   ```bash
   ssh installer@host "docker pull ghcr.io/scrollinondubs/behalfbot-chassis:latest"
   ```
   (Or if pre-publish window: tarball-ship via `git archive` per LESSONS_FROM_V1 #32.)
2. Lay down customer-specific config:
   ```bash
   ssh installer@host "mkdir -p ~/behalfbot"
   # Tarball-ship docs/install-<installer>-*.{md,yaml} files + chassis source via scp
   ssh installer@host "cd ~/behalfbot && cp docs/install-<installer>-chassis-config.yaml chassis.config.yaml && cp docs/install-<installer>-profile.md INSTALL_PROFILE.md"
   ```
3. **CRITICAL — `chassis.config.yaml` must have ALL `modules.*.enabled` flipped to `false` for base install.** Plugin-enable happens in Stage 6 as VW secrets land. Run a sanity check:
   ```bash
   ssh installer@host "grep -E 'enabled: true' ~/behalfbot/chassis.config.yaml"
   ```
   Expect: no output OR only top-level `modules.briefing.enabled: true` (briefing is base-required because the first-heartbeat smoke test fires through it). Any other `enabled: true` for a plugin = flag + flip to false before continuing.
4. Pre-stage minimal `.env` with values the operator already knows (no VW pull needed yet):
   ```bash
   # POSTGRES_PASSWORD, INSTANCE_NAME, INSTALLER_NAME, INSTALLER_DISCORD_USER_ID
   # These can be pre-populated via bw CLI to VW + read here, OR direct-written
   ```
5. Start chassis container with the minimal env:
   ```bash
   ssh installer@host "cd ~/behalfbot && docker compose up -d chassis"
   ```
6. Verify first-heartbeat fires in `#<installer>-briefings` channel — but expect it to be a SKINNY briefing (just calendar + ops, no plugin content) since plugins are off.

**Stage-4 exit criterion:** Chassis container running, dispatcher loop alive, skinny first-heartbeat lands in briefings channel within 15 min.

---

### Stage 5b — Playwright-driven account provisioning (PREFERRED PATH, operator-driven, ~15-25 min)

> **Status:** Sean directive 2026-05-18 (same conversation as the Stage 4 parallelization). Apply to installer-2's install onward — too late to retrofit Toby's (he already manually provisioned bot + ops webhook).

**Goal:** Eliminate installer typing of tokens, secrets, webhook URLs. Operator drives a headed Playwright Chromium on installer's Mac via SSH; installer logs in once per service (Discord, Google, GitHub); operator drives the rest — bot creation, token reset, intent toggles, OAuth URLs, webhook creation, App Password generation, PAT generation. Tokens captured via `page.locator(...).textContent()` and written direct to VW via bw CLI. Never displayed in cleartext, never pasted in Discord.

**Why on installer's Mac (not operator's machine):**
- Discord / Google / GitHub all check IP origin + 2FA challenges against installer's identity. Login from operator's machine triggers "new device" warnings + may lock the account.
- Cookies + session persist on installer's Mac across operator's automation passes.

**Playwright launch pattern over SSH** (X11 forwarding NOT needed; we use Playwright's headed mode with a persistent context that the installer can see locally):

```bash
# Operator launches a Playwright Chromium headed instance with persistent profile
ssh -t installer@host "cd ~/behalfbot && PWDEBUG=1 PLAYWRIGHT_BROWSERS_PATH=~/.cache/ms-playwright \
  /opt/homebrew/bin/npx playwright open --browser=chromium --user-data-dir=~/.behalfbot/playwright-profile \
  https://discord.com/developers/applications"
```

Installer sees the Chromium window on their Mac, logs in to Discord with `asimovthebot@gmail.com`. Browser profile saves session.

**Per-service automation flow** (operator runs from their own machine over SSH to installer's Mac):

```bash
# Operator writes a Playwright script that drives the installer's Chromium remotely.
# Connects via CDP (Chrome DevTools Protocol) over SSH tunnel.

# 1. SSH tunnel from operator's machine to installer's headed Chromium debug port
ssh -L 9222:localhost:9222 installer@host

# 2. Operator's Playwright script connects to ws://localhost:9222 (the installer's Chromium)
# 3. Script navigates + captures tokens, writes to VW via bw CLI on installer's host
```

**Provisioning steps the operator drives via Playwright:**

| Service | Page | Operator action | Token captured to VW item |
|---|---|---|---|
| Discord | https://discord.com/developers/applications | Click "New Application" → name "Asimov" → Bot tab → Reset Token | `Behalf.bot - Discord bot token` |
| Discord | (same page) | Bot tab → toggle MESSAGE CONTENT INTENT ON → Save | (no token — config side-effect) |
| Discord | (same page) | OAuth2 → URL Generator → scopes `bot`+`applications.commands` → bot perms (Send/Read Messages, Webhooks, Slash Commands) → copy URL | (URL displayed to installer to click for server-invite) |
| Discord | https://discord.com/channels/<server>/<channel> → channel settings → Integrations → Webhooks | New Webhook → "Asimov Briefings" → copy URL | `Behalf.bot - Discord briefings webhook` |
| Discord | (same, on #ops channel) | New Webhook → "Asimov Ops" → copy URL | `Behalf.bot - Discord ops webhook` |
| Google | https://myaccount.google.com/apppasswords (logged in as agent-Gmail) | Generate App Password → label "Asimov chassis" → copy 16-char password | `Behalf.bot - Google Workspace agent` (password field; username = agent Gmail) |
| GitHub | https://github.com/settings/tokens (logged in as agent-GitHub) | Generate new token (classic) → scopes `repo`+`workflow`+`read:org` → no expiry → copy ghp_… | `Behalf.bot - GitHub PAT` |

After each capture: operator runs `bw create item` (or `bw edit item` if it exists) with `NODE_TLS_REJECT_UNAUTHORIZED=0` + BW_SESSION, writes to VW.

**Stage-5b exit criterion:** All token-bearing VW items populated by operator-driven Playwright. Installer's role limited to logging in once per service.

**Fallback to Stage 5 manual:** If Playwright fails (rare — usually a service UI change), fall back to the manual path below.

---

### Stage 5 — Manual VW population (FALLBACK PATH, installer-driven, ~15-30 min)

> Use this only if Stage 5b Playwright path fails or the installer prefers manual flow.

Installer works through the canonical VW item list in `docs/installer-vw-template.md`. Operator queues a checklist in the install channel with EXACT item names (chassis hydration does string-match).

Operator pre-populates non-secret items via API (saves installer typing) for known literals:
- `Behalf.bot - instance name` → `<UPPERCASE-PERSONA>` (e.g. `ASIMOV`)
- `Behalf.bot - installer name` → installer full name
- `Behalf.bot - Discord installer user_id` → known from interview
- `Behalf.bot - Postgres password` → operator-generated `openssl rand -base64 32` value

Installer populates secrets only:
- `Behalf.bot - Discord bot token` (Developer Portal)
- `Behalf.bot - Discord briefings webhook` (channel settings)
- `Behalf.bot - Discord ops webhook` (channel settings)
- `Behalf.bot - Google Workspace agent` (App Password at myaccount.google.com/apppasswords)
- `Behalf.bot - GitHub PAT` (bot account's tokens page)
- `Behalf.bot - Vaultwarden API token` (VW UI → Settings → API Key)

NEVER pasted in Discord. Always direct-entered in VW.

**Stage-5 exit criterion:** Installer pings "all items in VW" in install channel.

---

### Stage 6 — Stream plugin enables per VW-secret-arrival (operator, ~15 min)

The chassis is already running base. Operator now enables plugins one-by-one as VW secrets are available.

Per-plugin enable cycle:
1. Verify VW has the plugin's required items (e.g. `Behalf.bot - Discord bot token` for any Discord-using plugin)
2. Edit `chassis.config.yaml`: flip `modules.<plugin>.enabled: true`
3. Run `chassis/scripts/hydrate-env-from-vw.sh` to pull new secrets into `.env`
4. Restart chassis container: `docker compose restart chassis`
5. Verify plugin's smoke test passes (per-plugin specifics in `plugins/<name>/SMOKE_TEST.md`)
6. Update install-state memory file (LESSONS_FROM_V1 #37)

Suggested enable order (least-to-most dependency):
1. `briefing` (already on as base)
2. `admin` (needs Gmail App Password + Calendar OAuth)
3. `bfl` (no additional secrets if vision-only; FDC/Strava/Oura optional)
4. `whatsapp` (needs WhatsApp Web session — operator handles separately, no VW secret)
5. `event_radar` (needs no secrets beyond core)
6. Per-installer custom modules (e.g. Toby's `task_management` + `article_pipeline` — both Day-91+ since plugin code TBD)

**Stage-6 exit criterion:** All interview-wishlist modules enabled, smoke tests green.

---

### Stage 7 — Signoff

**Acceptance criteria:** end-to-end running instance satisfying ALL interview-wishlist criteria. NOT "all homework items checked off."

The wishlist is:
- Every module the installer said "yes" or "tell me more" to during the scoping interview is enabled + verified working
- First-heartbeat lands cleanly 3 consecutive days in `#<installer>-briefings`
- Operator-installer review pass: walk through each interview priority, confirm Asimov/installer-named-bot delivers the expected behavior

Signoff handoff (~4-6 weeks post-install per LESSONS docs):
- Master VW password rotated (operator → installer)
- Operator SSH pubkey removed from installer's `~/.ssh/authorized_keys` (or kept as DevOps shadow, installer's choice)
- Memory rotation: state file marks install complete, lessons learned captured

---

## What this runbook replaces

Versus the V1 linear pattern, V2 parallelizes Stage 4 (chassis spinup) with Stage 5/5b (account provisioning). Saves ~30-45 min per install. Also degrades gracefully — if a plugin's VW secret is missing, that plugin stays disabled but the chassis still runs. Stage 5b Playwright path on top reduces installer typing to effectively zero (just login).

The old `installer-homework.md` Step-1 sub-items remain valid (they're the customer's account-provisioning checklist). What changed: Step 1f (Vaultwarden self-host) is now operator-handled in Stage 3. Step 2 (Linux/Mac box setup) splits into Stage 1 (installer pre-flight) + Stage 2 (operator deps). The "wizard" reference in Step 0 should be removed entirely (recurring installer confusion per installer-3's install).

---

## Open questions for V2 hardening

- **Tailscale HTTPS Certificates feature gate:** installer must enable in their tailnet admin OR operator falls back to self-signed cert path (mounts cert into chassis container per LESSONS_FROM_V1 #38 workaround). Document the toggle path clearly in `installer-homework.md`.
- **rbw vs bw choice for hydration:** chassis Dockerfile bakes rbw. If self-signed cert path is used (Tailscale ACME not enabled), the chassis container needs the cert mounted + `SSL_CERT_FILE` env var. Document in `containerization.md`.
- **Plugin smoke tests:** each `plugins/<name>/` should ship a `SMOKE_TEST.md` describing the per-plugin verification step for Stage 6. Currently inconsistent across plugins.
- **Playwright over SSH UX:** how does the installer see the headed Chromium window? Options: (a) installer launches the script themselves via local Terminal (1 command, then operator drives via CDP), (b) operator triggers `ssh -t` with X11 forwarding (slow over high-latency links), (c) screen-share window during the install session. Option (a) likely cleanest — codify command + paste into install channel.

---

## References

- `installer-homework.md` — customer-facing pre-flight checklist
- `installer-vw-template.md` — canonical VW item names (chassis hydration contract)
- `LESSONS_FROM_V1.md` #31-#38 — incremental learnings from installer-1 + Marc + installer-3 installs
- `bootstrap.sh` — legacy linear bootstrap (still callable for V1-style installs)
- `containerization.md` — Docker layout + volume mounts

---

*Generated 2026-05-18 during Toby's Phase-2 install per Sean directive in channel 1504030588603076699. Apply to installer-2's install (next up) + all future onboardings.*
