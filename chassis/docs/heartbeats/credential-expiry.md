# credential-expiry heartbeat

Warns before a credential expires, on the surfaces the operator can still be
reached on.

Issue: scrollinondubs/new-jaxity#550.

## The failure this exists for

Between 2026-09-01 and 2026-09-05 the reference install's operator was
off-grid. Four credentials expired within days of each other, and every
channel the assistant had for saying so depended on one of them:

- the **Tailscale node key** hit its 180-day default expiry on ~09-01. The ssh
  hostname resolves to the Tailscale IP, so that killed ssh AND VNC at once;
- the **macOS Keychain entry** `Claude Code-credentials` lost
  `claudeAiOauth.accessToken` at ~10:06Z on the same morning, which killed the
  HOST interactive Claude session. The container was fine throughout - it reads
  `~/.claude/.credentials.json` and refreshes off the refresh token - so
  scheduled work kept running, and kept spending, while the terminal was dead;
- an **iCloud trust token** expired on 09-04.

Nothing warned. Worse, `sync-claude-oauth-bridge.sh` had detected the Keychain
loss on the first tick and written `WARN: keychain JSON missing
claudeAiOauth.accessToken` 288 times over four days into
`logs/scheduled/claude-oauth-bridge-sync.log`. 48 a day is the ceiling for a
job running every 30 minutes, and three consecutive days hit it. A monitor that
fires into a log nobody reads is not a monitor. That half is fixed separately,
in the bridge itself: it now posts once on the first fault and stays quiet
until it recovers.

## What it checks

| id | source | what a fault means |
|---|---|---|
| `tailscale-self` | `tailscale status --json`, `.Self.KeyExpiry` | ssh and VNC are about to go, or have gone |
| `tailscale-peer-<dns-label>` | `.Peer[]` with a non-null `KeyExpiry` | the operator's laptop or phone loses tailnet access |
| `claude-keychain` | `security find-generic-password -s "Claude Code-credentials"` | the host interactive `claude` cannot authenticate |
| `claude-credentials-file` | `~/.claude/.credentials.json` | the chassis container cannot authenticate |
| `claude-token-refresh` | the newest `expiresAt` across both stores | nothing has refreshed the access token, so every refresh path is dead |
| anything else | a drop-in, see below | whatever the drop-in says |

A null or absent `Self.KeyExpiry` means key expiry is **disabled** for the
node, which is the healthy state for an always-on host and the fix the
reference install applied. The check reports it as `ok`.

`BackendState: NeedsLogin` is treated as `expired`, not as a graceful skip. It
is what an expired node key actually looks like from the CLI. `Stopped` and
`NoState` are skips; a missing binary or a dead daemon is a skip too.

The **access token** deliberately does not get the T-7 / T-1 ladder. It lives
about an hour and refreshes itself, so a day-based threshold on it would alert
permanently, which is how a real monitor gets muted. The `refreshTokenExpiresAt`
gets the ladder instead, and the access token only produces
`claude-token-refresh` when the newest one across both stores has been expired
for longer than the grace window (default 24h).

## Where it can see what

The dispatcher runs INSIDE the chassis container.

| check | host | container |
|---|---|---|
| `tailscale-*` | yes | no, there is no tailscaled |
| `claude-keychain` | yes | no, there is no macOS Keychain |
| `claude-credentials-file` | yes | yes, through the bind mount |
| `claude-token-refresh` | yes | yes |
| drop-ins | both | both |

So the heartbeat registration alone covers only the bottom three rows on a
containerized install. The two checks that would have caught the 2026-09-01
outage are host-only. And the heartbeat's delivery path is `claude -p`, while
one of the credentials being watched is Claude's own - routing that alert
through the host interactive session would route it through the thing that just
broke.

**On a containerized install, wire the host-side runner. It is the
load-bearing half, not a nice-to-have.**

## Host-side runner

`chassis/scripts/credential-expiry-alert.sh` runs the same gather and posts
straight to the ops channel via webhook. No model, no dispatcher, no ssh.

Preview what it would say without posting anything or consuming a threshold:

```bash
CUSTOMER_HOME=~/.behalfbot bash <chassis>/scripts/credential-expiry-alert.sh --dry-run
```

### macOS: a LaunchAgent, never a LaunchDaemon

`security find-generic-password` cannot unlock the login keychain from
launchd's Background session and fails with `User interaction is not allowed`,
which would turn the Keychain check into a permanent false alarm reporting
`unknown`. See `docs/launchd-domains.md` for the full trap - the same one that
broke Vaultwarden hydration for five weeks in 2026.

`~/Library/LaunchAgents/com.behalfbot.<bot>-credential-expiry.plist`:

```xml
<key>ProgramArguments</key>
<array>
  <string>/bin/bash</string>
  <string>/Users/&lt;user&gt;/behalfbot/chassis/scripts/credential-expiry-alert.sh</string>
</array>
<key>EnvironmentVariables</key>
<dict>
  <key>CUSTOMER_HOME</key><string>/Users/&lt;user&gt;/.behalfbot</string>
</dict>
<key>StartInterval</key><integer>3600</integer>
<key>RunAtLoad</key><true/>
```

```bash
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.behalfbot.<bot>-credential-expiry.plist
```

### Linux

A user systemd timer, or cron:

```
17 * * * * CUSTOMER_HOME=$HOME/.behalfbot /bin/bash <chassis>/scripts/credential-expiry-alert.sh
```

### Running in both places is safe

