# Installer graduation — Phase 2 → Phase 3 checklist

Every chassis install moves through three phases:

| Phase | What it means | Who's driving |
|---|---|---|
| **Phase 1 — Setup** | Hydration, secrets bake, first heartbeat fire, Discord bridge stood up | Vendor (Sean) leads, installer assists |
| **Phase 2 — Soak** | Install runs in production, heartbeats fire daily, bridge holds, no fires that need vendor intervention | Installer uses day-to-day; vendor monitors heartbeat-state.json |
| **Phase 3 — Graduated** | Install is the installer's own; vendor reacts to incidents only when escalated | Installer owns; vendor supports chassis upstream |

This doc covers the formal **Phase 2 → Phase 3 transition**. Before this existed, the criteria were tribal knowledge between the vendor (Sean) and whoever was onboarding. Surfaced as a gap during Toby's graduation 2026-05-29 — he asked *"can I just crack on with Asimov?"* and the honest answer (*"functionally yes, formally no"*) needed a written checklist instead of an ad-hoc sign-off.

## When to run this checklist

The installer is **ready for Phase 3 review** when:

- [ ] The bridge has been operational for at least 7 days since first heartbeat fire.
- [ ] The vendor can SSH to the installer's Mac by hostname via Tailscale.
- [ ] The installer has acknowledged the post-graduation responsibilities (below).

Don't run the review earlier — the soak period exists to catch failure modes that take days to surface (claude OAuth token refresh, reboots, scheduled-task fires across the full week, mid-day tmux deaths).

## Graduation criteria

Check each item via the install's `~/<install-root>/scheduled-tasks/heartbeat-state.json`, `~/Library/LaunchAgents/`, and live Discord channel exchanges. Items with linked verification commands are vendor-side, run from the vendor's machine over SSH.

### Soak fires

- [ ] **≥3 clean dispatcher fires** logged in `heartbeat-state.json` with `last_result: success` across the install's heartbeats. Verify:
  ```bash
  ssh installer@<install-hostname> 'cat ~/<install-root>/scheduled-tasks/heartbeat-state.json | head -200'
  ```
  Most installs ship at minimum the `morning-briefing` heartbeat. ≥3 clean fires of that, with the most recent fire under 24h old + `last_decision` consistent with a healthy schedule decision, is the bar.

- [ ] **No `fire_count` resets** mid-soak. If `fire_count` rolled back to 0, the dispatcher restarted or the state file was clobbered — diagnose before graduating.

### Bridge

- [ ] **Live round-trip** in the install's Discord channel within the last 24h. Vendor pings the bot persona; bot replies via the `discord-post` tool path. Confirms WebSocket + tmux session + claude process + reply path all working.

