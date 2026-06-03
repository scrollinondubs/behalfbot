# LaunchDaemon vs LaunchAgent — which to use

> Decision rule for every chassis-shipped (and plugin-shipped) host-side
> `launchd` plist on macOS. Wrong choice silently breaks unattended reboot
> recovery. Right choice is usually "daemon". See chassis#14 for the incident
> that drove this.

## TL;DR

| Domain | Path | Loads at | Needs GUI? | Survives unattended reboot? |
|---|---|---|---|---|
| **LaunchDaemon** (system) | `/Library/LaunchDaemons/` | Boot | No | **Yes** |
| LaunchAgent (user)        | `~/Library/LaunchAgents/` | First GUI/Aqua login | Yes (implicitly) | No |

**Default: LaunchDaemon.** Only fall back to LaunchAgent if the job genuinely
needs a display, an Aqua-session-only resource (some keychain items,
Cmd+Space integrations), or a GUI app's running process tree.

## Why this matters

A LaunchAgent in `~/Library/LaunchAgents/` only loads after the user logs into
the macOS GUI. On an unattended Mac (one that reboots overnight or while the
installer is traveling, with auto-login disabled), the user-session never
materialises — every chassis-shipped LaunchAgent silently fails to register.
The chassis Docker container keeps running because Docker Desktop is a system
service, but every host-side scheduled job stops firing.

That's exactly what happened on 2026-06-03 (chassis#14): the install Mac
rebooted around 09:43 with no console login, every `com.<bot>.*` agent stayed
dormant, and the 10:00 dating swipe slot got skipped. The failure was silent
because no telemetry reports it.

LaunchDaemons load at boot, no login required. They run as the user named in
`UserName`, not as root, so file-permission expectations still hold.

## When to use which

Use **LaunchDaemon** if the job:

- Does `docker exec` against the chassis container
- Hits the network (HTTP poll, MQTT subscriber, webhook receiver)
- Runs a headless Python/Node process (`uvicorn`, a script, a watcher)
- Syncs files between two paths
- Calls `launchctl bootout` / `launchctl bootstrap` on something else

Use **LaunchAgent** only if the job:

- Drives an Android emulator (needs Aqua to render the AVD window)
- Drives a Playwright Chromium / Firefox / WebKit instance (each browser
  needs an Aqua context — `headless: true` does *not* exempt you from this
  on macOS, despite what the name suggests)
- Touches `pasteboard`, `screencapture`, or any AppKit API that requires
  the user's `WindowServer` session
- Talks to a GUI app via Apple Events / AppleScript

If you're not sure, default to daemon and try it. If the job fails because it
can't find an Aqua resource, then it's an agent.

## Daemon plist requirements (vs agent plist)

Agents and daemons share the same XML structure, but daemons must include
two extra keys and observe a few path constraints:

```xml
<key>UserName</key>
<string>${USER}</string>
<key>GroupName</key>
<string>staff</string>
```

Without `UserName`, the daemon runs as `root`. Anything that writes to the
user's home dir or talks to the user-owned Docker socket then breaks with
permission errors. `staff` is the default user-group on macOS.

Path constraints:

- All paths must be **absolute**. No `~`, no `$HOME` expansion shortcuts.
  Template substitution should produce `/Users/<installer>` literally.
- `EnvironmentVariables` should set `HOME=/Users/<installer>` explicitly.
- `WorkingDirectory` likewise — absolute, not `~`.

## How chassis installs them

`chassis/scripts/bootstrap-customer-scripts.sh` renders the templates from
`chassis/launchd/*.plist.template` into `${CUSTOMER_HOME}/launchd/`, then —
when invoked with `--activate-plists` — installs each rendered plist into
the correct domain:

- **Daemon-domain** plists: `sudo cp` to `/Library/LaunchDaemons/`,
  `sudo chown root:wheel`, `sudo chmod 644`, `sudo launchctl bootstrap
  system <plist>`.
- **Agent-domain** plists: `ln -sf` into `~/Library/LaunchAgents/`,
  `launchctl bootstrap gui/$(id -u) <plist>`.

A single sudo prompt fires at the start of the activation pass (the script
calls `sudo -v` to cache credentials) so installers aren't surprised by
multiple `Password:` prompts mid-run.

The domain for each chassis-shipped plist is encoded in the `CHASSIS_PLIST_DOMAINS`
array at the bottom of `bootstrap-customer-scripts.sh`. To add a new
host-side plist:

1. Drop the template in `chassis/launchd/com.behalfbot.<name>.plist.template`.
2. Add `render_template` and `install_plist` entries in the renderer.
3. Add `"com.behalfbot.${BOT_NAME}-<name>.plist <agent|daemon>"` to
   `CHASSIS_PLIST_DOMAINS`.

For daemon-domain plists, also add `UserName` / `GroupName` keys in the
template and make sure all paths are absolute.

## Current chassis-shipped host plists

| Plist | Domain | Why |
|---|---|---|
| `com.behalfbot.<bot>-discord-restart` | **Daemon** | `docker exec` only - no GUI needed |
| `com.behalfbot.<bot>-discord-watchdog` | **Daemon** | `docker exec` only - no GUI needed |
| `com.behalfbot.heartbeat-dispatcher` | **Deprecated** | Dispatcher runs inside the chassis container now (see `docker-compose.yml`). Template retained for the legacy bare-metal V1 install path; do not promote to daemon. |

Plugins ship their own plists separately. The dating plugin's
`plugins/dating/scheduled-tasks/dating-swipe.plist.template` stays as a
LaunchAgent because the Android emulator and Playwright Chromium it drives
both need an Aqua session — see that plugin's README for the trade-off note.

## Related

- chassis#14 — original incident and fix discussion.
- `docs/LESSONS_FROM_V1.md` — running ledger of install-time failures.
- `chassis/scripts/bootstrap-customer-scripts.sh` — the renderer + installer.
