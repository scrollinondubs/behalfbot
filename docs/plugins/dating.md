# Plugin: Dating

Dating-app automation as a sandboxed subagent. Photo-verification consensus engine, behaviour-based catfish screening (reply-gated), an optional installer-configured regional gate (ships disabled with an empty country list), concierge framing, Angel Protocol prereq for in-person meets. The plugin is **dormant by default** - opt-in only.

The agent runs in its own subagent context (`claude -p --cwd ${CHASSIS_HOME}/plugins/dating/`), uses its own CLAUDE.md, and is bound to a single Discord channel for output. The split is the whole point: a confused dating subagent that posts to the wrong channel or touches the wrong file is mechanically prevented from doing so.

## When to enable

- Installer wants the agent to handle dating-app screening + early-conversation work, with the installer reviewing escalations and ratifying decisions.
- The Angel Protocol plugin is enabled (or the installer is fine with in-person meets being deflected to video until it is).

## Activation

```yaml
# chassis.config.yaml
modules:
  dating:
    enabled: true
    social_channel_id: "<your social-channel ID>"
    social_webhook_env_var: CHASSIS_SOCIAL_WEBHOOK_URL  # name of env var holding the webhook
    installer_facts_path: ${CHASSIS_HOME}/plugins/dating/installer-facts.md
    cleared_matches_path: ${CHASSIS_HOME}/plugins/dating/cleared-matches.json
    pending_instructions_path: ${CHASSIS_HOME}/plugins/dating/pending-instructions.md
    scheduling_blocks_path: ${CHASSIS_HOME}/plugins/dating/scheduling-blocks.md
    verifications_output_root: ${CHASSIS_HOME}/data/dating/verifications
    verify_venv_path: ${CHASSIS_HOME}/.venv-dating
    platforms:
      hinge:
        enabled: true
        transport: android_emulator
      tinder:
        enabled: false
      bumble:
        enabled: false
    emulator:
      avd_name: Dating_Pixel
      spoofed_lat: <installer's local-city lat>     # NEVER home coords
      spoofed_lon: <installer's local-city lon>     # NEVER home coords
    safety_floor:
      photo_verification:
        mode: reply_gated
      regional_default_reject:
        enabled: false          # optional gate - ships disabled; populate country_codes
        country_codes: []       # for your own threat model; the chassis names no countries
      five_exchange_rule: true
      angel_protocol_required_before_in_person: true
      anti_doxx: true
      concierge_framing: true
      never_dinner_first: true
      never_private_first: true
    scoring:
      face_scorer_enabled: false
```

## Activation prereqs

Three things must be in place before enabling:

1. **A dedicated Discord channel** for dating output (the "social channel"). All session reports, photo-verification verdicts, and escalations land there. NEVER co-mingle with the primary briefing channel — the privacy and ops contexts are different. Webhook URL goes in `.env` under the name configured in `social_webhook_env_var`.

