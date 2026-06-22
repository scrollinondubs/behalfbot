# Chassis update available - notify the owner

The weekly `chassis-update-check` heartbeat has detected that this install's chassis is behind the upstream `scrollinondubs/behalfbot` main branch. Your job is to post a single, well-formed consent notification to the owner's **alerts channel** so they can decide whether to apply the update.

This is a notify-only heartbeat. Do NOT apply the update yourself. Application is gated behind a separate Discord trigger word (`update chassis` / `update chassis --force`) handled by `skills/chassis-update.md`.

## What you have

The gather script (`gather-chassis-update-check.sh`) emitted JSON with these fields:

- `current` - the customer's installed chassis VERSION (e.g. `0.3.1`)
- `latest` - the upstream main VERSION (e.g. `0.3.4`)
- `changelog_url` - link to the CHANGELOG.md on `scrollinondubs/behalfbot`
- `breaking` - boolean. `true` if the changelog between `current` and `latest` contains a `BREAKING CHANGES:` marker

The gather script has already filtered out:
- The case where this install is up to date
- The case where the owner dismissed this exact version
- The case where this version was already offered in the current week (no double-fire)

## What to do

### Step 1 - Resolve the alerts channel

Read `chassis.config.yaml` at `${CHASSIS_HOME}/chassis.config.yaml`. The customer's alerts channel comes from `discord_channels.alerts_label` (display name, e.g. `#alerts` or `#jax-ops`). The runtime channel ID is in the `.env` as `DISCORD_ALERTS_CHANNEL_ID`. Use `chassis/scripts/post-to-channel.sh` (or the equivalent) with the alerts channel ID.

If neither is configured, fall back to `DISCORD_PRIMARY_CHANNEL_ID`. Do NOT silently drop the notification.

### Step 2 - Compose the notification

**Non-breaking update:**

```
Chassis update available: `<current> → <latest>`
Changelog: <changelog_url>

Reply `update chassis` to apply, or `skip update` to dismiss until the next version drops.
```

**Breaking update (when `breaking: true`):**

```
:warning: Chassis update available with BREAKING CHANGES: `<current> → <latest>`
Manual review required before applying.

Changelog: <changelog_url>

Reply `update chassis --force` to apply after you've reviewed the changelog, or `skip update` to dismiss until the next version drops.
```

Use the literal template above. Don't editorialize, don't add reassurances, don't elaborate on what's in the changelog. The owner reads the changelog themselves; your job is just to surface the offer.

### Step 3 - Post once

Post the message to the resolved alerts channel via the standard post-to-channel script. Then exit.

The owner's response (`update chassis`, `update chassis --force`, `skip update`) is handled by `skills/chassis-update.md` - a separate Discord trigger. You don't need to wait for or process the reply.

## Important

- **Do not apply the update yourself.** The application path lives in `chassis/scripts/chassis-update.sh` and is gated behind explicit operator consent in Discord.
- **Do not modify state files.** `state/chassis-update/last-offered.json` and `state/chassis-update/dismissed.json` are owned by the gather script and the apply trigger respectively.
- **Do not re-notify** if the same version was already offered this week. The gather script's `already_offered` gate prevents this; if you reach this prompt, the offer is fresh.
- **No nag, ever.** Single message, single offer. The dismiss flow is the owner's lever.

## Out of scope

- Reading the changelog and summarizing it. The owner clicks the link.
- Recommending whether to apply. You don't know the owner's risk appetite.
- Touching production state of any kind.
- Cross-installer telemetry (this heartbeat runs per-install; nothing aggregates).
