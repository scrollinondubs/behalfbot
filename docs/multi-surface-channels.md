# Multi-surface channel posting

> How chassis scripts post to different chat platforms (Discord / Telegram / Slack) with the same channel-key convention. The pattern supports single-surface installs (Discord-only) and multi-surface installs (Discord + Telegram + Slack).

## The pattern

Three parallel scripts under `chassis/scripts/`:

| Script | Platform | Auth | API |
|---|---|---|---|
| `post-to-channel.sh` | Discord | per-channel webhook URL | `POST <webhook>` with `{content}` |
| `post-to-telegram.sh` | Telegram | bot token | `POST sendMessage` with `chat_id + text` |
| `post-to-slack.sh` | Slack | xoxb bot token | `POST chat.postMessage` with `channel + text` |

All three accept the same first two args: `<channel-key> <message>`. Channel-key is platform-agnostic (`ops`, `briefings`, `leads`, `admin`, `social`, etc.). Each script resolves the key to a platform-specific identifier via env-var lookup:

| Channel key | Discord env | Telegram env | Slack env |
|---|---|---|---|
| `ops` | `${INSTANCE_NAME}_OPS_WEBHOOK_URL` | `${INSTANCE_NAME}_OPS_TELEGRAM_CHAT_ID` | `${INSTANCE_NAME}_OPS_SLACK_CHANNEL_ID` |
| `briefings` | `${INSTANCE_NAME}_BRIEFINGS_WEBHOOK_URL` | `${INSTANCE_NAME}_BRIEFINGS_TELEGRAM_CHAT_ID` | `${INSTANCE_NAME}_BRIEFINGS_SLACK_CHANNEL_ID` |

`INSTANCE_NAME` (e.g. `OZZY`, `MARC`) is the per-installer prefix. Resolved env vars live in `$CHASSIS_HOME/.env` (hydrated from Vaultwarden per `docs/installer-vw-template.md`).

## Caller patterns

**Single-surface install (Discord-only):**

```bash
post-to-channel.sh ops "Restarted vaultwarden after OOM"
```

**Multi-surface install (Marc, Telegram primary + Slack secondary):**

```bash
post-to-telegram.sh ops "Restarted vaultwarden after OOM"
post-to-slack.sh ops "Restarted vaultwarden after OOM"
```

For now, callers fan-out explicitly. A unified dispatcher (`post-to-channel-multi.sh`) that reads `chassis.config.yaml.surfaces.primary` + `.secondary` and fans out automatically is a follow-up. Tracked in chassis follow-ups, not blocking Marc V1.

## Heartbeat scripts that need to post

Heartbeat scripts (under `chassis/scheduled-tasks/` or `scheduled-tasks/` on the installer side) that produce briefings / alerts / lead notifications should fan out per the installer's surface config. Until the multi-dispatcher lands, scripts can read the config themselves:

```bash
primary=$(yq -r '.surfaces.primary' "$CHASSIS_HOME/chassis.config.yaml")
secondary=$(yq -r '.surfaces.secondary // ""' "$CHASSIS_HOME/chassis.config.yaml")

case "$primary" in
    discord)  post-to-channel.sh  briefings "$MSG" ;;
    telegram) post-to-telegram.sh briefings "$MSG" ;;
    slack)    post-to-slack.sh    briefings "$MSG" ;;
esac

if [[ -n "$secondary" ]]; then
    case "$secondary" in
        discord)  post-to-channel.sh  briefings "$MSG" ;;
        telegram) post-to-telegram.sh briefings "$MSG" ;;
        slack)    post-to-slack.sh    briefings "$MSG" ;;
    esac
fi
```

## Telegram supergroup-topics

For installers using one Telegram supergroup with multiple forum topics (vs separate chats per channel), append `/<thread_id>` to the chat_id in the env var:

```bash
# .env line
MARC_OPS_TELEGRAM_CHAT_ID="-1001234567890/47"
```

`post-to-telegram.sh` splits the value and passes `message_thread_id=47` to the Telegram API. This matches the supergroup-with-topics pattern Marc may choose at install kickoff per `docs/installer-homework-marc.md` open question 1.

## Slack thread replies

`post-to-slack.sh` supports `--thread-ts <ts>` to reply in an existing thread. Useful for heartbeats that append to a long-running thread (e.g. a daily-ops thread that gets new alerts throughout the day instead of new top-level messages).

```bash
post-to-slack.sh ops "Auth check OK" --thread-ts "1748287200.123456"
```

## Refs

- `chassis/scripts/post-to-channel.sh` - Discord (pre-existing)
- `chassis/scripts/post-to-telegram.sh` - this PR
- `chassis/scripts/post-to-slack.sh` - this PR
- `docs/install-marc-vw-items.md` - VW item list including `TELEGRAM_BOT_TOKEN` + `SLACK_BOT_TOKEN`
- `docs/install-marc-chassis-config.yaml` - chassis config wiring `surfaces.primary: telegram + .secondary: slack`
- `<v1-reference-install>#538` - installer-2 install tracking
