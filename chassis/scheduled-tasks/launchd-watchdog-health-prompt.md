# Launchd watchdog health alert

A critical LaunchAgent (one of the install's persistent watchdogs / daemons) is no longer loaded into `gui/$UID`. This blocks any work that agent does silently until manually re-bootstrapped. Without this heartbeat the gap is invisible — the absence of a heartbeat-issuing agent is invisible to its own heartbeat.

You will receive the missing agent labels in the gather output (JSON `{"count": N, "missing": [...]}`).

For each missing agent:

1. **Verify the plist exists** at `~/Library/LaunchAgents/<label>.plist`. If not, the agent was intentionally removed — drop it from the `CHASSIS_WATCHDOG_CRITICAL_AGENTS` list (see `chassis.config.yaml > modules.watchdog_health.critical_agents` or the equivalent env-var declaration) and skip.

2. **Try a re-bootstrap:**

   ```
   launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/<label>.plist
   launchctl print "gui/$(id -u)/<label>" | head -10
   ```

   If `state = running` or `xpcproxy` and `last exit code` is reasonable, the fix held.

3. **If it crash-loops** (last exit code nonzero, throttled state), check the stderr log path declared in the plist. Common causes: stale paths from a repo move, missing env vars, the executable not found.

4. **Post a one-line summary** to the install's ops channel: which agents went missing, what you did, whether they're back. Include the per-agent stderr tail if any are still down. Do NOT spam the channel for healthy ticks — the threshold condition only fires this prompt when something is wrong.

5. **If the agent silently unloaded without leaving an exit code or log entry**, that's a system-level signal (the user rebooted, the user-session got cycled). Log the event but no fix is needed beyond the bootstrap above.

Do not mark the work done until `launchctl print gui/$(id -u)/<label>` returns successfully for every previously-missing agent.

## Heartbeat registration

To enable this heartbeat in an install, add to `${CUSTOMER_HOME}/HEARTBEATS.md`:

```yaml
## launchd-watchdog-health

```yaml
schedule: every 30m
gather: ${CHASSIS_HOME}/chassis/scripts/gather-launchd-watchdog-health.sh
condition: threshold count > 0
prompt: ${CHASSIS_HOME}/chassis/scheduled-tasks/launchd-watchdog-health-prompt.md
model: sonnet
budget: 1
criticality: critical
```
```

Populate the critical-agent list via `CHASSIS_WATCHDOG_CRITICAL_AGENTS` in the install's environment (comma- or newline-separated labels).
