---
name: chassis-update
description: Apply or dismiss a pending chassis version bump in response to the operator's Discord trigger words (`update chassis`, `update chassis --force`, `skip update`). Use whenever the operator replies to a chassis-update-check notification in the configured alerts channel.
---

# Skill: chassis-update — operator-consent flow for the chassis auto-updater

This skill handles the **apply / dismiss** side of the chassis auto-updater (issue scrollinondubs/behalfbot#33). The **detect + notify** side lives in `gather-chassis-update-check.sh` + `scheduled-tasks/chassis-update-check-prompt.md`.

The operator-consent loop:

1. Weekly heartbeat detects a chassis bump → Claude posts a consent notification to the alerts channel.
2. Operator replies one of: `update chassis`, `update chassis --force`, `skip update`.
3. This skill catches that reply, validates eligibility, runs the apply script (or marks the version dismissed), and reports the outcome to the same alerts channel.

This skill NEVER applies an update without an explicit trigger word from the operator. It NEVER auto-decides between `update chassis` and `--force` on the operator's behalf — those are distinct semantic choices and the operator must type the right one.

## When to invoke this skill

Activate when a Discord message in the configured alerts channel matches one of these patterns (case-insensitive, leading/trailing whitespace tolerated):

| Trigger pattern | Action |
|---|---|
| `update chassis` | Run `chassis-update.sh` (non-breaking) |
| `update chassis --force` | Run `chassis-update.sh --force` (BREAKING-allowed) |
| `update chassis --dry-run` | Run `chassis-update.sh --dry-run` (preview, no changes) |
| `update chassis --rollback` | Run `chassis-update.sh --rollback` (restore most recent snapshot) |
| `skip update` | Append the latest offer's `offer_key` to `state/chassis-update/dismissed.json` |

Sender allowlist: only the principal (`INSTALLER_DISCORD_USER_ID` env var). Other users in the channel may type the words — ignore them.

## What you have

- `state/chassis-update/last-offered.json` - the most recent offer surfaced to the operator. This is what any `update chassis` / `skip update` reply refers to. Fields:
  - `version` - the upstream VERSION at the time of the offer.
  - `kind` - `version` or `drift`. Absent on state files written before behalfbot#147; treat absent as `version`.
  - `offer_key` - the identity of this offer: `<version>` for a version offer, `<version>+<digest>` for a drift offer. Also absent on pre-#147 files, where `version` served as the key.
  - `unreleased_digest` - short hash of upstream's `## Unreleased` section.
  - `offered_at` - ISO timestamp.
- `chassis.config.yaml` — `discord_channels.alerts_label` (display) + `.env`'s `DISCORD_ALERTS_CHANNEL_ID` (runtime).
- `chassis/scripts/chassis-update.sh` — the apply script.

## The two kinds of offer

`chassis/VERSION` only moves on an explicit release commit, while `main` is the branch every install pulls from. So there are two ways to be behind:

- **`kind: version`** - upstream VERSION is newer. `update chassis` applies it, and the apply reports `vX → vY`.
- **`kind: drift`** - the versions match but `main` carries changes under `## Unreleased` this install does not have. `update chassis` applies it exactly the same way (the apply script detects drift itself, on the same signal), and reports `vX unreleased <digest> → <digest> (VERSION unchanged)`.

A drift apply reports the same version on both sides of the arrow. That is correct, not a failed update. The evidence it worked is the digest change and the `Update complete` line, and `last-applied.json` records `"kind": "drift"` with both digests.

## What to do

### Branch 1: `update chassis` or `update chassis --force`

1. **Verify there's a pending offer.** Read `state/chassis-update/last-offered.json`. If missing or empty, reply to the alerts channel with: "No pending chassis update. Wait for the next weekly check or run `gather-chassis-update-check.sh` manually." Exit.
2. **Run the apply script.**
   - Non-force: `bash ${CHASSIS_HOME}/chassis/scripts/chassis-update.sh`
   - Force: `bash ${CHASSIS_HOME}/chassis/scripts/chassis-update.sh --force`
3. **Capture stdout + exit code.**
4. **Report outcome:**
   - Success: post to alerts channel - "Chassis updated: `<from> → <to>`. Snapshot: `<path>`. Healthcheck green." Include any relevant migration script output. For a drift apply, quote the script's own closing line rather than composing a `vX → vX` message that reads as a no-op.
   - Failure: post to alerts channel — "Chassis update FAILED. Last 20 lines of output:\n\n```\n<tail>\n```\n\nSnapshot for rollback: `<path>`. Run `update chassis --rollback` if container is unhealthy." Tag the principal.
5. **Clear `last-offered.json`** on success (next weekly check will re-populate if more versions remain).

### Branch 2: `update chassis --dry-run`

1. Run `bash ${CHASSIS_HOME}/chassis/scripts/chassis-update.sh --dry-run`.
2. Post the full stdout to the alerts channel in a code block. No state changes.

### Branch 3: `update chassis --rollback`

1. Verify the operator wants to restore the most recent pre-update snapshot. The script picks the latest `chassis-pre-v*.tgz` in `backups/chassis-update/`.
2. Run `bash ${CHASSIS_HOME}/chassis/scripts/chassis-update.sh --rollback`.
3. Report restored snapshot + remind the operator to verify Discord bridge / dispatcher health manually.

### Branch 4: `skip update`

1. Read `state/chassis-update/last-offered.json` to get the offer being dismissed.
2. If nothing is recorded, reply: "Nothing pending to skip." Exit.
3. Append the offer's **`offer_key`** to `state/chassis-update/dismissed.json` (initialize to `[]` if missing). Fall back to `.version` when the file predates behalfbot#147 and has no `offer_key`:
   ```bash
   KEY=$(jq -r '.offer_key // .version' state/chassis-update/last-offered.json)
   jq --arg v "$KEY" '. + [$v] | unique' state/chassis-update/dismissed.json
   ```
   For a version offer the key IS the bare version, so this writes exactly what it always wrote. For a drift offer it writes `<version>+<digest>`, which mutes that specific set of changes and nothing more.
4. Reply to alerts channel:
   - version offer: "Chassis update v$VERSION dismissed. Will re-notify when a newer version drops."
   - drift offer: "Those unreleased changes are dismissed. Will re-notify when more land on main, or when a new version drops."

**Never write a bare version string to dismiss a drift offer.** A bare version is the legacy "mute this version entirely" entry: it would silence every future merge under that version number until a release is cut, which on a branch where VERSION moves only on release commits means silencing the install indefinitely. That is the exact failure behalfbot#147 exists to remove.

## Important

- **Never apply without explicit trigger.** This skill is reactive only.
- **Never override BREAKING gate.** If the apply script refuses without `--force`, surface the refusal verbatim — do NOT auto-retry with `--force`. The operator must type that themselves.
- **Run from the right working directory.** The apply script reads `$CHASSIS_HOME` from env; ensure it's set before invoking. In containerized installs, `$CHASSIS_HOME` is set by the dispatcher.
- **Post output to alerts channel, not primary.** This is ops chatter, not principal-facing conversation. Use `DISCORD_ALERTS_CHANNEL_ID`.
- **Don't echo the changelog.** Operator can click the link in the original notification.

## Out of scope

- Surfacing a new offer (that's the heartbeat's job).
- Validating individual changelog entries.
- Scheduling future updates ("apply next week"). No scheduling — operator re-triggers manually if they want to defer-then-apply.
- Cross-version skip-ahead (e.g. apply v0.5.0 directly skipping v0.3.x and v0.4.x). The apply script always pulls upstream main; if multiple versions stack between current and latest, all changelog windows + all migration scripts run sequentially as a single operation.
