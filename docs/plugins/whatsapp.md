# Plugin: WhatsApp

Read-only WhatsApp group monitoring with a hard-coded DM-blocking architecture. Per lesson #30 (privacy boundaries are surface-specific) — group context is already disclosed to N participants, but DMs carry an expectation of privacy from the other party that the installer cannot unilaterally consent to share. The plugin enforces this at three layers.

## Activation

```yaml
# chassis.config.yaml
modules:
  whatsapp:
    enabled: true
    allowlist_path: ${CHASSIS_HOME}/plugins/whatsapp/data/whatsapp-allowlist.json
    safe_wrapper_path: ${CHASSIS_HOME}/plugins/whatsapp/scripts/wacli-safe.sh
```

Bootstrap → env exports:

```bash
export CHASSIS_WHATSAPP_SAFE="${CHASSIS_HOME}/plugins/whatsapp/scripts/wacli-safe.sh"
export WHATSAPP_ALLOWLIST_PATH="${CHASSIS_HOME}/plugins/whatsapp/data/whatsapp-allowlist.json"
```

## Three layers of enforcement

1. **Plugin wrapper** (`scripts/wacli-safe.sh`) — only routes `messages list` / `messages search` to `wacli` when a `--chat` filter resolves to a JID in the allowlist. Pass-through for metadata-only subcommands.
2. **Chassis-core hook** (`chassis/.claude/hooks/guardrails.sh`) — when `CHASSIS_WHATSAPP_SAFE` is set, blocks any raw `wacli messages|send|media` invocation that doesn't go through the wrapper path. Second-line defense.
3. **Allowlist file** (`plugins/whatsapp/data/whatsapp-allowlist.json`) — groups-only, hand-curated by the installer. `groups[]` allowed; `_excluded_known_groups[]` for documenting the no's.

## Installation

1. Install `wacli` upstream. Confirm the daemon is running and synced.
2. Generate the allowlist file from the template:
   ```bash
   cp plugins/whatsapp/data/whatsapp-allowlist.template.json plugins/whatsapp/data/whatsapp-allowlist.json
   ```
   Edit to add the groups you want the agent to read. Use `wacli groups list --json` to discover JIDs.
3. Enable in `chassis.config.yaml`.
4. Re-run the chassis bootstrap so env exports propagate.
5. Restart chassis service so the hook picks up the new env.

## Curating the allowlist

- **Group must be `@g.us` suffix.** DMs (`@s.whatsapp.net`), individual `@lid`, `@newsletter` are blocked even if added.
- **2-person groups are functionally DMs** — same privacy expectation. Move to `_excluded_known_groups[]`.

## What does NOT ship

- Specific groups from the V1 reference. Template ships with empty `groups[]`.
- Any send-side `wacli` capabilities. Read-only by design.

## Lesson references

- **#30** — privacy boundaries surface-specific
- **#27** — anchored regex in chassis-core hook prevents heredoc false positives

## Cross-references

- `plugins/whatsapp/scripts/wacli-safe.sh` — the wrapper
- `plugins/whatsapp/data/whatsapp-allowlist.template.json` — allowlist template
- `plugins/whatsapp/skills/whatsapp.md` — skill body (frontmatter retrofitted via <v1-reference-install> PR #504)
- `chassis/.claude/hooks/guardrails.sh` — chassis-core enforcement
- `docs/security.md` — plugin extensions to guardrails
- `docs/LESSONS_FROM_V1.md` — full lesson list, especially #30
