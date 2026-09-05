# Remote kill switch

Type `halt` in the control channel. Every heartbeat stops at the next
dispatcher tick. Type `resume` to bring it back.

That is the whole feature. The rest of this page is why it is built the way it
is, and precisely what it does not cover.

## Why it exists

From `scrollinondubs/new-jaxity#550`. Between 2026-09-01 and 2026-09-05 the
reference install's operator was off-grid camping. In that window:

- A heartbeat called `repo-drift` fired every 30 minutes on a condition that
  could never self-clear (a dirty working tree only he could clean). 514 model
  invocations, $45.79, 66% of all scheduled spend for the period, and 50+
  near-identical messages into a Discord channel.
- His Tailscale node key hit its 180-day expiry the same morning. No ssh, no
  VNC.
- The macOS Keychain lost the Claude Code OAuth token. The host `claude`
  session that serves Discord was dead, so replies in the channel reached
  nobody.

He could watch the alerts arrive on his phone for four days and had no way to
stop them. He drove home and pulled the power cord out of the Mac Mini.

Conservation mode already existed and would have stopped most of that spend.
It is flipped by `scripts/conservation-mode.sh`, which needs a shell, which is
the one thing he did not have.

So the requirement is narrow and unusual: **the stop path must not share a
dependency with anything that was broken.** Not the shell, not ssh, not
Tailscale, not the Claude session, not the dispatcher.

## How it works

`chassis/scripts/discord-control-listener.py` is a standalone process. It polls
the Discord REST API with the bot token, matches a short fixed lexicon from the
install's principal, writes the dispatcher's flag files directly, and acks in
the channel. There is no model in the path and no shell in the path.

It runs as a supervised child of the dispatcher loop in the container
entrypoint. Existing installs gain it by pulling a new image - no compose
change. Installs that want stronger isolation can run the same image with the
`control-listener` mode as a separate service instead.

```
docker compose run --rm chassis control-listener
```

### Host-side (non-container) installs

The listener is started by the container entrypoint. An install that runs the
dispatcher directly from launchd or systemd gets the halt **gate** (the
dispatcher reads `halt.json` either way) but nothing starts the listener, so
there is no way to write that file from Discord. Those installs need their own
supervisor for `chassis/scripts/discord-control-listener.py` - a LaunchAgent or
a systemd unit alongside the dispatcher's own. Not shipped yet.

## Commands

Match is on the whole message, case-insensitive. A leading bot mention and
trailing punctuation are stripped. A control word inside a sentence is
conversation, not a command - "we should probably halt the drift alert" does
nothing.

| Command | Aliases | Effect |
|---|---|---|
| `halt [<duration>] [<reason>]` | `mute`, `silence` | Skip EVERY heartbeat, `critical` included |
| `resume` | `unmute`, `unhalt` | Clear halt AND conservation |
| `conserve [<duration>] [<reason>]` | `throttle`, `conservation on` | Skip `normal` and `background`; `critical` still runs |
| `unconserve` | `conservation off` | Clear conservation only |
| `status` | | Report both flags. Changes nothing |

`<duration>` is `30m`, `2h`, `3d`. It becomes `auto_lift_after`.

**Omitting the duration means no auto-lift.** That is deliberate. An auto-resume
that fires while the operator is still off-grid restarts the outage, and by
definition they are not there to see it happen.

`resume` clears both flags, so one word is enough under pressure.

## What halt stops, and what it does not

Stops, from the next dispatcher tick (within `DISPATCHER_INTERVAL_SECONDS`,
15 minutes by default):

- Every heartbeat in `HEARTBEATS.md`, regardless of `criticality`. This is the
  difference from conservation mode, which lets `critical` through.
- Therefore every `claude -p` invocation the dispatcher would have made, and
  every message those would have sent.

Does **not** stop:

- **An in-flight tick.** The gate is evaluated at tick start. A `claude -p`
  already running finishes, up to about 40 minutes with the retry path.
- **Host-side launchd / systemd jobs.** The iCloud poller, `discord-restart`,
  `discord-watchdog`, plugin-owned agents. They are not children of the
  dispatcher and this flag is invisible to them.
- **A live interactive Claude session.** If one is up it can still talk.
- **The listener itself.** It has to stay up to hear `resume`.

