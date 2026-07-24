# Container liveness alert

The scheduled `container-liveness` check found a core chassis container DOWN, restart-looping, or unhealthy. Unlike `service-health` (which probes HTTP endpoints), this check reads container State directly via `docker inspect`, so it catches the case an HTTP probe never could: a container that is not serving because it is not running at all. #118 introduced a real boot-failure mode (an exit-5 assertion aborts the entrypoint), so an unmonitored crash-loop is a live risk.

You will receive the gather output as JSON: `{"count": N, "issues": [...], "checked": M, "status": "..."}`.

## Triage by issue tag (per container `<c>`)

- **`<c>_down_<status>`** - `State.Status` is `exited` / `dead` / `created` / `paused`, not `running`. The container is down. For `behalfbot` itself this is the most severe case. Check `docker logs <c> --tail 100` for the exit cause - an `exit 5` from `resolve-chassis-root.sh` points at chassis-root drift (cross-reference the `chassis-root-health` heartbeat and its prompt). Restart via the install's `compose.sh` wrapper (or bare `docker compose up -d` only when no override is in play).
- **`<c>_restarting`** - `State.Restarting == true`: caught mid restart-loop. Same log triage; a tight loop usually means the entrypoint aborts every boot (bad env, torn chassis tree, exit 5).
- **`<c>_restart_loop`** - `RestartCount` climbed since the last tick, i.e. the container restarted within the interval. Even if it is `running` right now, it is not stable. Find the crash cause in the logs.
- **`<c>_absent`** - no such container. It was never created or was removed. Verify the compose stack is up: `docker compose ps`.
- **`<c>_unhealthy`** - the container's own healthcheck reports unhealthy (e.g. postgres failing `pg_isready`). Check the dependent services.

## What to do

1. `docker logs <c> --tail 100` and `docker inspect <c>` to establish the cause.
2. Post a one-line summary to the install's ops channel: which container, which tag, the exit cause from the logs.
3. Restarting production containers is operator-visible - surface the fix for Sean's go-ahead unless it is an obvious transient that has already self-recovered. Diagnosis (logs, inspect) needs no approval.
4. If `behalfbot` itself was the down container, note how long it was down (from `docker inspect .State.StartedAt` / `FinishedAt`) - a dead dispatcher means every heartbeat was silent for that window.

## STRUCTURAL coverage note - the fully-dead-behalfbot blind spot

This heartbeat's gather runs INSIDE the `behalfbot` container. When `behalfbot` is fully dead (never completes a dispatcher tick), the dispatcher is not running, so this gather never fires for it. In-container, this check reliably catches: sibling containers down/unhealthy (postgres, vaultwarden, a bridge), and `behalfbot` restart-looping (sampled when it briefly comes up between crashes).

To catch a `behalfbot` that is fully down within one interval, run the SAME script from OUTSIDE the container - it is location-agnostic (pure docker CLI + a state file). Wire a host-side watcher against the host docker daemon:

- macOS LaunchAgent / Linux systemd timer / cron running, every few minutes:
  `CUSTOMER_HOME=<host-customer-dir> CHASSIS_LIVENESS_CONTAINERS=behalfbot bash <chassis>/scripts/gather-container-liveness.sh`
  and paging (Discord webhook / `notify`) when `count > 0`.
- This is the belt-and-suspenders layer: in-container catches siblings + loops, host-side catches a dead `behalfbot`. An install that already runs a Discord-bridge watchdog LaunchAgent can fold this check into it.

## Heartbeat registration

To enable the in-container half in an install, add to `${CUSTOMER_HOME}/HEARTBEATS.md`:

```yaml
## container-liveness

```yaml
schedule: every 15m
gather: ${CHASSIS_HOME}/chassis/scripts/gather-container-liveness.sh
condition: threshold count > 0
prompt: ${CHASSIS_HOME}/chassis/scheduled-tasks/container-liveness-prompt.md
model: sonnet
budget: 1
criticality: critical
```
```

Requires the docker socket bind-mount (same one `gather-docker-prune.sh` uses). Set `CHASSIS_LIVENESS_CONTAINERS` in the install's environment to the full stack (e.g. `behalfbot,behalfbot-postgres,behalfbot-discord-bridge`). Docker-unreachable is a deliberate no-op so installs without the socket never false-alarm.
