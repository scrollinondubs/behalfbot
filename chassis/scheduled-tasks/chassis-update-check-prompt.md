# Chassis update available - notify the owner

The weekly `chassis-update-check` heartbeat has detected that this install's chassis is behind the upstream `scrollinondubs/behalfbot` main branch. Your job is to post a single, well-formed consent notification to the owner's **alerts channel** so they can decide whether to apply the update.

This is a notify-only heartbeat. Do NOT apply the update yourself. Application is gated behind a separate Discord trigger word (`update chassis` / `update chassis --force`) handled by `skills/chassis-update.md`.

## What you have

The gather script (`gather-chassis-update-check.sh`) emitted JSON with these fields:

- `kind` - `version` or `drift`. **Read this first.** It decides which template you post, and it is stated explicitly rather than inferred precisely because a `drift` offer has `current == latest`.
- `current` - the customer's installed chassis VERSION (e.g. `0.3.1`)
- `latest` - the upstream main VERSION (e.g. `0.3.4`)
- `changelog_url` - link to the CHANGELOG.md on `scrollinondubs/behalfbot`
- `breaking` - boolean. `true` if what this update would deliver contains a `BREAKING CHANGES:` marker. The unreleased section counts, because a pull of main delivers it.
- `unreleased_digest` - short hash of upstream's `## Unreleased` section
- `offer_key` - what this offer is identified by. `<version>` for a version offer, `<version>+<digest>` for a drift offer. `skills/chassis-update.md` writes this to `dismissed.json` on `skip update`.

### The two kinds

`chassis/VERSION` only moves on an explicit release commit, but `main` is the distribution branch, so most of the time an install is behind without the version number saying so.

- **`kind: version`** - the install is on an older VERSION. `latest > current`.
- **`kind: drift`** - the versions match, and upstream `main` carries changes under `## Unreleased` that this install does not have. `current == latest`, and that is not a mistake. Do not report it as "no update available", and do not omit the notification because the two version numbers look the same.

The gather script has already filtered out:
- The case where this install is genuinely level with main
- The case where the owner dismissed this offer (or muted the whole version)
- The case where this same offer was already made inside the cooldown window

## What to do

### Step 1 - Resolve the alerts channel

Read `chassis.config.yaml` at `${CHASSIS_HOME}/chassis.config.yaml`. The customer's alerts channel comes from `discord_channels.alerts_label` (display name, e.g. `#alerts` or `#<installer>-ops`). The runtime channel ID is in the `.env` as `DISCORD_ALERTS_CHANNEL_ID`. Use `chassis/scripts/post-to-channel.sh` (or the equivalent) with the alerts channel ID.

If neither is configured, fall back to `DISCORD_PRIMARY_CHANNEL_ID`. Do NOT silently drop the notification.

### Step 2 - Compose the notification

**`kind: version`, non-breaking:**

```
Chassis update available: `<current> → <latest>`
Changelog: <changelog_url>

Reply `update chassis` to apply, or `skip update` to dismiss until the next version drops.
```

**`kind: version`, breaking (when `breaking: true`):**

```
:warning: Chassis update available with BREAKING CHANGES: `<current> → <latest>`
Manual review required before applying.

Changelog: <changelog_url>

Reply `update chassis --force` to apply after you've reviewed the changelog, or `skip update` to dismiss until the next version drops.
```

**`kind: drift`, non-breaking:**

```
Chassis update available: unreleased changes on `main`, still v`<current>`
The version number has not moved. `main` carries changes under `## Unreleased` that this install does not have.

Changelog: <changelog_url>

Reply `update chassis` to apply, or `skip update` to dismiss these changes.
```

**`kind: drift`, breaking (when `breaking: true`):**

```
:warning: Chassis update available with BREAKING CHANGES: unreleased changes on `main`, still v`<current>`
The version number has not moved. Manual review required before applying.

Changelog: <changelog_url>

Reply `update chassis --force` to apply after you've reviewed the changelog, or `skip update` to dismiss these changes.
```

Use the literal template above. Don't editorialize, don't add reassurances, don't elaborate on what's in the changelog. The owner reads the changelog themselves; your job is just to surface the offer.

For a drift offer, `skip update` dismisses **this set of changes**, not the version. The next merge under the same version number is offered again. Say "dismiss these changes", not "dismiss until the next version drops" - the second one would be a lie.

### Step 3 - Post once

Post the message to the resolved alerts channel via the standard post-to-channel script. Then exit.

The owner's response (`update chassis`, `update chassis --force`, `skip update`) is handled by `skills/chassis-update.md` - a separate Discord trigger. You don't need to wait for or process the reply.

## Important

- **Do not apply the update yourself.** The application path lives in `chassis/scripts/chassis-update.sh` and is gated behind explicit operator consent in Discord.
- **Do not modify state files.** `state/chassis-update/last-offered.json` and `state/chassis-update/dismissed.json` are owned by the gather script and the apply trigger respectively.
- **Do not re-notify** if the same offer was already made inside the cooldown window. The gather script's `already_offered` gate prevents this; if you reach this prompt, the offer is fresh.
- **Do not treat `current == latest` as nothing to report.** That is what a drift offer looks like. Check `kind`.
- **No nag, ever.** Single message, single offer. The dismiss flow is the owner's lever.

## Out of scope

- Reading the changelog and summarizing it. The owner clicks the link.
- Recommending whether to apply. You don't know the owner's risk appetite.
- Touching production state of any kind.
- Cross-installer telemetry (this heartbeat runs per-install; nothing aggregates).
