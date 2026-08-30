# docker-prune Alert

The weekly docker-prune heartbeat failed. The gather emitted `count > 0` with one of:

| `status` | What broke | Fix |
|---|---|---|
| `docker_unreachable` | `docker info` returned non-zero inside the chassis container. The host's `/var/run/docker.sock` bind-mount is missing, the daemon is down, or container UID lacks socket permissions. | Verify the compose `volumes:` block still has `${DOCKER_SOCKET:-/var/run/docker.sock}:/var/run/docker.sock`. Check the host's Docker Desktop / Engine is running. On Linux installs, ensure the chassis user is in the `docker` group. |
| `prune_failed` | `docker builder prune` OR `docker image prune` returned non-zero. The `detail` line carries the tail of stderr. | Read the detail. Common causes: daemon under load, disk full to the point prune itself fails, a container holding an exclusive lock. |

## Your job

1. Classify the failure from `status` + `detail`.
2. Post one concise alert to the install's alerts channel via the discord MCP `reply` tool: date, status tag, best-guess cause, fix suggestion. The display label is `discord_channels.alerts_label` in `${CHASSIS_HOME}/chassis.config.yaml`; the runtime channel ID is `DISCORD_ALERTS_CHANNEL_ID` in the install's `.env`.
3. If the same status fires for 2+ consecutive weeks, file-or-comment on the install's own repo (the `origin` remote of `${CUSTOMER_HOME}`, not the chassis upstream) using the dedup helper.

## What NOT to do

- Don't retry the prune from this prompt. The next weekly tick handles retry. If the disk is critical, surface that to the operator to handle manually rather than burning Claude budget on multiple consecutive prune attempts.
- Don't escalate to the primary conversation channel. Infrastructure alerts route to the alerts channel.
- Don't use `gh issue create` directly - use the dedup helper.

## Cost

Should be ~150 tokens of output. Cheap haiku invocation.
