# Prompt Setup

Per-install rendering of chassis prompt templates. Source-of-truth: `*.md.template` files alongside the prompts under `plugins/*/scheduled-tasks/` and `chassis/scheduled-tasks/`.

The chassis ships prompts as `.md.template` files with `${VAR}` placeholders. The bootstrap script renders them to `.md` at install time, substituting values from `chassis.config.yaml` + `.env`. Rendered `.md` files are gitignored — they're per-install runtime artifacts.

Sibling pattern: [`mcp-setup.md`](./mcp-setup.md) (.mcp.json from a template). Same shape.

## Bootstrap (recommended path)

```bash
# After hydrating .env from Vaultwarden + filling chassis.config.yaml:
bash chassis/scripts/bootstrap-prompts.sh

# Preview without writing:
bash chassis/scripts/bootstrap-prompts.sh --dry-run

# Rotate a value in chassis.config.yaml or .env, then re-render:
bash chassis/scripts/bootstrap-prompts.sh --force
```

Idempotent: re-running is a no-op unless `--force` or a `.template` was edited (mtime check).

Run after every `git subtree pull` of chassis main: if a template changed upstream, the rendered version needs to refresh.

## Required configuration

The script reads from two places.

### chassis.config.yaml (non-sensitive identity)

```yaml
identity:
  principal:
    full_name: <e.g. "Alex Smith">
    first_name: <e.g. "Alex">
    home_city: <e.g. "Lisbon">
    home_country: <e.g. "Portugal">
    timezone: <e.g. "Europe/Lisbon">

  assistant:
    name: <e.g. "Ozzy">
    email: <e.g. "ozzy-agent@agentmail.to">

discord_channels:
  primary_label: <e.g. "#alex">
  alerts_label: <e.g. "#alex-devops">
  ops_label: <e.g. "#alex-ops">

quiet_hours:
  start: "22:00"
  end:   "09:00"
```

### .env (sensitive — Vaultwarden-hydrated)

```
PRINCIPAL_MOBILE=+1xxxxxxxxxx
DISCORD_PRIMARY_CHANNEL_ID=14xxxxxxxxxxxxxxxxx
DISCORD_ALERTS_CHANNEL_ID=14xxxxxxxxxxxxxxxxx
DISCORD_OPS_CHANNEL_ID=14xxxxxxxxxxxxxxxxx
```

Sensitive values stay out of any committed file. Discord IDs are technically not secrets but ID-based fingerprints; treat them as PII-adjacent.

### Derived (automatic)

```
ASSISTANT_DISPLAY_NAME = "<assistant.name> - <principal.full_name>'s AI assistant"
```

Override by setting `ASSISTANT_DISPLAY_NAME` explicitly if you want a different signature.

## Missing-value behavior

If any required value is empty or still has a `<placeholder>` shape, the script prints ALL missing values at once and exits with code 1 without writing anything. Fix them in one pass, re-run.

## What gets rendered

Currently rendered:

- `plugins/angel-protocol/scheduled-tasks/welfare-check-prompt.md`
- `plugins/angel-protocol/scheduled-tasks/dormant-sean-prenudge-prompt.md`
- `plugins/remarkable/scheduled-tasks/remarkable-health-alert-prompt.md`

The script discovers `*.md.template` by glob — add new templates by dropping them next to existing prompts. They render on the next `bootstrap-prompts.sh` invocation.

## Allowlisted variables

Only these `${VAR}` placeholders are expanded; others (e.g. `${CHASSIS_HOME}` in script examples inside a prompt) pass through unchanged so prose around shell snippets stays readable.

| Variable | Source |
|---|---|
| `${PRINCIPAL_NAME}` | `identity.principal.full_name` |
| `${PRINCIPAL_FIRST_NAME}` | `identity.principal.first_name` |
| `${PRINCIPAL_HOME_CITY}` | `identity.principal.home_city` |
| `${PRINCIPAL_HOME_COUNTRY}` | `identity.principal.home_country` |
| `${PRINCIPAL_TIMEZONE}` | `identity.principal.timezone` |
| `${PRINCIPAL_MOBILE}` | `.env` |
| `${ASSISTANT_NAME}` | `identity.assistant.name` |
| `${ASSISTANT_EMAIL}` | `identity.assistant.email` |
| `${ASSISTANT_DISPLAY_NAME}` | derived |
| `${DISCORD_PRIMARY_LABEL}` | `discord_channels.primary_label` |
| `${DISCORD_PRIMARY_CHANNEL_ID}` | `.env` |
| `${DISCORD_ALERTS_LABEL}` | `discord_channels.alerts_label` |
| `${DISCORD_ALERTS_CHANNEL_ID}` | `.env` |
| `${DISCORD_OPS_LABEL}` | `discord_channels.ops_label` |
| `${DISCORD_OPS_CHANNEL_ID}` | `.env` |
| `${QUIET_HOURS_START}` | `quiet_hours.start` |
| `${QUIET_HOURS_END}` | `quiet_hours.end` |

Extending the allowlist: edit `bootstrap-prompts.sh` (variable list + `REQUIRED` array + `allowlist` string in `render_one`).
