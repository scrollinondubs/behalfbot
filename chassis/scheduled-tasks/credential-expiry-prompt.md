# Credential expiry alert

The scheduled `credential-expiry` check found a credential that is expired,
missing, or inside a warning threshold. This exists because of
scrollinondubs/new-jaxity#550: over 2026-09-01 to 2026-09-05 four credentials
on the reference install expired within days of each other, every channel for
reporting it depended on one of them, and the install went dark for four days
with nothing said. Treat anything here as a path back in that is about to
close.

You will receive the gather output as JSON:

```json
{"count": N, "issues": ["<id>_<stage>", ...], "checked": M,
 "status": "alerting", "alert_signature": "...", "unhealthy": [...],
 "credentials": [{"id": "...", "label": "...", "stage": "...",
                  "days_remaining": 3.2, "expires_at": 1789200000,
                  "detail": "..."}],
 "skipped": [{"check": "...", "reason": "..."}]}
```

`issues` is what fired THIS run. `unhealthy` is everything currently in a bad
state, including conditions already alerted about inside the cooldown - report
on `issues`, and use `unhealthy` only for context.

## Stages

| stage | meaning |
|---|---|
| `t7` | inside the first threshold, default 7 days. A heads-up. |
| `t1` | inside the urgent threshold, default 1 day. Act today. |
| `expired` | already gone. Something is broken right now. |
| `missing` | the credential is absent where it should be. Same urgency as expired. |
| `unknown` | the CHECK could not run. Not the same as healthy - a credential nobody is watching. |
| `warn` | a drop-in check's own judgement. Read its `detail`. |

## Triage by check

- **`tailscale-self`** - this host's node key. `expired` means ssh and VNC are
  probably both gone, because the ssh hostname usually resolves to the
  Tailscale IP. The fix is `tailscale up`, and the durable fix is disabling key
  expiry for an always-on host in the admin console - a null `KeyExpiry` is the
  healthy state this check reports as `ok`.
- **`tailscale-peer-<name>`** - a laptop or phone on the tailnet. Less urgent
  than self, and still worth saying: the operator's own device is how they get
  back in when the host is unreachable.
- **`claude-keychain`** - the macOS Keychain entry `Claude Code-credentials`.
  `missing` kills the HOST interactive Claude session and needs `/login` on the
  host. The container is NOT affected - it reads `~/.claude/.credentials.json`
  and refreshes off the refresh token - so scheduled work keeps running and
  keeps spending while the terminal is dead. That asymmetry is exactly what
  made the 2026-09-01 outage read as healthy from the inside.
- **`claude-credentials-file`** - `~/.claude/.credentials.json`. `missing` is
  the container's authentication, so heartbeats will start failing.
- **`claude-token-refresh`** - nothing has refreshed the access token in over
  the grace window (default 24h). The oauth bridge or the container refresh
  path is dead. Check `logs/scheduled/claude-oauth-bridge-sync.log`.
- **anything else** - a customer drop-in check from
  `scheduled-tasks/credential-checks.d/`. Its `detail` is written by whoever
  added it and should say how to renew.

## What to do

1. Post ONE message to the install's alerts channel naming each fired
   credential, its stage, and the concrete renewal step. Lead with anything
   `expired` or `missing`.
2. Do NOT try to renew anything yourself. Every credential here is an
   interactive re-auth (`tailscale up`, `/login`, an app-specific sign-in) and
   several are the operator's personal identity. Surface, do not act.
3. If `skipped` is non-empty, mention it only when it means a check the install
   expects to be running is not - e.g. `tailscale not installed` on a host that
   is meant to be on the tailnet.
4. **Never quote a token value.** The gather emits presence, epochs and
   hostnames only, and nothing you add should change that.

## Known blind spot: where this heartbeat can and cannot see

The dispatcher runs INSIDE the chassis container. There is no macOS Keychain
and no tailscaled in there, so on a containerized install this heartbeat sees
only `claude-credentials-file`, `claude-token-refresh` and the drop-ins. The
two checks that would have caught the 2026-09-01 outage are host-only.

Worse, this heartbeat's own delivery path is `claude -p`, and one of the
credentials being watched is Claude's own.

So the host-side runner is not optional on a containerized install:

```
# macOS: a LaunchAgent, NOT a LaunchDaemon (a Background-session job cannot
# unlock the login keychain - see docs/launchd-domains.md)
/bin/bash <chassis>/scripts/credential-expiry-alert.sh
```

It runs the same gather and posts straight to the ops webhook with no model
call. Full wiring in `chassis/docs/heartbeats/credential-expiry.md`.

## Heartbeat registration

```yaml
## credential-expiry

```yaml
schedule: every 1h
gather: state/chassis-root/scripts/gather-credential-expiry.sh
condition: threshold count > 0
prompt: state/chassis-root/scheduled-tasks/credential-expiry-prompt.md
model: sonnet
budget: 1
criticality: critical
```
```

`criticality: critical` - a credential expiring while the install is in
conservation mode is the case that hurts most, since conservation usually
means nobody is watching.