The ack posted in-channel says all of this, because in the moment that ack is
the only documentation the operator has.

## Conservation mode, unchanged

`conserve` writes the same `scheduled-tasks/conservation-mode.json` that
`scripts/conservation-mode.sh` and `gather-quota-check.sh` have always written,
with the same five keys. Nothing about conservation mode's semantics changed.
The halt flag is a second file with the same schema, checked earlier and
ignoring `criticality`.

```json
{
  "enabled": true,
  "enabled_at": "2026-09-05T10:29:00Z",
  "enabled_by": "discord-control-listener:<principal snowflake>",
  "auto_lift_after": null,
  "reason": "runaway repo-drift"
}
```

Files:

| Path | Meaning |
|---|---|
| `$CUSTOMER_HOME/scheduled-tasks/halt.json` | Total stop |
| `$CUSTOMER_HOME/scheduled-tasks/conservation-mode.json` | Throttle |
| `$CUSTOMER_HOME/state/control-listener-cursor.json` | Last message id read per channel |

All three are customer state, gitignored, and never tracked in the chassis
tree. `halt.json` is included in the S3 state backup alongside the others.

## Authorisation

Only the install's principal. The listener reads `CHASSIS_PRINCIPAL_USER_ID`
(the same id `chassis/.claude/hooks/principal-policy-hook.sh` uses), falling
back to `INSTALLER_DISCORD_USER_ID` (the id `bootstrap-discord-access.sh`
already requires). Messages from anyone else in the channel are ignored
entirely - no ack, no log noise beyond the cursor advancing.

The principal-policy hook fails **open** when no principal id is configured: a
misconfigured install should not lose its prompts. This listener fails
**closed**. With no principal id it refuses to start, exits 78, and the
entrypoint writes the reason into the container log:

```
WARN: remote kill switch UNARMED - set CHASSIS_PRINCIPAL_USER_ID (or
WARN: INSTALLER_DISCORD_USER_ID), DISCORD_BOT_TOKEN and a control channel.
```

A kill switch that silently never arms is the exact failure shape of #550.
`bootstrap-audit.sh` also checks for it.

## Configuration

Set in the customer `.env` (values are runtime-only and never committed):

| Variable | Required | Meaning |
|---|---|---|
| `DISCORD_BOT_TOKEN` | yes | Bot token. Needs View Channel + Read Message History + Send Messages on the control channel |
| `CHASSIS_PRINCIPAL_USER_ID` | yes | Principal's Discord snowflake. `INSTALLER_DISCORD_USER_ID` is accepted as a fallback |
| `CHASSIS_CONTROL_CHANNEL_IDS` | no | Comma or space separated channel ids to watch. Defaults to `DISCORD_PRIMARY_CHANNEL_ID` |
| `CHASSIS_CONTROL_POLL_SECONDS` | no | Poll cadence, default 20 |
| `CHASSIS_CONTROL_LOOKBACK_SECONDS` | no | Restart replay cap, default 3600 |

Log: `$CUSTOMER_HOME/logs/scheduled/control-listener.log`.

## Design notes worth keeping

**The cursor only ever reads forward.** On a cold start the listener
synthesises a snowflake from the current time and reads from there. A
persisted cursor is honoured but capped at `CHASSIS_CONTROL_LOOKBACK_SECONDS`.
A listener that replayed history on restart would re-execute a four-day-old
`halt`, or worse a `resume` sent before the thing it was meant to stop.

**State is written before the ack.** If Discord is unreachable the halt still
takes effect. The ack is the operator's confirmation, not the mechanism.

**The poll loop swallows everything.** Every HTTP failure returns `None` and
the loop continues. Being alive to hear `resume` is the entire value of this
process; there is no failure worth dying for.

**Polling, not a gateway websocket.** A gateway connection would be lower
latency and a heavier dependency (a library, an intent configuration, a
reconnect state machine). Twenty-second polling on one channel is a handful of
requests a minute against a 50-per-second bucket, and it uses the same REST
surface `discord-post.sh` has used since 2026-05-27.

**No `stop` in the lexicon.** People type "stop" mid-conversation to interrupt.
The words that flip a flag are ones nobody types by accident.
