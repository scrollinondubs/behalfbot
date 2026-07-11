# LaunchDaemon vs LaunchAgent - which to use

> Decision rule for every chassis-shipped (and plugin-shipped) host-side
> `launchd` plist on macOS. Wrong choice silently breaks either unattended
> reboot recovery (agent) or the login keychain (daemon). The keychain failure
> is the expensive one: it is silent, it takes down every Vaultwarden-sourced
> credential, and it survives restarts.
>
> This doc previously said "default: LaunchDaemon". That rule was wrong and it
> produced a five-week outage. Read the decision rule below before you touch a
> plist.

## The rule

**If the job, or ANYTHING IT SPAWNS, touches the user login keychain or runs a
Claude process, it must be a gui-domain LaunchAgent.**

LaunchDaemons are only for jobs that are genuinely headless and keychain-free.

"Anything it spawns" is load-bearing. A job that runs a two-line shell script
which starts a tmux session which runs `claude` which shells out to
`bw-unlock.sh` which calls `security find-generic-password` IS a job that
touches the login keychain. The chain is invisible in the plist. Follow it.

## TL;DR

| Domain | Path | Loads at | Login keychain? | Survives unattended reboot? |
|---|---|---|---|---|
| **LaunchAgent** (user) | `~/Library/LaunchAgents/` | GUI (Aqua) login | **Yes** | Only with auto-login on |
| LaunchDaemon (system) | `/Library/LaunchDaemons/` | Boot | **No** | Yes |

Quick self-test from inside any job: `launchctl managername` prints `Aqua` in a
gui LaunchAgent and `Background` in a LaunchDaemon (verified 2026-07-11 on the
reference Mac Mini). Anything reporting `Background` cannot get at the login
keychain.

## Why a daemon cannot reach the login keychain

A LaunchDaemon runs in launchd's **Background** session. That is true even with
`UserName` set - `UserName` changes the uid the process runs as, not the
session it runs in. The user's login keychain is unlocked BY the GUI login; a
Background session has no GUI to unlock it and cannot prompt for one. So
`security find-generic-password` returns **error 36, "User interaction is not
allowed"**, and every credential behind Vaultwarden fails.

## The tradeoff, stated honestly

chassis#14 promoted the discord jobs to daemons for a real reason: a
LaunchAgent only loads after GUI login, so on an installer Mac that reboots
unattended with auto-login disabled, no agent ever registers and the bot stays
down until someone logs in.

**On macOS you cannot have both login-keychain access and pre-login startup.**
The login keychain is unlocked by the user login. There is no clever fix, no
`SessionCreate` trick, no daemon-plus-`launchctl asuser` workaround that gets a
Background job into an unlocked login keychain.

Correct guidance, and the one the chassis ships:

- Use a **LaunchAgent**.
- **Enable auto-login on the install Mac** (System Settings > Users & Groups >
  Automatically log in as). That gives you back unattended-reboot recovery: the
  Mac boots, logs in, the Aqua session materialises, the agents load, and the
  keychain is unlocked.
- If the install Mac genuinely cannot auto-login (FileVault with no unlock
  policy, shared hardware, a security requirement), accept that the bot needs a
  human login after a reboot. Do NOT "fix" it by going back to a daemon: that
  trades a visible outage for a silent one.

## The two traps that hid the bug for five weeks

1. **tmux runs ONE server per user socket** (`/tmp/tmux-<uid>/default`).
   Whichever job creates the server fixes the launchd session for EVERY session
   on it, and for every process those sessions spawn. One Background-domain job
   spawning any tmux session poisons the server for all of them. Converting only
   some of your tmux-spawning jobs to agents achieves nothing.

2. **`tmux kill-session` does not fix a poisoned server.** The restart scripts
   killed the session, not the server, so the Background-born server survived
   every daily 05:00 restart. The reference install stayed poisoned from
   2026-06-03 to 2026-07-11 through ~40 restarts.

The chassis restart script now records the provenance of the tmux server it
creates (`$CUSTOMER_HOME/scheduled-tasks/tmux-server-session.state`), and when
it runs under Aqua and finds a server it did not create under Aqua, it kills the
SERVER so launchd rebuilds it in the right session. The watchdog detects the
same condition and delegates to the restart script. A Background-domain run of
either script never kills the server - that would let a stray daemon re-poison
it on every tick.

## When to use which

Use **LaunchAgent** if the job (or anything downstream of it):

- Reads or writes the user login keychain (`security`, Vaultwarden unlock,
  Claude OAuth bridge sync)
- Runs a `claude` process, or spawns a tmux session that runs one
- Shares a tmux server with any job that does either of the above
- Drives an Android emulator (needs Aqua to render the AVD window)
- Drives a Playwright Chromium / Firefox / WebKit instance (`headless: true`
  does NOT exempt you on macOS)
- Touches `pasteboard`, `screencapture`, or any AppKit API that needs the user's
  `WindowServer` session
- Talks to a GUI app via Apple Events / AppleScript

Use **LaunchDaemon** only if the job is all of these:

- Never reads the keychain, directly or through a child process
- Never runs Claude, and never touches the user's tmux server
- Is otherwise pure infrastructure: a network poll, a file sync, a container
  lifecycle hook, a log rotator