2. **`installer-facts.md` filled in.** Copy the template:
   ```bash
   cp plugins/dating/installer-facts.template.md plugins/dating/installer-facts.md
   ```
   Edit. Add to `.gitignore` (template suggests `plugins/dating/installer-facts.md` — don't commit your personal facts).

3. **`cleared-matches.json` seeded** (empty by default — preauth clearances accumulate over time as the installer issues them):
   ```bash
   cp plugins/dating/cleared-matches.template.json plugins/dating/cleared-matches.json
   ```

4. **Pending instructions + scheduling blocks** seeded as empty:
   ```bash
   cp plugins/dating/pending-instructions.template.md plugins/dating/pending-instructions.md
   cp plugins/dating/scheduling-blocks.template.md plugins/dating/scheduling-blocks.md
   ```

5. **Photo-verification venv created.** Playwright is the only Python dependency:
   ```bash
   python3 -m venv ${CHASSIS_HOME}/.venv-dating
   ${CHASSIS_HOME}/.venv-dating/bin/pip install playwright
   ${CHASSIS_HOME}/.venv-dating/bin/playwright install chromium
   ```

6. **Android emulator + ADB** for any platform whose transport is `android_emulator`. Create the AVD at install time, name it per the manifest, and configure GPS spoofing to your local-city coordinates (NEVER home coords).

7. **Angel Protocol plugin enabled** (or accept that all in-person meet proposals will deflect to video until you do — the safety floor is hard-wired). Currently scaffold-only; see `docs/plugins/angel-protocol.md`.

## Runtime: host-resident, not in-container

**Dating runs on the host, NOT inside the chassis container.** This is a deliberate carve-out from the chassis "everything in container" paradigm because the dating subagent's runtime dependencies are all host-bound:

- **Android emulator** — a host GUI process (Android Studio / qemu), screen-attached, with adb-server listening on host:5037. The chassis container has no display, no adb-server, and can only reach the host's adb via a `tcp:host.docker.internal:5037` bridge — which gets the emulator state but not the rest of the stack.
- **Playwright + Chromium** — Tinder runs through the Playwright web flow with a persistent browser profile. Bundling Chromium-on-arm64 into the chassis image is ~600MB of additional bloat for a single plugin.
- **CLIP scorer + taste-refs / negative-refs** — the calibration engine (`scripts/score-calibrate.py`, `scripts/dating-reconcile.py`) reads / writes large ML model caches + reference image directories that live on the host filesystem.
- **The installer's hand-sorted picks feedback loop** (`${CHASSIS_HOME}/rhl-picks/` by default) - the installer sorts screenshots into like/super-like/pass folders and the subagent reads them to update calibration. Mounting a folder from the installer's host home directory into the container would be a security-model regression.
- **dating-context subagent skill bundle** - the cwd loads its own CLAUDE.md + skill files + installer-facts.md from the host's dating-context directory tree.

The chassis-runtime "everything in a container heartbeat" path doesn't fit. Dating is the canonical "fat-client plugin" tier: schedule via host launchd, fire `claude -p` against the dating subagent's cwd, let it use Keychain auth + full host filesystem. The dispatcher's other heartbeats stay in-container.

Reference install (<v1-reference-install>, post-2026-05-25 cutover):
- `scheduled-tasks/com.<assistant>.dating-swipe-{1,2,3}.plist` — launchd plists fire 10:00 / 14:00 / 18:00 local.
- `scripts/dating-swipe-host.sh` — wrapper script: 0-30 min random jitter (preserves the chassis dispatcher's anti-detection profile), `gather-dating-swipe.sh` gate, then `claude -p --max-budget-usd 6` with the dating-swipe-prompt against the dating-context cwd.
- HEARTBEATS.md keeps the dating-swipe-N blocks HTML-commented (so the reconciler doesn't false-positive on staleness) with an inline pointer to the host launchd jobs.

Future state: if/when the chassis runtime grows native "host-resident plugin" support (RPC bridge, container-side stubs that delegate to a host helper daemon), the dating plugin can move back under the container dispatcher. Until then, this is the supported pattern for installs that want dating.

### Trade-off: dating swipes run as user LaunchAgents (not LaunchDaemons)

chassis#14 promoted most host plists to **LaunchDaemons** so they would survive an unattended Mac reboot. That was reverted on 2026-07-11: a LaunchDaemon runs in launchd's Background session and cannot reach the login keychain, which killed Vaultwarden on every macOS install for five weeks. Chassis host plists are gui-domain **LaunchAgents** again. Dating-swipe plists were always LaunchAgents, for their own reasons:

- The Android emulator window is an Aqua-session resource — qemu/AndroidStudio needs `WindowServer` to render the AVD. Without a logged-in GUI session, the emulator binary won't start.
- Playwright Chromium on macOS likewise needs an Aqua session even in `headless: true` mode (Chromium's launcher path on darwin assumes a `WindowServer` connection during init).

Consequence: **if your install Mac reboots while you're away and you don't have auto-login enabled, the day's missed swipe slots are lost — they don't replay.** Each slot fires once at its scheduled time; if nothing was loaded to fire it, the slot just doesn't happen.

Your two mitigations:

1. **Keep the Mac logged in.** Don't reboot when you're not present, and don't let the OS auto-reboot for updates (System Settings → General → Software Update → Automatic Updates → off for "Restart automatically").
2. **Enable auto-login.** System Settings → Users & Groups → Automatic login → pick your user. The Mac boots straight into Aqua without password entry, so the LaunchAgent loads on reboot. The security trade-off is that anyone with physical access skips the login prompt. Your call.

See `docs/launchd-domains.md` for the broader LaunchDaemon vs LaunchAgent decision rule and the chassis#14 incident context.

## Architecture

Three layers of separation:

1. **Subagent split.** The dating logic runs in `claude -p --cwd plugins/dating/`. That cwd loads `plugins/dating/CLAUDE.md` instead of the chassis root one, which restricts file-read scope, channel scope, and tool scope. A dating subagent that ignores its CLAUDE.md is a separate process from the orchestrator — it can't accidentally post to the briefing channel from the orchestrator's context.

2. **Channel binding.** The dating subagent is hard-coded to one Discord channel (the social channel). All escalations, photo-verification verdicts, and session reports go there. Every other channel is off-limits — the CLAUDE.md states this explicitly and refusal is the model's job.

3. **Safety floor.** Four non-negotiable rules wired to `chassis.config.yaml > modules.dating.safety_floor`:
   - **Behaviour-based catfish screening** (reverse-image consensus, single-country-footprint detection, verifiable local presence, refusal-of-verification signals), plus an optional regional default-reject gate the installer may enable and configure for their own threat model. The gate ships disabled with an empty country list, and its override path is clearly defined so legitimate expats can still pass.
   - **Reply-gated photo verification** — the four-engine consensus (TinEye + Google Lens + PimEyes + Yandex) runs only after the match replies to the opener, minimizing wasted cycles on bot profiles.
   - **Angel Protocol Phase 0 required before any in-person meet** — until the angel-protocol plugin is enabled and Phase 0 is live, all in-person counter-proposals deflect to video.
   - **Preauth clearance pierces the regional video-screen, NOT the safety floor.** The installer can vouch for a specific match out-of-band (WhatsApp, IG, real life), but photo verification, override-gate evidence, and Angel Protocol monitoring all still apply.

## Photo verification consensus engine

`scripts/verify-match.py` runs four reverse-image-search engines in parallel and aggregates their results into a traffic-light verdict:

| Engine | Strength | Auto-reject signal |
|---|---|---|
| **TinEye** | Exact-pixel byte-match | Hit on adult-aggregator domain → RED |
| **Google Lens** | Visual match + celebrity ID + AI labels | Identifies named celebrity OR high-confidence hit on adult-aggregator domain → RED |
| **PimEyes** | Face-geometry recognition | Footprint concentrated exclusively on a single country's domains and aggregators with zero corroborating presence anywhere else → RED |
| **Yandex** | Visual similarity (NOT byte-match) | Demoted to YELLOW max — informational only, never sufficient to auto-reject |

Yandex is similarity-only — there is always someone who looks similar on the web. The catfish bar is exact byte-match (TinEye) or high-confidence visual (Lens) on a high-suspicion domain.

Output (under `${CHASSIS_HOME}/data/dating/verifications/<slug>-<platform>-<date>/`):
- `search-results.png` / `sites.png` — full-page screenshots
- `report.md` — human-readable summary with detected names + sites + verdict
- `raw.json` — structured data for downstream automation

Wrapper script `scripts/verify-match.sh` activates the dedicated venv and shells out to the Python script. Exit codes: `0 = green / no_signal`, `1 = yellow / unknown`, `2 = red / catfish`.

## Recovery hook

`scheduled-tasks/recovery-hooks.d/dating-emulator-recovery.sh` registers `chassis_recovery_dating_emulator()` — a self-healing watchdog the chassis heartbeat dispatcher calls every 15 minutes. It:

- Respects the `EMULATOR_PAUSE` flag (a file in this plugin directory)
- Skips silently if the emulator is already ready
- Enforces a cooldown between restart attempts (default 30 min)
- Posts to the configured ops webhook after 3 consecutive failures (default; configurable)

The hook ports the V1 reference's `scripts/emulator-watchdog.sh` into a chassis-managed sourceable function with no installer-specific paths or AVD names baked in.

The installer must provide their own emulator-start script at `${DATING_EMULATOR_START_SCRIPT:-${CHASSIS_HOME}/plugins/dating/scripts/emulator-start.sh}` — the chassis doesn't ship a start script because the AVD setup is install-time and platform-specific (macOS vs Linux, Android Studio vs command-line `avdmanager`, etc.).

## Files in this plugin

- `openclaw.plugin.json` — manifest
- `CLAUDE.md` — subagent contract (loaded when the dating subagent starts)
- `skills/dating.md` — the canonical dating playbook
- `scripts/verify-match.{py,sh}` — photo-verification consensus engine
- `scheduled-tasks/recovery-hooks.d/dating-emulator-recovery.sh` — emulator self-heal hook
- `cleared-matches.template.json` — preauth-clearance store template
- `pending-instructions.template.md` — installer's real-time-overrides inbox template
- `scheduling-blocks.template.md` — date/time blackout windows template
- `installer-facts.template.md` — installer's canonical-facts file template

## What does NOT ship

The plugin extracts the architectural pattern only — installer-personal data from the V1 reference (<v1-reference-install>) is deliberately stripped:

- The V1 installer's specific Hinge profile, voice prompt, age, height, neighborhood, character preferences, vision board photos
- Specific city venues, GPS coords, neighborhood rotation lists
- Hard-coded Discord channel IDs (replaced with `${SOCIAL_CHANNEL_ID}` placeholder)
- Memory entries from the V1 install (the per-match `lead:` entities and their observation history)
- Specific match incidents from the V1 install (confirmed-catfish cases where a photo set reverse-imaged to an unrelated person, matches marked GO DARK, and the rest of the per-match history)
- The local CLIP face scorer + taste references (installer must build their own at install time — face-aesthetic preference is fundamentally per-installer)

Each installer authors their own `installer-facts.md`, builds their own taste references if they enable the face scorer, and accumulates their own pending-instructions / cleared-matches state over time.

## Cross-references

- `plugins/dating/CLAUDE.md` — subagent contract
- `plugins/dating/skills/dating.md` — canonical playbook
- `plugins/angel-protocol/` — hard prereq for in-person meets
- `chassis.config.yaml > modules.dating` — configuration schema
- V1 reference (extracted from): <v1-reference-install> — `dating-context/`, `skills/dating.md`, `scripts/verify-match.{py,sh}`, `scripts/emulator-watchdog.sh`
- Source issue: <v1-reference-install>#496