- [ ] **Discord bridge auto-respawn LaunchAgent loaded.** Verify:
  ```bash
  ssh installer@<install-hostname> 'launchctl print gui/$(id -u)/<installer-label>.<session-name>-watchdog | head -10'
  ```
  State should be `running` or `not running` with `last exit code = 0` (watchdog runs in bursts; "not running" just means the current interval hasn't fired). See [`chassis/scripts/install-discord-bridge-launchagent.sh`](../chassis/scripts/install-discord-bridge-launchagent.sh) for the install path.

- [ ] **Reboot survival proven.** Either (a) the install's Mac has rebooted at least once during soak AND the bridge came back automatically (verify via `${SESSION_NAME}-watchdog.log` — look for the `Session missing — restarting` line after a reboot timestamp), or (b) the vendor has done a controlled `sudo reboot` during the soak window and confirmed the same.

### Billing

- [ ] **Claude Max subscription confirmed**, NOT API/PAYG. Verify by SSHing in and capturing the bridge tmux pane:
  ```bash
  ssh installer@<install-hostname> 'tmux capture-pane -t <session-name> -p | head -10'
  ```
  First line of the claude banner should show `Claude Max` (or equivalent paid plan, e.g. `Claude Team`). API/PAYG installs are NOT ready for graduation — the cost model isn't right.

### Access

- [ ] **Tailscale node reachable by hostname.** Verify:
  ```bash
  tailscale status | grep <install-hostname>
  ssh installer@<install-hostname> 'hostname'
  ```
  Status should show the node as `idle` or `active`, NOT `offline`. SSH by hostname (not just IP) should return cleanly. This is the vendor's ongoing access channel for reactive support.

- [ ] **GitHub repo ownership transferred or push-access granted.** If the install repo is under the vendor's GitHub during setup, transfer to the installer (or grant them admin/push access) before graduation. The installer owns the repo post-graduation.

### Secrets

- [ ] **Vendor-provisioned secrets handed off.** Anything minted on the vendor side during setup — bot tokens the vendor created, shared API keys, OAuth client secrets — should be rotated into the installer's vault so they don't depend on vendor's vault for day-to-day operation.

- [ ] **Vault unlocked headlessly** (e.g. via `bw-unlock.sh` keychain bridge) so heartbeats can hydrate secrets without the installer being interactively present.

## Vendor → installer handoff (post-checklist)

Once every checkbox above is checked:

- [ ] Vendor posts a sign-off in the install's Discord channel: *"Phase 3 graduated as of [date]. You own this install."*
- [ ] Vendor marks the install **graduated** in vendor-side tracking (Linear / Notion / wherever).
- [ ] Optional: vendor and installer agree on a dissolution timeline for the setup-era support channel (e.g. *"channel dissolves end of week N"*) — keeps day-to-day clutter out of the vendor's queue.

## Installer responsibilities post-graduation

After graduation, the installer owns:

- **Day-to-day bridge operations.** Bot is yours.
- **Local monitoring habits.** Periodically `tail` the watchdog + restart logs; check `heartbeat-state.json` weekly.
- **`brew upgrade claude` cadence.** Keep Claude Code current. Releases ship roughly every 2 weeks; staying within one minor version is plenty.
- **Tailscale / SSH access maintenance.** If the vendor's SSH key gets rotated or your Tailscale auth lapses, renew the access. The vendor can't help if they can't reach in.
- **Chassis subtree pulls.** When the vendor publishes a chassis update with a fix that affects your install, pull the subtree and rebuild. See [`docs/INSTALL.md`](INSTALL.md) for the subtree-pull command.

## Vendor responsibilities post-graduation

After graduation, the vendor owns:

- **Reactive support** for issues the installer can't self-diagnose. Installer pings via the agreed channel; vendor responds.
- **Pushing chassis updates** that fix systemic issues affecting the install. Includes giving the installer a heads-up when breaking changes ship.
- **Maintaining the SSH foothold** so they can triage when called in.

## Failure modes that surface mid-soak

These are the situations that justify the soak period existing. None of them necessarily block graduation, but each needs investigation when it appears:

- **Bridge silently dropping.** Either an unstable network, a known-bad chassis interval, or an unhandled claude crash. Check the watchdog log for restart frequency.
- **Heartbeat dispatcher failing fires.** `last_result: failure` on any heartbeat. Could be flaky deps, missing secrets, or a script bug. Vendor and installer triage together.
- **OAuth refresh issues.** `Not logged in` / `Please run /login` in the bridge pane. Usually a Keychain access regression — confirm the LaunchAgent is still in `gui/$UID` domain.
- **Discord WebSocket drops.** Bot shows offline but no auth error in the pane. Watchdog should catch this on the next 30-min tick, but if it happens repeatedly, check the install's Discord token health.

## Examples

- **`scrollinondubs/new-jaxity`** (Sean's install) — bootstrap install, no formal graduation since vendor and installer are the same person.
- **installer-3's install (2026-05-29)** — first formal graduation. Soak: 9 morning-briefing fires across 8 days; bridge proven via round-trip; Max billing confirmed; auto-respawn LaunchAgent shipped post-graduation (originally manual respawn pattern). Channel dissolution: end of 2026-06-06.
- **installer-1 install** — pending. Will follow this checklist when soak window completes.

## Updating this doc

Phase 3 patterns will evolve as more installs graduate. When a graduation surfaces a new failure mode or a new checklist item, edit this doc in the same PR as the operational fix — don't let the checklist drift behind reality.
