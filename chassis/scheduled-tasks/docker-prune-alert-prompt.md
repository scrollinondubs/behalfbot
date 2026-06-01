# docker-prune Alert

The weekly docker-prune heartbeat failed. The gather emitted `count > 0` with one of:

| `status` | What broke | Fix |
|---|---|---|
| `docker_unreachable` | `docker info` returned non-zero inside the chassis container. The host's `/var/run/docker.sock` bind-mount is missing, the daemon is down, or container UID lacks socket permissions. | Verify the compose `volumes:` block still has `${DOCKER_SOCKET:-/var/run/docker.sock}:/var/run/docker.sock`. Check the host's Docker Desktop / Engine is running. On Linux installs, ensure the chassis user is in the `docker` group. |
| `prune_failed` | `docker builder prune` OR `docker image prune` returned non-zero. The `detail` line carries the tail of stderr. | Read the detail. Common causes: daemon under load, disk full to the point prune itself fails, a container holding an exclusive lock. |

## Your job

1. Classify the failure from `status` + `detail`.
2. Post one concise alert to `#<devops>` (channel `1497870976237699173`) via the discord MCP `reply` tool: date, status tag, best-guess cause, fix suggestion.
3. If the same status fires for 2+ consecutive weeks, file-or-comment on `scrollinondubs/new-jaxity` using the dedup helper.

## What NOT to do

- Don't retry the prune from this prompt. The next weekly tick handles retry. If the disk is critical, surface that to Sean to handle manually rather than burning Claude budget on multiple consecutive prune attempts.
- Don't escalate to `#<primary>`. Infrastructure alerts route to `#<devops>` per the 2026-05-25 routing update.
- Don't use `gh issue create` directly — use the dedup helper.

## Cost

Should be ~150 tokens of output. Cheap haiku invocation.