If you are unsure, choose **agent**. The cost of a wrong agent is a bot that
needs a login after a reboot, which is loud and obvious. The cost of a wrong
daemon is silently broken credentials, which is neither.

## Agent plist requirements

Agents need no `UserName` / `GroupName` - a LaunchAgent already runs as the
logged-in user. Those keys are daemon-only; putting them in an agent is a smell
that the plist was copied from a daemon.

Keep paths absolute anyway (`/Users/<installer>/...`, not `~`), and set
`HOME` explicitly in `EnvironmentVariables`. launchd does not run a login shell,
so `PATH` also has to be spelled out in the plist.

## How chassis installs them

`chassis/scripts/bootstrap-customer-scripts.sh` renders the templates from
`chassis/launchd/*.plist.template` into `${CUSTOMER_HOME}/launchd/`, then - when
invoked with `--activate-plists` - installs each rendered plist into its domain:

- **Agent-domain** plists: `ln -sf` into `~/Library/LaunchAgents/`,
  `launchctl bootstrap gui/$(id -u) <plist>`. No sudo.
- **Daemon-domain** plists: `sudo cp` to `/Library/LaunchDaemons/`,
  `sudo chown root:wheel`, `sudo chmod 644`, `sudo launchctl bootstrap system
  <plist>`. (No chassis-shipped plist uses this path today.)

The domain for each chassis-shipped plist is encoded in the
`CHASSIS_PLIST_DOMAINS` array in `bootstrap-customer-scripts.sh`. To add a new
host-side plist:

1. Drop the template in `chassis/launchd/com.behalfbot.<name>.plist.template`.
2. Add a `render_template` call in the renderer.
3. Add `"com.behalfbot.${BOT_NAME}-<name>.plist <agent|daemon>"` to
   `CHASSIS_PLIST_DOMAINS`.

Run activation **outside tmux**. The discord-restart agent fires on load and
rebuilds the tmux server, which would kill an activating shell that lives inside
it. Both `bootstrap.sh` and `bootstrap-customer-scripts.sh` refuse to activate
from inside tmux unless `BOOTSTRAP_ALLOW_TMUX=1`.

## Migrating an install off the #14 LaunchDaemons

Installs bootstrapped between 2026-06-03 and this fix have
`com.behalfbot.<bot>-discord-{restart,watchdog}.plist` in
`/Library/LaunchDaemons/`. A leftover daemon is not inert: it fires the same
restart script from the Background session and recreates a keychain-blind tmux
server, fighting the new agent forever. Remove it first.

```sh
sudo launchctl bootout system/com.behalfbot.<bot>-discord-restart
sudo launchctl bootout system/com.behalfbot.<bot>-discord-watchdog
sudo rm -f /Library/LaunchDaemons/com.behalfbot.<bot>-discord-restart.plist
sudo rm -f /Library/LaunchDaemons/com.behalfbot.<bot>-discord-watchdog.plist

# then, from a shell that is NOT inside tmux:
CUSTOMER_HOME=~/.behalfbot CHASSIS_HOME=~/behalfbot BOT_NAME=<bot> \
  bash chassis/scripts/bootstrap-customer-scripts.sh --activate-plists
```

`bootstrap.sh` detects the stale daemons and offers to run the removal for you
(it prints the exact commands and prompts; it never sudos silently). Both
scripts refuse to install the agents while a daemon of the same label exists.

Activation fires the restart agent, which finds a tmux server with no Aqua
provenance record, kills the server, and rebuilds it under Aqua. Other tmux
sessions on that server go down with it and come back via their own watchdogs -
on an Aqua-born server this time. Verify:

```sh
launchctl print gui/$(id -u)/com.behalfbot.<bot>-discord-restart | head
tmux ls
tmux send-keys -t <bot>-discord ...   # or just:
launchctl managername                  # from inside the new tmux: expect Aqua
security find-generic-password -s <some-item> -w >/dev/null && echo keychain OK
```

## Current chassis-shipped host plists

| Plist | Domain | Why |
|---|---|---|
| `com.behalfbot.<bot>-discord-restart` | **Agent** | Spawns a host tmux session running `claude` - needs the login keychain |
| `com.behalfbot.<bot>-discord-watchdog` | **Agent** | Invokes the restart script; shares the same tmux server |
| `com.behalfbot.heartbeat-dispatcher` | **Deprecated** | Dispatcher runs inside the chassis container now (see `docker-compose.yml`). Template retained for the legacy bare-metal V1 install path. |

Plugins ship their own plists separately. The dating plugin's
`plugins/dating/scheduled-tasks/dating-swipe.plist.template` is a LaunchAgent
because the Android emulator and Playwright Chromium it drives both need an Aqua
session - and, by this doc's rule, because it runs Claude.

## Related

- chassis#14 - the daemon sweep that introduced the regression. Its premise
  ("this job only docker execs the chassis container") was factually wrong.
- scrollinondubs/new-jaxity#271 - the customer-side fix, proven on the reference
  install 2026-07-11.
- `docs/LESSONS_FROM_V1.md` - running ledger of install-time failures.
- `chassis/scripts/bootstrap-customer-scripts.sh` - the renderer + installer.