Suppression state is per-check, not per-run. A run that cannot see the Keychain
leaves that check's record alone rather than fighting the other runner over a
whole-run fingerprint. The state file lives under `CUSTOMER_HOME`, which both
sides reach through the same bind mount, and it is written atomically.

For a check both sides CAN see, whichever runs first consumes the transition,
so the operator hears about it once - through the webhook or through the
heartbeat, not both.

## Repeat suppression

Each check resolves to a **stage**: `ok`, `t7`, `t1`, `warn`, `expired`,
`missing`, `unknown`. The stage is what gets recorded and compared. A check
fires when its stage changed since the last recorded alert, so T-7 fires once,
then silence, then T-1 fires once.

`expired`, `missing` and `unknown` additionally re-nag once the last alert is
older than `CHASSIS_CREDENTIAL_REALERT_DAYS` (default 3), because those states
do not self-resolve and an unanswered notification is not a dismissal.
`t7` / `t1` / `warn` never re-nag: a 3-day re-nag on `t7` would fire again at
T-4, which is the daily nagging the design rules out.

Returning to `ok` clears the record, so a later re-degrade fires again from
scratch.

**`days_remaining` is deliberately absent from both the state record and
`alert_signature`.** A fingerprint carrying a volatile counter never matches
its own previous value, so the cooldown never applies and the alert fires on
every tick. That failure has shipped here before, and
`test-credential-expiry.sh` cases 12 to 16 exist to keep it shipped-once: nine
of them fail against a build that puts the day count back in.

## Adding your own checks

Drop an executable file in
`${CUSTOMER_HOME}/scheduled-tasks/credential-checks.d/`. Nothing in `chassis/`
needs editing - that directory lives under `CUSTOMER_HOME` by design.

Print a JSON array, or one record per line:

```bash
#!/usr/bin/env bash
# credential-checks.d/icloud.sh
SESSION=~/.config/icloudpd/session
EXPIRES=$(( $(stat -f %m "$SESSION") + 30 * 86400 ))
printf '{"id": "icloud-trust-token", "label": "iCloud trust token", "expires_at": %s, "detail": "re-run scripts/icloud-login.sh and answer the 2FA prompt"}\n' "$EXPIRES"
```

Fields:

| field | required | notes |
|---|---|---|
| `id` | yes in practice | the suppression key. Defaults to the filename, so two records from one script need explicit ids |
| `label` | no | human name for the alert. Defaults to `id` |
| `expires_at` | one of these | epoch seconds, epoch milliseconds, or ISO8601. The stage is derived from it |
| `state` | one of these | `ok` / `warn` / `expired` / `missing` / `unknown`, when the script would rather decide for itself. Anything else clamps to `unknown` |
| `detail` | no | shown verbatim in the alert. Say how to renew it |

A drop-in that exits non-zero or emits unparseable output is reported as
`unknown` rather than dropped: a credential check that cannot run is a
credential nobody is watching. Keep them fast - this gather runs on every
dispatcher tick.

## Configuration

| var | default | what it does |
|---|---|---|
| `CHASSIS_CREDENTIAL_CHECKS` | `tailscale,claude` | built-in groups to run. Empty string runs drop-ins only |
| `CHASSIS_CREDENTIAL_CHECKS_DIR` | `$CUSTOMER_HOME/scheduled-tasks/credential-checks.d` | drop-in directory |
| `CHASSIS_CREDENTIAL_STATE` | `$CUSTOMER_HOME/scheduled-tasks/credential-expiry-state.json` | suppression state |
| `CHASSIS_CREDENTIAL_WARN_DAYS` | `7` | first threshold |
| `CHASSIS_CREDENTIAL_URGENT_DAYS` | `1` | second threshold |
| `CHASSIS_CREDENTIAL_REALERT_DAYS` | `3` | re-nag window for the states that do not self-resolve |
| `CHASSIS_CREDENTIAL_TAILSCALE_BIN` | auto | PATH, then `/Applications/Tailscale.app/Contents/MacOS/Tailscale` |
| `CHASSIS_CREDENTIAL_TAILSCALE_PEERS` | all | comma list of peer DNS labels or hostnames to narrow to |
| `CHASSIS_CREDENTIAL_KEYCHAIN_SERVICE` | `Claude Code-credentials` | keychain service name |
| `KEYCHAIN_ACCOUNT` | `$USER` | keychain account. Same var the oauth bridge reads |
| `CLAUDE_CREDENTIALS_FILE` | `$HOME/.claude/.credentials.json` | |
| `CHASSIS_CREDENTIAL_REFRESH_GRACE_HOURS` | `24` | how long the access token may sit expired before the refresh loop counts as dead |
| `CHASSIS_ALERT_CHANNEL` | `ops` | channel key for the host runner, resolved by `post-to-channel.sh` |
| `CHASSIS_ALERT_CMD` | unset | full command line to deliver alerts instead, e.g. `post-to-slack.sh ops` |

## Secrets

The gather emits token presence, expiry epochs, hostnames and states. Never a
token value, and never the raw Keychain JSON. This matters because the
dispatcher logs gather stdout verbatim; `test-credential-expiry.sh` asserts it
directly.

## Tests

```bash
bash chassis/scripts/test-credential-expiry.sh              # 58 cases
bash chassis/scripts/test-claude-oauth-bridge-alert.sh      # 27 cases
```

Both stub `tailscale` and `security` on PATH and need no network, no docker,
no real keychain and no tailnet. Both are wired into `shell-tests.yml`.
