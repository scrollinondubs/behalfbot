# Matrix as an alert transport (opt-in)

An install that moved its operator chat to Matrix can send chassis alerts there
and run the remote kill switch there. Nothing changes unless the install opts
in: with `CHASSIS_ALERT_TRANSPORT` unset every alert goes to Discord exactly as
before.

Origin: `scrollinondubs/new-jaxity#756`.

## Alerts

`CHASSIS_ALERT_TRANSPORT` picks the chat app. It is read from the environment,
then from the install's `.env.baked` or `.env`, so host-side launchd jobs that
start with an empty environment see it too.

| Value | Behaviour |
|---|---|
| unset, `discord` | Discord only. The previous behaviour, byte for byte |
| `matrix` | Matrix. When Matrix fails, falls back to Discord and says so on stderr |
| `both` | Both, always both attempted. Success when either delivered |

`both` is the soak setting: run it for a few days, check nothing went missing,
then switch to `matrix`. It counts as delivered when one leg worked, because
the edge-triggered callers (the oauth bridge, credential expiry) retry on
failure, and a strict "both or nothing" would repost the working leg on every
tick while the other leg is down.

Who honours it:

- `chassis_alert` in `chassis/scripts/_alert.sh`: credential expiry, the
  oauth bridge, TripWires. `CHASSIS_ALERT_CMD`, when set, still overrides
  everything.
- `chassis/scripts/alert-post.sh <alerts|ops|dm> <text|@file> [files...]`:
  the single entrypoint the shipped prompts now use. `--discord-channel <id>`
  pins the Discord side for callers that carry their own channel id
  (`TRIPWIRE_CHANNEL_ID`, `SPEND_ALERT_CHANNEL_ID`).
- `gather-spend-snapshots.py` alerts, through `alert-post.sh`.

The Matrix side is `chassis/scripts/matrix-post.py`, standard library only. It
sends `m.text` (so a phone notifies) with a `formatted_body` that renders
`**bold**`, `` `code` `` and `[text](https://link)`. Files are uploaded to the
homeserver's media repo and sent as `m.image`, `m.video`, `m.audio` or
`m.file`, after the text.

## Kill switch

`chassis/scripts/matrix-control-listener.py` is the Matrix twin of
`discord-control-listener.py`: the same lexicon (`halt`, `resume`, `conserve`,
`unconserve`, `status` and their aliases), the same flag files, the same acks.
It imports the parser, the flag writer and the ack text from the Discord
listener, so the two cannot drift. `enabled_by` reads
`matrix-control-listener:@principal:example.org`.

The dispatcher entrypoint supervises it only when `CHASSIS_MATRIX_CONTROL=1`.
It runs beside the Discord listener, not instead of it. Standalone:
`docker compose run --rm chassis matrix-control-listener`.

Rules:

- Only `m.text` from the exact user id in `CHASSIS_MATRIX_PRINCIPAL`. Display
  names are never compared.
- Events older than 10 minutes are dropped, so a restart never replays a
  countermanded command.
- Edits are ignored. A reply's quoted fallback is stripped before matching.
- **Encrypted rooms do not work.** The listener holds no keys. Element creates
  DMs encrypted by default, so make the control room unencrypted. An encrypted
  control room found at start refuses to arm (exit 78, reason in the container
  log) instead of listening to a room it cannot read.
- It polls `/rooms/{room}/messages` with its own cursor, never `/sync`, so it
  cannot consume to-device messages meant for another client on the same token.

## Configuration

All in the install's `.env`. Values are runtime-only and never committed.
When the install has a `.env.baked`, re-run `bake-env.sh` after adding them:
the container and these scripts read the baked file first.

`host.docker.internal` resolves on Docker Desktop and colima. On Linux Docker
it does not by default, so either set `CHASSIS_MATRIX_HOMESERVER` to an
address the container can reach or add `host.docker.internal:host-gateway` to
the service's `extra_hosts`.

| Variable | Used by | Meaning |
|---|---|---|
| `CHASSIS_ALERT_TRANSPORT` | alerts | `discord` (default), `matrix` or `both` |
| `CHASSIS_MATRIX_HOMESERVER` | both | Default `http://127.0.0.1:8008`. Inside the container a loopback host is rewritten to `host.docker.internal`, so one `.env` works on both sides |
| `CHASSIS_MATRIX_TOKEN_FILE` | both | JSON file with `access_token`. Relative paths resolve against `CUSTOMER_HOME`. Never printed |
| `CHASSIS_MATRIX_ALERTS_ROOM` | alerts | Room id for `alerts`. Falls back to the ops room, then the DM room |
| `CHASSIS_MATRIX_OPS_ROOM` | alerts | Room id for `ops`. Falls back to the alerts room, then the DM room |
| `CHASSIS_MATRIX_DM_ROOM` | both | Room id for `dm`, and the default control room |
| `CHASSIS_MATRIX_CONTROL` | kill switch | `1` to start the Matrix listener |
| `CHASSIS_MATRIX_PRINCIPAL` | kill switch | The operator's full Matrix user id |
| `CHASSIS_MATRIX_CONTROL_ROOMS` | kill switch | Optional room ids to watch instead of the DM room |
| `CHASSIS_MATRIX_CONTROL_POLL_SECONDS` | kill switch | Default 15 |

Log: `$CUSTOMER_HOME/logs/scheduled/matrix-control-listener.log`. Cursor:
`$CUSTOMER_HOME/state/matrix-control-listener-cursor.json`.
