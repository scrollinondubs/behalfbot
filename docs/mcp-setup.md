# MCP Setup

Per-MCP install instructions for chassis instances. Source-of-truth template: [`chassis/.mcp.json.template`](../chassis/.mcp.json.template).

The template ships every MCP the V1 reference install runs, categorized by activation tier. Installers run [`chassis/scripts/bootstrap-mcp-config.sh`](../chassis/scripts/bootstrap-mcp-config.sh) once at install time — it reads the template, hydrates every `<PLACEHOLDER>` from the Vaultwarden-sourced `.env`, strips the `_README` / `_role` / `_enable_when` doc fields, validates the result parses as JSON, and writes `$CHASSIS_HOME/.mcp.json` with `0600` perms. Idempotent: skips if the file already exists unless invoked with `--force`. Use `--dry-run` to preview without writing.

> **Discord is NOT in the MCP list.** Discord lives in Claude Code's plugin layer (the `discord@claude-plugins-official` plugin), not as an MCP server. Wire it via `claude /discord:configure` after installing the plugin.

## Bootstrap (recommended path)

```bash
# At install time, after .env has been hydrated from Vaultwarden:
bash chassis/scripts/bootstrap-mcp-config.sh

# Preview without writing (logs which placeholders were filled vs left empty):
bash chassis/scripts/bootstrap-mcp-config.sh --dry-run

# Rotate a credential in Vaultwarden, then re-run to refresh:
bash chassis/scripts/bootstrap-mcp-config.sh --force
```

Unfilled placeholders (env var unset / Vaultwarden offline) stay as `<NAME>` strings in the output. The script logs which placeholders survived so you can fill them manually or set the env var and rerun with `--force`.

Once the file lands, restart Claude Code so it re-reads. The manual hydration walkthrough below is still useful for understanding what each MCP needs and where to source it from — the bootstrap script just automates the copy + fill once that knowledge exists in Vaultwarden.

---

## Tiering

| Tier | MCPs | When |
|---|---|---|
| **Always-on** (no auth) | `memory`, `playwright`, `context7` | Every install. Keep. |
| **Always-on with config** | `github` | Every install touches GitHub. Provision agent-side PAT. |
| **Pick one** (second-brain) | `siyuan` OR `notion` | Per `chassis.config.yaml.second_brain.backend`. Delete the other. |
| **Optional, opt-in** | `brave-search`, `tavily`, `turso`, `amplitude`, `n8n`, `loom`, `remarkable`, `oura`, `frame0` | Per `chassis.config.yaml.modules.*` flags. Delete entries you don't activate. |

---

## Per-MCP setup

### memory (always-on, no auth)

Knowledge graph for cross-session continuity. Storage at `${CHASSIS_HOME}/memory/memory.jsonl` (gitignored).

```bash
npx -y @modelcontextprotocol/server-memory --version  # smoke check
```

No env vars to wire.

### playwright (always-on, no auth)

Browser automation fallback (sites that block API access, OAuth flows, etc.). Pulls Chromium on first run (~200MB).

```bash
npx -y @playwright/mcp@latest --help
```

### context7 (always-on, no auth)

Upstash's up-to-date library docs MCP. No auth.

### github (always-on, with config)

Setup:
1. Provision an **agent-side GitHub account** (per the dual-identity pattern). Don't use your personal account.
2. Generate a Personal Access Token: Settings → Developer settings → Personal access tokens (classic) → Generate.
3. Scopes: `repo`, `workflow`, `read:org`. (Add `admin:repo_hook` if any plugin needs to manage webhooks.)
4. Save to your password manager labeled `<INSTANCE>-github-pat`.
5. Hydrate into `.mcp.json` at install time.

Sanity check: `gh auth status` returns the agent-side identity (NOT your personal account).

### siyuan OR notion (pick one)

#### siyuan

If you're running SiYuan (self-hosted block-based notes):

1. SiYuan running at a URL accessible from your chassis machine (typically `https://notes.<your-domain>` via Cloudflare Tunnel or Tailscale).
2. Generate an API token in SiYuan settings → About → API token.
3. Save token to password manager labeled `<INSTANCE>-siyuan-token`.
4. Hydrate into `.mcp.json`.

#### notion

If you're running Notion (cloud, page+block model):

1. In your Notion workspace (admin): Settings → Connections → Develop or manage integrations → New integration.
2. Capabilities: Read content, Update content, Insert content, Read user info without email.
3. Copy the Internal Integration Token. Save labeled `<INSTANCE>-notion-token`.
4. Share the relevant pages/databases with the integration (pages won't be visible to the integration until shared).
5. Hydrate into `.mcp.json`.

---

## Optional MCPs

### brave-search

Free tier: 2k queries/month. Generate API key at https://brave.com/search/api/.

### tavily

Premium research. Generate API key at https://tavily.com/.

### turso

Only if using Turso for app databases. Generate API token at https://app.turso.tech/account/api-tokens. `TURSO_ORG` is your org slug, `TURSO_DEFAULT_DB` is the database name.

### amplitude

Product analytics. HTTP MCP — no env vars; auth handled at first call (browser-based OAuth). Use the `eu` URL for EU-region projects, swap to `us` otherwise.

### n8n

Only if self-hosting n8n. Generate API key inside n8n: Settings → API. Wire `N8N_API_URL` + `N8N_API_KEY`.

### loom

Only if you want a Loom MCP (chassis also ships `chassis/scripts/process-loom.sh` as a CLI alternative).

### remarkable

Only if you have a reMarkable tablet. USB connection required.

### oura

Only if BFL plugin enabled AND `strava_oura_reconcile: true`. Generate Personal Access Token at https://cloud.ouraring.com/personal-access-tokens.

### frame0

Wireframe / mockup generation. No auth required for stdio mode.

---

## Lessons baked in

- **#6** — agent-side accounts for every credential. Don't reuse personal identity.
- **#28** — what matters is what's on disk. After hydrating `.mcp.json`, restart Claude Code so it re-reads.

---

## Cross-references

- `chassis/.mcp.json.template` — the canonical template
- `docs/security.md` — extending the guardrails allowlist for new external APIs (TBD per issue #498)
- `docs/installer-homework.md` — credentials checklist
