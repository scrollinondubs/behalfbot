# Bare-metal Linux install via systemd

For installs that run chassis WITHOUT Docker (Linux bare-metal — installer-1/fatboy is the V1 reference). The Docker install path doesn't need any of this — the chassis container's entrypoint runs the dispatcher in a sleep-loop with the right env + path set.

## Why this template exists

installer-1's install on `fatboy` (bare-metal Debian) hit two silent failure modes during install in May 2026:

1. **systemd resets PATH** on service spawn to `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin`. The `claude` binary installs to `~/.local/bin/claude` (npm convention) — NOT in that default. Without an explicit `PATH` override, `claude -p` invocations failed `FileNotFoundError` silently.

2. **`claude -p` without `--mcp-config`** doesn't load the Discord/memory/siyuan/etc. MCP servers. Claude could process messages but had no way to call `mcp__plugin_discord_discord__reply` to respond. (The chassis dispatcher's `claude -p` calls ALREADY pass `--mcp-config`; this gap was in installer-1's custom Discord-gateway daemon, not in chassis core. But the principle applies: any systemd service that wraps `claude -p` must pass it.)

Both issues are install-time gotchas that the chassis Docker container avoids by construction. This doc + template surfaces them for non-Docker installs.

Source: scrollinondubs/behalfbot-chassis#99 + OzzyBotman incident report in #behalf-bot-setup 2026-05-22.

## Install steps

### 1. Drop the template into systemd's unit directory

```bash
cp chassis/systemd/dispatcher.service.template \
   /etc/systemd/system/behalfbot-dispatcher.service
# Or for user-scoped:
# cp chassis/systemd/dispatcher.service.template \
#    ~/.config/systemd/user/behalfbot-dispatcher.service
```

### 2. Substitute install-specific values

The template uses systemd's `%h` token for the install user's home dir (works under user-scoped units automatically). For SYSTEM-scoped units, edit the file and replace `%h` with `/home/<installer>` literally.

If your install lives somewhere other than `~/behalfbot/`, also update the `CHASSIS_HOME=` line + the `ExecStart` path.

### 3. Reload + enable

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now behalfbot-dispatcher
```

(User-scoped: `systemctl --user daemon-reload && systemctl --user enable --now behalfbot-dispatcher`.)

### 4. Verify

```bash
systemctl status behalfbot-dispatcher
journalctl -u behalfbot-dispatcher -f   # tail logs
```

Within 15 min you should see dispatcher tick logs from `${CHASSIS_HOME}/logs/scheduled/YYYY-MM-DD-dispatcher.log`.

### 5. (Optional) Set up reconciler

If you want the independent audit overlay (recommended for the first few weeks post-install — see chassis#95), add a `behalfbot-reconciler.timer` + `behalfbot-reconciler.service` pair that fires `chassis/scripts/reconcile-heartbeats.sh` every 15 min. Sample timer config:

```ini
# /etc/systemd/system/behalfbot-reconciler.timer
[Unit]
Description=Behalfbot Reconciler — heartbeat dispatcher health audit

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
AccuracySec=30s

[Install]
WantedBy=timers.target
```

```ini
# /etc/systemd/system/behalfbot-reconciler.service
[Unit]
Description=Behalfbot Reconciler — heartbeat dispatcher health audit (one-shot)

[Service]
Type=oneshot
Environment="CHASSIS_HOME=%h/behalfbot"
ExecStart=%h/behalfbot/chassis/scripts/reconcile-heartbeats.sh
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now behalfbot-reconciler.timer
```

Findings JSONL lands at `${CHASSIS_HOME}/logs/scheduled/heartbeat-reconciler-findings.jsonl`.

## Common gotchas

### Permission denied on `~/behalfbot/...`

If you used `User=behalfbot` (system-scoped) but the chassis tree lives at `/home/sean/behalfbot/`, systemd won't have permission. Either:
- Re-clone the chassis as the `behalfbot` user, OR
- Drop `User=` (runs as root — not recommended), OR
- Switch to user-scoped systemd (`systemctl --user`)

### Logs are empty / dispatcher seems dead

Check:
1. `systemctl status behalfbot-dispatcher` — is it active?
2. `journalctl -u behalfbot-dispatcher | tail -50` — is there an error?
3. `which claude` from the install user's shell — does it return `~/.local/bin/claude`?
4. `echo $PATH` — does PATH include `~/.local/bin`?

The PATH gotcha is the most common failure mode for the first 24h. The template's `Environment="PATH=..."` line fixes it, but if you edited it wrong the binary lookup silently breaks.

### claude -p says "Invalid API key" or "OAuth credentials missing"

The dispatcher's `claude -p` invocations need OAuth, not API key. Check:
1. `ANTHROPIC_API_KEY` is NOT set in your install's `.env` (or chassis-side `.env.baked` if you have one). It's actively unset by both the systemd template + the dispatcher script.
2. `~/.claude/.credentials.json` exists and has fresh OAuth tokens. If not, run `claude` interactively once to log in.
3. `~/.claude.json` exists. Some Claude Code versions write OAuth state there too.

## Docker install path

If you're considering switching to the Docker install path (recommended for new customers as of 2026-05-22), see `chassis/docs/containerization.md`. The Docker container handles all of the above by construction.

## Cross-references

- `chassis/scheduled-tasks/heartbeat-dispatcher.sh` — the dispatcher this service wraps
- `chassis/scripts/reconcile-heartbeats.sh` — companion reconciler (chassis#95)
- `chassis/docs/containerization.md` — Docker install (alternative path)
- `scrollinondubs/behalfbot-chassis#99` — the installer-1 incident report this doc + template responds to
