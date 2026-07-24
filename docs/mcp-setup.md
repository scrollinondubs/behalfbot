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
| **Pick one** (second-brain) | `siyuan` OR `notion` OR `secondbrain` | Per `chassis.config.yaml.second_brain.backend` + `second_brain.mode`. Mode `direct` (default): the backend's native server. Mode `adapter`: the chassis-owned `secondbrain` server INSTEAD, and the native server is not registered. Obsidian has no native server - obsidian installs need `mode: adapter` for any second-brain MCP surface. |
| **Google** (headless installs) | `gmail`, `google-calendar`, `google-sheets` | Per `chassis.config.yaml.modules.google.*`. All default off. The only path to Google on a box you reach over SSH - Claude's hosted connectors need a browser and never complete remotely. |
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

### secondbrain (adapter mode)

If `chassis.config.yaml.second_brain.mode` is `adapter`, the hydrator registers the chassis-owned `secondbrain` server (`chassis/second_brain/mcp_server.py`) and OMITS the native backend server below. It needs no extra auth of its own - it reads the backend credentials from `chassis.config.yaml` / `.env` via the second-brain adapter factory. Tools: `create_doc`, `append_to_doc`, `read_doc`, `search`, `list_recent`, `get_deeplink` - identical on every backend. See docs/second-brain-adapters.md.

### siyuan OR notion (pick one, direct mode)

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

## Google MCPs (gmail, google-calendar, google-sheets)

Off by default. Turn them on with `modules.google.gmail` / `modules.google.calendar` /
`modules.google.sheets` in `chassis.config.yaml`.

