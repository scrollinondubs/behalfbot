# Chassis-root resolution health alert

The scheduled `chassis-root-health` check found that the boot-time chassis-root resolution (#118, `resolve-chassis-root.sh`) is in a bad state on this install. Left unfixed, the runtime is executing the WRONG chassis tree - either the stale image-baked copy while the operator's live tree sits unused, or a tree the resolver refused to trust. Every consumer of `CHASSIS_ROOT` (the dispatcher, gather scripts, `bootstrap-mcp-config.sh` and the `.mcp.json.template` it renders, `first-boot-announce.sh`) is affected. This is the exact "operator updated, runtime did not" silent-drift class #118 exists to kill.

You will receive the gather output as JSON: `{"count": N, "issues": [...], "mode": "...", "resolved_root": "...", "baked_version": "...", "live_version": "...", "error": "..."}`.

## Triage by issue tag

### `chassis_root_stale_baked`

The runtime resolved the BAKED tree (`mode: baked`) but a usable LIVE tree exists at `$CUSTOMER_HOME/chassis/chassis` right now. The resolver picked baked at boot (the mount was likely absent then); the live tree became available afterwards without a container restart. The install is silently on stale chassis.

1. Confirm the live tree is genuinely usable: `VERSION` readable, `scripts/` present, `scheduled-tasks/heartbeat-dispatcher.sh` present. Compare `live_version` vs `baked_version` from the gather output.
2. The fix is a container restart so the entrypoint re-runs `resolve-chassis-root.sh` and picks the live tree: `docker compose pull && docker compose up -d` (or the install's `compose.sh` wrapper if an override is in play - never a bare `up -d` when an override exists, per the #100 fix).
3. After the restart, re-read `$CUSTOMER_HOME/chassis-root.state.json` and confirm `mode` flipped to `live` and `error` is null. Do not mark the work done until it has.
4. If a restart is disruptive (mid-task), post the finding to the ops channel and ask Sean before bouncing the container.

### `chassis_root_assertion_failed`

The resolver hit an exit-5 assertion. The `error` field in the gather output tells you which:

- **Torn live tree** - a live tree exists but is missing `VERSION`, `scripts/`, or the dispatcher (mid-pull, partial mount). The runtime fell back to baked and is shouting. Fix: complete/repair the live tree (finish the `git pull`, re-establish the mount), then restart.
- **MAJOR version skew** - the live tree's MAJOR differs from baked. MAJOR is reserved for image-contract changes; running cross-MAJOR code on this image base is unsafe. Fix: refresh the image (`compose pull` + `up -d`) to a base that matches the live tree's MAJOR - do NOT force the live tree onto a mismatched base.
- **Symlink materialisation failure** - the live tree resolved but `$CUSTOMER_HOME/state/chassis-root` could not be written, so `docker exec` sessions and the host healthcheck cannot see the truth. Fix: check permissions on `$CUSTOMER_HOME/state/`, then restart.

### `chassis_root_state_unparseable`

`$CUSTOMER_HOME/chassis-root.state.json` is present but not valid JSON (truncated write, hand-edit). Re-run the resolver read-only or restart the container to regenerate it, then confirm it parses.

## What to do

1. Read the `error` field and the version fields; identify which failure mode above applies.
2. Post a one-line summary to the install's ops channel: which tag fired, `mode`, `live_version` vs `baked_version`, and the fix you propose.
3. Container restarts on the production stack are operator-visible - surface the fix for Sean's go-ahead rather than bouncing the container autonomously. Non-disruptive diagnosis (reading the state file, `docker inspect`) needs no approval.

## Important

- The check runs on a recurring schedule. The same tag two intervals running means the first fix did not stick - re-read the state file and escalate rather than repeating the same restart.
- Do NOT `git checkout` / mutate the live chassis tree from inside this flow. Chassis-side fixes go upstream via PR; this alert is about getting the runtime onto the CORRECT existing tree, not editing it.

## Heartbeat registration

To enable this heartbeat in an install, add to `${CUSTOMER_HOME}/HEARTBEATS.md`:

```yaml
## chassis-root-health

```yaml
schedule: every 30m
gather: ${CHASSIS_HOME}/chassis/scripts/gather-chassis-root-health.sh
condition: threshold count > 0
prompt: ${CHASSIS_HOME}/chassis/scheduled-tasks/chassis-root-health-prompt.md
model: sonnet
budget: 1
criticality: critical
```
```

Near-free per tick (a file read + a live-tree usability probe, no docker, no network). Silent on a healthy install and on any pre-#118 install (no state file = nothing resolved yet = nothing to drift).
