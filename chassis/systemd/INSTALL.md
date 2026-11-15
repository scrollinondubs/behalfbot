# Bare-metal Linux install via systemd

For installs that run chassis WITHOUT Docker (Linux bare-metal — installer-1/fatboy is the V1 reference). The Docker install path doesn't need any of this — the chassis container's entrypoint runs the dispatcher in a sleep-loop with the right env + path set.

The exception is [Discord session (channels) supervision](#discord-session-channels-supervision): the `claude --channels` session runs outside the container on every install, so that section applies to Docker installs too.

## Why this template exists

installer-1's install on `fatboy` (bare-metal Debian) hit two silent failure modes during install in May 2026:

1. **systemd resets PATH** on service spawn to `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin`. The `claude` binary installs to `~/.local/bin/claude` (npm convention) — NOT in that default. Without an explicit `PATH` override, `claude -p` invocations failed `FileNotFoundError` silently.

2. **`claude -p` without `--mcp-config`** doesn't load the Discord/memory/siyuan/etc. MCP servers. Claude could process messages but had no way to call `mcp__plugin_discord_discord__reply` to respond. (The chassis dispatcher's `claude -p` calls ALREADY pass `--mcp-config`; this gap was in installer-1's custom Discord-gateway daemon, not in chassis core. But the principle applies: any systemd service that wraps `claude -p` must pass it.)

Both issues are install-time gotchas that the chassis Docker container avoids by construction. This doc + template surfaces them for non-Docker installs.

Source: scrollinondubs/behalfbot#99 + OzzyBotman incident report in #behalf-bot-setup 2026-05-22.

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

## Discord session (channels) supervision

The long-running `claude --channels` Discord session lives in a detached tmux session on the host. On macOS two LaunchAgents keep it alive (`chassis/launchd/com.behalfbot.discord-{restart,watchdog}.plist.template`). On Linux the same job is done by four systemd user units:

| Template | Rendered as | What it does |
|---|---|---|
| `discord-restart.service.template` | `behalfbot-<bot>-discord-restart.service` | `Type=oneshot`, runs `$CUSTOMER_HOME/scripts/restart-<bot>-discord.sh` (kill + recreate the tmux session) |
| `discord-restart.timer.template` | `behalfbot-<bot>-discord-restart.timer` | daily 05:00, and 30s after the timer starts (boot with linger, or `enable --now`) |
| `discord-watchdog.service.template` | `behalfbot-<bot>-discord-watchdog.service` | `Type=oneshot`, runs `$CUSTOMER_HOME/scripts/watchdog-<bot>-discord.sh` (restarts the session when it is missing or stuck) |
| `discord-watchdog.timer.template` | `behalfbot-<bot>-discord-watchdog.timer` | 5 min after the timer starts, then every 30 min |

Both services set `KillMode=process`. Read the KillMode entry under "Common gotchas" before changing that.

### Render

`bootstrap.sh` renders them in step 8. By hand:

```bash
CUSTOMER_HOME=~/.behalfbot CHASSIS_HOME=~/behalfbot BOT_NAME=<bot> \
    bash chassis/scripts/bootstrap-customer-scripts.sh --plists
```

On Linux this writes the units to `$CUSTOMER_HOME/systemd/` and prints the commands below with your paths filled in. It never enables them.

### Enable (user scope, recommended)

```bash
sudo loginctl enable-linger "$USER"     # user manager runs at boot, survives logout
systemctl --user link ~/.behalfbot/systemd/behalfbot-<bot>-discord-restart.service
systemctl --user link ~/.behalfbot/systemd/behalfbot-<bot>-discord-restart.timer
systemctl --user link ~/.behalfbot/systemd/behalfbot-<bot>-discord-watchdog.service
systemctl --user link ~/.behalfbot/systemd/behalfbot-<bot>-discord-watchdog.timer
systemctl --user daemon-reload
systemctl --user enable --now behalfbot-<bot>-discord-restart.timer behalfbot-<bot>-discord-watchdog.timer
```

Without `enable-linger` the user manager only exists while you are logged in: the timers do not run after a reboot, and logging out of your last SSH session kills the tmux server.

Enabling the restart timer restarts the live session 30 seconds later. Run the commands from a shell outside that tmux session.

Verify:

```bash
systemctl --user list-timers 'behalfbot-*'
journalctl --user -u behalfbot-<bot>-discord-restart.service -n 50
tmux ls                                  # session still there a minute after the unit went inactive
```

`$CUSTOMER_HOME/logs/scheduled/discord-restart.log` has the restart script's own log.

### System scope alternative

If you would rather not use lingering, copy the four rendered files to `/etc/systemd/system/` and in each `.service`:

- add `User=<installer>` under `[Service]`;
- replace every `%h` with the literal home path (`/home/<installer>`). In the system manager `%h` is root's home, whatever `User=` says;
The `.timer` files need no change. Then `sudo systemctl daemon-reload && sudo systemctl enable --now behalfbot-<bot>-discord-restart.timer behalfbot-<bot>-discord-watchdog.timer`. Keep `KillMode=process`.

### Container-path installs

The rendered restart script starts host `claude`. A container-path install whose session command is `docker compose run --rm -it ... chassis claude --channels ...` needs its own start script instead. The units still fit: copy them, point `ExecStart=` at your script, and keep `Type=oneshot` with `KillMode=process`. Any unit whose script runs `tmux new-session -d` and then exits needs that line, whatever runs inside the session.

## Common gotchas

### The Discord session dies seconds after the supervisor starts it (missing `KillMode=process`)

Symptom: the timer fires, the restart script logs "session restarted successfully", and a few seconds later `tmux ls` shows no session (or no server). The timer starts it again next tick, forever. `journalctl` shows the unit as succeeding every time.

Cause: systemd's default `KillMode=control-group`. When a `Type=oneshot` service's script exits, systemd kills every process still in the unit's cgroup. `tmux new-session -d` forks a tmux server that stays in the cgroup of whoever started it, so the server and the `claude` process inside it are killed right after the script finishes.

Fix: `KillMode=process` under `[Service]`, then `systemctl --user daemon-reload` (or `sudo systemctl daemon-reload` for a system unit). The chassis templates set it. Check a hand-rolled unit with `systemctl --user show -p KillMode <unit>`.

Seen on a Hetzner Ubuntu container-path install on 2026-11-14/15: a hand-written 5-minute timer plus oneshot service without the line restarted the bot every 5 minutes for ten hours (21:54 to 08:18 UTC). It went unnoticed for two days because the sessions before it had been started by hand over SSH, outside the unit's cgroup, so they never got killed.

Expected side effects: the next start logs `Found left-over process ... (tmux)`, which is the session from the previous run. `systemctl stop` on the unit no longer stops the bot; use `tmux kill-session -t <bot>-discord`.

### Permission denied on `~/behalfbot/...`

If you used `User=behalfbot` (system-scoped) but the chassis tree lives at `/home/<your-user>/behalfbot/`, systemd won't have permission. Either:
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
- `scrollinondubs/behalfbot#99` — the installer-1 incident report this doc + template responds to