**Who needs them:** every headless install. Claude's hosted Google connectors need a
browser and a human at the keyboard, so they do not complete over SSH - on a Linux box
or VPS there is no other way to reach Google. A desktop install that already has the
hosted connectors working can leave all three flags false, unless it wants Gmail
attachment downloads (#39, #40) or Sheets cell writes (#63), neither of which the
hosted connectors expose. Reading a spreadsheet as a Drive file is not writing a cell.

**Packages:**

| Server | Package | Notes |
|---|---|---|
| `gmail` | [`@gongrzhe/server-gmail-autoauth-mcp`](https://github.com/gongrzhe/server-gmail-autoauth-mcp) | The server the first headless install has been running in production. Exposes `download_attachment`. |
| `google-calendar` | [`@cocal/google-calendar-mcp`](https://github.com/nspady/google-calendar-mcp) | Supports `ENABLED_TOOLS` filtering, which is how `trust_line.calendar` is enforced. |
| `google-sheets` | [`@shivaduke28/google-sheets-mcp`](https://github.com/shivaduke28/google-mcp) | Reads the same `GOOGLE_OAUTH_CREDENTIALS` client as Calendar. Enforces a per-spreadsheet write allowlist server-side. Tools: `list-spreadsheets`, `get-spreadsheet`, `get-values`, `update-values`, `append-values`, `create-from-template`. |

**Credentials:** all three read ONE OAuth client of type *Desktop app*
(`gcp-oauth.keys.json`), and each keeps its own consented token. A service account is
not an option: reading a mailbox with one needs domain-wide delegation, which needs a
Workspace admin and cannot be granted on a personal Gmail account.

**The consent step needs a browser, and your server does not have one.** Run the auth
flow on your laptop and copy the token files up. Full procedure, plus the Google Cloud
Console setup and the 7-day test-mode token expiry that bites everyone:
[`docs/installer-homework.md`](installer-homework.md) section 4.

**Scope is not access.** A Sheets token can only reach spreadsheets that have been
**shared with the agent's Google account** - Editor, if it is to write. This is the
single most common reason a correctly-configured Sheets server still cannot open a
sheet, and it is independent of scope: neither one substitutes for the other.
[`installer-homework.md`](installer-homework.md) section 4g.

**Calendar write access** is gated on `trust_line.calendar`. `read_only` (the default)
registers the server with read tools only; `read_write` adds `create-event`,
`update-event`, `delete-event`, `respond-to-event`. This is tool-gating, not
scope-gating - the token holds a read-write scope either way. Gmail has no equivalent
filter and is all-or-nothing, send included.

**Sheets write access** is gated on `trust_line.sheets`, by a different and stronger
mechanism. The server has no tool filter, so its write tools are always advertised;
it instead **refuses the write at call time** unless the target spreadsheet is listed
`readwrite` in the `GOOGLE_MCP_CONFIG` allowlist. `read_only` (the default) leaves that
file unset, so every write is denied by the server. `read_write` points at an allowlist
naming the writable spreadsheets by id - there is deliberately no blanket write tier.
Caveat, stated rather than hidden: `create-from-template` checks only read access, so
on the read floor it can still copy a readable sheet into a new file. It cannot alter
an existing one. The Sheets server also requests a full `drive` scope alongside
`spreadsheets`, which is broader than the module name suggests.

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

## Drift detection + reconcile (`reconcile-mcp-config.py`)

A live `.mcp.json` can drift from what `chassis.config.yaml` + the template would hydrate today. When it does, a `--force` re-hydrate (or any regen) **silently drops** every server that is present in the live file but not enabled in config - no error, no warning, working MCP integrations just vanish. Real case: a live file running 15 servers where the config only enables 6; a `--force` there would drop the other 9.

[`chassis/scripts/reconcile-mcp-config.py`](../chassis/scripts/reconcile-mcp-config.py) detects that gap before it bites, and can write the missing flags back into `chassis.config.yaml`. It reuses the hydrator's own gating logic, so its answer matches what a real hydrate would produce. It is **read-only on `.mcp.json`** - `--fix` only ever edits `chassis.config.yaml`, after backing it up.

```bash
# Report drift (default). Exit 0 = clean, exit 1 = drift, exit 2 = bad input.
python3 chassis/scripts/reconcile-mcp-config.py --check

# Same, machine-readable (used by smoke-test.sh + the bootstrap --force guard):
python3 chassis/scripts/reconcile-mcp-config.py --json

# Write the preserving flags into chassis.config.yaml (idempotent, backs up first):
python3 chassis/scripts/reconcile-mcp-config.py --fix
```

Paths default from `$CUSTOMER_HOME` / `$CHASSIS_HOME`; override with `--config`, `--template`, `--mcp`, `--env`.

### The three sets

| Set | Meaning | Action |
|---|---|---|
| **PRESENT_BUT_WOULD_DROP** | Live now, but a re-hydrate would NOT emit it (config never enabled it). The dangerous set. | `--fix` adds the exact `_enable_when` flag that preserves each one. |
| **WOULD_EMIT_BUT_MISSING** | Config enables it but the live file lacks it. Under-provisioned; a re-hydrate would ADD it. | Info only. Re-hydrate when convenient. |
| **CONSISTENT** | Present in both. | None. |

Two extra checks ride along: every server that WOULD emit is verified against `.env` for the `<PLACEHOLDER>` tokens it needs (so a later hydrate does not produce placeholder-broken entries - same condition the hydrator exits 2 on), and host-vs-container **path-model mismatches** are flagged where detectable (a live entry carrying a host absolute path where the template renders a container path like `${CHASSIS_ROOT:-/app/chassis}/...`).

`--fix` only auto-applies `==`-gated flags. The second-brain servers gate on `mode != 'adapter'`; "fixing" one by flipping mode would drop the adapter server instead, so those are surfaced with a note and left to a human.

### Integration points

- **`smoke-test.sh`** runs the check as `mcp_config_drift`. It FAILs only on PRESENT_BUT_WOULD_DROP (the destructive set); the info sets do not fail the smoke run.
- **`bootstrap-mcp-config.sh --force`** runs the check before overwriting an existing `.mcp.json`. If PRESENT_BUT_WOULD_DROP is non-empty it **refuses** (exit 3) and points at `reconcile-mcp-config.py --fix`. Legit token-rotation `--force` runs are unaffected - a consistent install has an empty drop set and the guard passes silently. Pass `--allow-drop` to overwrite and accept the loss on purpose.

---

## Lessons baked in

- **#6** — agent-side accounts for every credential. Don't reuse personal identity.
- **#28** — what matters is what's on disk. After hydrating `.mcp.json`, restart Claude Code so it re-reads.

---

## Cross-references

- `chassis/.mcp.json.template` — the canonical template
- `docs/security.md` — extending the guardrails allowlist for new external APIs (TBD per issue #498)
- `docs/installer-homework.md` — credentials checklist
