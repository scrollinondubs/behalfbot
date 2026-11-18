# Matrix channel for Claude Code

Two-way Matrix bridge for a long-lived Claude Code session. Same tool surface
and inbound notification shape as the official Discord channel plugin, over
the Matrix client-server API. Works against any homeserver (Synapse, Conduit,
Dendrite, a hosted one).

The only network destination is `MATRIX_HOMESERVER_URL`. Rooms are expected
to be unencrypted; encrypted events are skipped with a log line.

## Install

From any Claude Code session:

```
/plugin marketplace add scrollinondubs/behalfbot
/plugin install matrix@behalfbot
```

Or from a shell:

```
claude plugin marketplace add scrollinondubs/behalfbot
claude plugin install matrix@behalfbot
```

Then configure it and start a session with the channel attached:

```
/matrix:configure MATRIX_HOMESERVER_URL=https://matrix.example.org MATRIX_BOT_USER_ID=@yourbot:example.org MATRIX_ACCESS_TOKEN=<token>
/matrix:access add @you:example.org
claude --channels plugin:matrix@behalfbot
```

`matrix` is not on Anthropic's default channel allowlist, so Claude Code only
registers it when managed settings allow it. Add this to
`managed-settings.json` on the machine that runs the session (macOS:
`/Library/Application Support/ClaudeCode/`, Linux: `/etc/claude-code/`):

```json
{
  "channelsEnabled": true,
  "allowedChannelPlugins": [
    { "marketplace": "claude-plugins-official", "plugin": "discord" },
    { "marketplace": "behalfbot", "plugin": "matrix" }
  ]
}
```

`allowedChannelPlugins` replaces Anthropic's default list when set, so list
every channel plugin you run, Discord included. To try the plugin without
managed settings, `claude --dangerously-load-development-channels
plugin:matrix@behalfbot` loads it with a confirmation prompt at each start.

Several channels can run in one session: `claude --channels
plugin:discord@claude-plugins-official plugin:matrix@behalfbot`.

Requires `bun` on `PATH` (the chassis image ships it).

## Bot account

1. Register a dedicated user for the bot on your homeserver (for Synapse,
   `register_new_matrix_user`, or a normal client sign-up).
2. Get its access token: log in once with
   `curl -s -XPOST https://matrix.example.org/_matrix/client/v3/login -d '{"type":"m.login.password","identifier":{"type":"m.id.user","user":"yourbot"},"password":"..."}'`
   and keep `access_token` from the response. Store it in your password
   manager; it goes into `.env` through `/matrix:configure`, never into a repo.
3. Create a room from your own account, keep encryption off, and invite the
   bot. It auto-joins invites from allowlisted users.

## Tools

| Tool | Matrix mapping |
|---|---|
| `reply(chat_id, text, reply_to?, files?)` | `m.text` (markdown rendered to `formatted_body`), split at 16k chars by default. `reply_to` adds `m.in_reply_to` to the first part. Each file is uploaded to the media repo and sent as `m.image` / `m.audio` / `m.video` / `m.file` by mimetype. |
| `react(chat_id, message_id, emoji)` | `m.reaction` annotation |
| `edit_message(chat_id, message_id, text)` | `m.replace` with `m.new_content`; only the bot's own messages |
| `fetch_messages(channel, limit?)` | `/rooms/{id}/messages`, oldest-first, edits folded in |
| `download_attachment(chat_id, message_id)` | Fetches the event, downloads its `mxc://` media via authenticated media (`/_matrix/client/v1/media/download`) into `<state>/inbox/`, returns the path |

`chat_id` is the room ID (`!abc:server`); a room alias (`#name:server`) also
works. Tools only touch rooms the bot has joined and that pass
`MATRIX_ROOM_IDS`.

## Inbound

```
<channel source="matrix" chat_id="!room:example.org" message_id="$event" user="Alice"
         user_id="@you:example.org" ts="2026-11-18T19:24:50.977Z" room_name="Alice + Bot"
         [reply_to="$e"] [edit_of="$e"]
         [attachment_count="1" attachments="voice-message-recording.ogg (audio/ogg, 87KB, 14s, voice note)"]
         [voice="true" voice_duration_s="14"]>
```

- Only senders in `access.json` `allowedUsers` get through (re-read per message).
- `m.text`, `m.notice`, `m.emote`, `m.location`, and media `m.image`,
  `m.audio`, `m.video`, `m.file`. Reply fallbacks are stripped.
- Voice notes (Element X: `m.audio` with `org.matrix.msc3245.voice`) carry
  `voice="true"`. Downloads get the right extension (`.ogg` for audio/ogg,
  `.m4a` for audio/mp4), ready for whatever transcription step the install
  uses.
- Invites are auto-joined only when the inviter is allowlisted.
- The `since` token is persisted (`<state>/sync-token`), so restarts resume
  instead of replaying history. A first run with no token skips history.

## Several sessions, one bot

Every Claude Code session with the plugin enabled starts this server. The
first one takes `<state>/mux.lock` (flock; freed by the kernel if the process
dies), runs `/sync`, persists the token and fans events out over
`<state>/mux.sock`. The rest connect as clients and take over when the owner
exits. Only the owner sends ack reactions and typing notifications.

Known limitation: if the only live session owning sync is one that was *not*
started with `--channels`, it still advances the token, and messages that
arrive then are not delivered anywhere. Set `MATRIX_INBOUND=off` in the
environment of sessions that should never sync.

## Permission relay

Declares `claude/channel/permission`. Prompts go to the last active room
(else `MATRIX_PERMISSION_ROOM_ID`, else the first of `MATRIX_ROOM_IDS`), with
pre-added check / cross reactions. An allowlisted user answers by reacting, or
by replying `yes <id>` / `no <id>`.

## Configuration files

State dir: `~/.claude/channels/matrix/` (override with `MATRIX_CHANNELS_DIR`;
keep it short, macOS caps unix socket paths at 104 bytes). `/matrix:configure`
and `/matrix:access` write these for you.

```
# .env (chmod 600)
MATRIX_HOMESERVER_URL=https://matrix.example.org
MATRIX_BOT_USER_ID=@yourbot:example.org
MATRIX_ACCESS_TOKEN=...
MATRIX_ROOM_IDS=!a:example.org,!b:example.org   # optional

# access.json
{"allowedUsers": ["@you:example.org"]}
```

The MCP server is declared inline in `.claude-plugin/plugin.json`, so no
`.mcp.json` entry is needed.

## Not mapped

- Discord's `create_thread`: Matrix threads exist, but this plugin does not
  start them.
- Pairing codes: Matrix user IDs are stable and known up front, so access is a
  plain allowlist.
- E2EE: not supported by design. Keep the bot's rooms unencrypted.

## Development

```
bun install
bun test
```

`@modelcontextprotocol/sdk` is pinned to 1.27.1 (the version the Discord
channel ships with). Claude Code skips channel registration when a newer
protocol revision is negotiated, so bump it only after checking that.

Sync loop, multiplexer and permission relay adapted from
[zekker6/claude-code-channel-matrix](https://github.com/zekker6/claude-code-channel-matrix)
(MIT). This directory is MIT-licensed (see LICENSE), separate from the
chassis licence.
