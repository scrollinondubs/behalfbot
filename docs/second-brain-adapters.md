# Second-brain adapters

Per-installer chassis writes prose (briefings, content stubs, daily logs) and structured rows (LP CRMs, deal pipelines, contact records) to a backend the installer chose at install time. The chassis abstracts the backend so plugins don't carry per-installer branches.

> **Source-of-truth issue:** [<v1-reference-install> #512](<v1-reference-install>#512). Design rationale: `project_behalfbot_install_architecture.md` (Sean voice memo 2026-05-02 13:54).

## Two surfaces

A second brain has both prose and structured semantics. We split them into two cooperating adapters rather than collapsing into one shape:

| Surface | Use cases | Backed by |
|---|---|---|
| `notes` | Briefings, content stubs, daily logs, Pacman proposals, free-form prose | Page/block APIs |
| `database` | LP CRM rows, deal pipelines, contacts, tasks, scheduling | Database / property APIs |

Notion implements both natively. SiYuan and Obsidian implement `notes` natively and raise `NotImplementedError` on `database`. Faking `database` via structured frontmatter index files in Obsidian is a possible follow-up.

## V1 backends

| Backend | Status | Notes |
|---|---|---|
| `siyuan` | ✅ V1 (notes only; `database` raises `NotImplementedError`) | Sean's primary; HTTP kernel API + SQL search |
| `notion` | ✅ V1 (both surfaces) | V1 installer #1 primary; Notion REST API |
| `obsidian` | ✅ V1 (notes only; `database` raises `NotImplementedError`) | Direct file IO on the vault directory; read-only vaults first-class ([#55](https://github.com/scrollinondubs/behalfbot/issues/55)) |

## Configuration

Pick the backend in `chassis.config.yaml`.

### Credential resolution order

**Credentials live in `.env`, not in `chassis.config.yaml`.** Adapter mode and direct mode read the same vars from the same place, so a secret is never duplicated and flipping `mode` cannot strand one of them. `chassis.config.yaml` is committed; a token must never be pasted into it.

| Backend | Env var (source of truth) | YAML override (optional) | Fallback |
|---|---|---|---|
| siyuan | `SIYUAN_TOKEN` | `second_brain.siyuan.token` | none - **empty token raises `ValueError` at startup** |
| siyuan | `SIYUAN_URL` | `second_brain.siyuan.base_url` | `http://127.0.0.1:6806` |
| siyuan | - | `second_brain.siyuan.notebook_id` | `second_brain.databases.notes_root`; empty raises `ValueError` |
| siyuan | - | `second_brain.siyuan.deeplink_template` | `siyuan://blocks/` |
| notion | `NOTION_API_TOKEN` | `second_brain.notion.token` | none |
| notion | - | `second_brain.notion.notes_root` | `second_brain.databases.notes_root` |
| obsidian | - (no credential) | `second_brain.obsidian.vault_path` | none - required |

Resolution is: **YAML key if set, else env var, else the documented default.** A YAML value of `${SIYUAN_TOKEN}` that expands to nothing counts as unset and falls through rather than shadowing the env var.

The SiYuan adapter refuses to construct with an empty token or an empty `notebook_id`. Both used to be silently tolerated, which produced an adapter that answered every call with `Auth failed [session]`. Failing loudly at server startup is deliberate - `mcp_server.main()` resolves the adapter eagerly so a broken config shows up in `claude mcp list` and the server log, not mid-task.

> **Container gotcha:** from inside the chassis container, `127.0.0.1` is the container itself. A SiYuan kernel running on the host is reachable at `http://host.docker.internal:6806`. Set `SIYUAN_URL` accordingly.

### SiYuan

Every key below is an optional override. With `SIYUAN_URL` / `SIYUAN_TOKEN` in `.env` and `second_brain.databases.notes_root` set, the block can be omitted entirely.

```yaml
second_brain:
  backend: siyuan
  siyuan:
    base_url: http://127.0.0.1:6806              # default; env SIYUAN_URL wins over this default
    token: ${SIYUAN_TOKEN}                        # default: env SIYUAN_TOKEN
    notebook_id: 20231101120000-abc123            # default: second_brain.databases.notes_root
    deeplink_template: siyuan://blocks/           # default; point at a reverse proxy
                                                  # (https://siyuan.example.com/?id=) for
                                                  # phone-clickable links
```

### Notion

```yaml
second_brain:
  backend: notion
  notion:
    token: ${NOTION_INTEGRATION_TOKEN}
    notes_root: 1234abcd-5678-90ef-1234-567890abcdef   # parent page for create_doc
    databases:
      lp_crm: aaaa1111-bbbb-2222-cccc-333333333333
      startup_pipeline: bbbb2222-cccc-3333-dddd-444444444444
    natural_keys:                                       # uniqueness key per DB
      lp_crm: email
      startup_pipeline: deal_name
    active_database: lp_crm                             # default for upsert/query
```

### Obsidian

```yaml
second_brain:
  backend: obsidian
  obsidian:
    vault_path: /home/hugues/second-brain   # absolute path to the vault root
    vault_name: second-brain                 # for obsidian:// deeplinks; defaults to the directory name
    read_only: true                          # pull-only vault clone (e.g. read-only deploy key)
```

Doc ids are vault-relative paths (`Briefings/2026-07-09.md`; the `.md` suffix is optional on input). `create_doc` treats `parent` as a vault-relative directory and empty `parent` as the vault root. No Obsidian process or plugin is required - the adapter reads and writes the vault files directly.

Read-only vaults are first-class: with `read_only: true`, or when the filesystem itself denies writes (pull-only git clone through a read-only deploy key), `create_doc` / `append_to_doc` raise `ObsidianReadOnlyError` naming the cause. Config states intent, the filesystem states truth - a write is refused if either side blocks it, and the error says which. Writes that do proceed are atomic (temp file + rename), doc ids that resolve outside the vault root are rejected, and `create_doc` refuses to overwrite an existing note.

`${VAR}` references resolve against `os.environ` at import time; chassis bootstrap loads `.env` before any plugin imports the adapter.

## Usage

```python
from chassis.second_brain import get_adapter

sb = get_adapter()                       # reads chassis.config.yaml
print(sb.backend)                         # 'notion'

# Notes — free-form prose
doc_id = sb.notes.create_doc(
    parent="<parent-id-or-empty-for-default>",
    title="2026-05-07 morning briefing",
    body="# Daily roundup\n\nThree items today:\n- ..."
)
sb.notes.append_to_doc(doc_id, "\n\n**Update 18:00:** ...")
print(sb.notes.get_deeplink(doc_id))      # iPhone-clickable URL

# Database — structured rows (Notion only in V1)
hit = sb.database.upsert_row({
    "_database": "lp_crm",
    "email": "alice@fund.com",
    "name": "Alice Investor",
    "stage": "warm",
})
sb.database.update_property(hit, "last_outreach_at", "2026-05-07")

# Search
hits = sb.notes.search("morning briefing", limit=5)
for h in hits:
    print(h.title, h.deeplink)

# Recent activity - docs created/modified in a time window, newest first
from datetime import datetime, timedelta
hits = sb.notes.list_recent(
    since=datetime.now() - timedelta(days=1),
    until=datetime.now(),
    min_content_len=200,
    limit=50,
)
```

## `list_recent` per-backend divergences

All three backends implement `list_recent(since, until, min_content_len, limit)` - docs created or modified in `[since, until)`, newest first. The implementations are honest but the underlying signals are NOT equivalent. Naive datetimes are interpreted as local time.

| | Timestamp source | Granularity | `min_content_len` measure | Caveats |
|---|---|---|---|---|
| SiYuan | `blocks.updated` (kernel-local clock) | second | `SUM(LENGTH(content))` over the doc's child blocks via correlated subquery - the doc row's own `content` column holds only the TITLE (verified against a live kernel: max 81 chars over 283 docs), so it cannot be used for length filtering | cleanest of the three; block timestamps reflect actual edits |
| Obsidian | filesystem mtime of `*.md` | filesystem-dependent | file size in bytes (frontmatter and markdown syntax count toward it; multi-byte characters count per byte) | NOISIEST: a git pull, iCloud resync, or any sync tool that rewrites files produces false "activity". Treat hits as candidates, not facts |
| Notion | `last_edited_time` via `/search`, descending scan with client-side windowing (the endpoint has no timestamp filter) | MINUTE - Notion truncates seconds, so edits at a window boundary can fall on either side | reconstructed-markdown length of the first 100 blocks; costs one extra API call per candidate page, so leave at 0 unless needed | only pages shared with the integration are visible; scan is capped at 500 pages per call |

### SiYuan's SQL index is eventually consistent

`search()` and `list_recent()` read SiYuan's SQL index (`SELECT ... FROM blocks`), which the kernel populates **asynchronously** after a write commits.

Verified against a live kernel: immediately after `appendBlock` returned success, the new block was absent from the `blocks` table for several seconds, then appeared once the kernel flushed its transaction.

This is **not a write failure** - the write persists correctly and `read_doc` on the returned id reflects it right away. Only the index lags. The consequence:

> A caller that writes with `create_doc` / `append_to_doc` and then immediately reads back via `search()` or `list_recent()` will NOT see the doc it just wrote.

Callers that write-then-read-back must tolerate the lag: keep the id `create_doc` returned rather than searching for the doc by title, or poll. Obsidian (direct filesystem IO) and Notion (API-backed) have no equivalent lag - a write is visible to the next read. This divergence is SiYuan-only, and it is the one place where the adapters' shared interface hides genuinely different semantics.

### SiYuan `search()`: LIKE wildcards pass through, and cannot be escaped

`SiYuanNotes.search(query)` interpolates `query` into a SQL `LIKE '%...%'` pattern. **`%` and `_` in a query stay live as LIKE wildcards.** A search for `50%` also matches `50 percent`; `a_b` also matches `axb`.

This is a known tradeoff, not an oversight. **SiYuan's `/api/query/sql` does not accept an `ESCAPE` clause**, so the wildcards cannot be neutralized. Measured against a live kernel:

| Statement | Result |
|---|---|
| `... content LIKE '%Vibecode%'` | `code 0`, 397 rows |
| `... content LIKE '%Vibecode%' ESCAPE '\'` | `code 0`, `data: null` - **zero rows** |
| same with `ESCAPE '!'` or `ESCAPE '#'` | `code 0`, `data: null` - **zero rows** |

Any escape character makes the kernel refuse the query. An earlier revision of the adapter added `ESCAPE '\'` to tame the wildcards and thereby broke `search()` outright: every query returned no hits, against a kernel holding hundreds of matches. Do not re-add it.

Wildcard pass-through is safe. The single-quote doubling in `_escape()` is the injection defense - a query can never break out of the string literal it sits in - and results are `LIMIT`-capped. The only cost is that a query containing a wildcard matches more broadly than the caller may have intended.

### SiYuan `data: null` means the query was REFUSED, not "no matches"

Related, and the reason the above was silent for so long. SiYuan answers a SQL statement it will not run with a **success-shaped null**:

```json
{"code": 0, "msg": "", "data": null}
```

A genuinely empty result set comes back as `[]`. The adapter's `_query_sql()` therefore raises `SiYuanError` (naming the offending statement) on a null payload rather than coercing it to an empty list. Treating null as "no rows" is what turned a rejected query into a plausible-looking "nothing found". Only the `/api/query/sql` path is hardened this way - other endpoints (`appendBlock`) return null legitimately.

## `second_brain.mode` and the `secondbrain` MCP server

`chassis.config.yaml` carries a `mode` key next to `backend`:

```yaml
second_brain:
  backend: siyuan     # siyuan | notion | obsidian
  mode: direct        # direct | adapter (direct is the default, and what a missing key means)
```

- **`direct`** (default): today's behavior. The backend's own MCP server (`siyuan` / `notion`) is registered in `.mcp.json`; chassis scripts talk to the backend natively. Installs whose config predates the key see zero change.
- **`adapter`**: the chassis-owned `secondbrain` MCP server (`chassis/second_brain/mcp_server.py`) is registered INSTEAD, exposing one fixed tool namespace over `get_adapter()`: `create_doc`, `append_to_doc`, `read_doc`, `search`, `list_recent`, `get_deeplink`. The native backend server is deliberately NOT registered - tool availability is the guardrail that keeps prompts backend-neutral. One server over N adapter classes, not one server per backend: MCP tool names are namespaced by server name, so per-backend servers would mean per-backend prompt text, which defeats the abstraction.

Registration is driven by `_enable_when` predicates in `chassis/.mcp.json.template`, evaluated by `chassis/scripts/hydrate-mcp-json.py` (which supports `==`, `!=`, and `&&`; a missing `mode` key satisfies `mode != 'adapter'`, keeping legacy configs on the direct path).

Backend support per mode:

| backend | direct | adapter |
|---|---|---|
| siyuan | `siyuan` MCP server (`siyuan-mcp@1.0.4`) | `secondbrain` |
| notion | `notion` MCP server (`@suekou/mcp-notion-server`) | `secondbrain` |
| obsidian | NO second-brain MCP surface - no suitable native server exists (community options require the Obsidian desktop app + Local REST API plugin, which headless container installs do not run) | `secondbrain` |

Obsidian installs are therefore adapter-mode-only if they want a second-brain MCP surface at all.

## Contract

The `NotesAdapter` and `DatabaseAdapter` Protocols live in `chassis/second_brain/base.py`. Implementations MUST:

- Raise the appropriate adapter-error subclass (e.g. `SiYuanError`, `NotionError`) on backend failures, not bare `Exception`.
- Return real ids / urls — never empty strings on success.
- Treat empty `parent` in `create_doc` as a signal to use the configured default (`notes_root` for Notion, hpath="/" for SiYuan).
- Truncate single-line content over the backend's hard limit (Notion: 2000 chars per text block) rather than failing.

The `SearchHit` dataclass is the canonical return shape for both `notes.search` and `database.query`. Adapters MAY put backend-specific extras under `raw` for callers that need them.

## V1 caveats

- **No simultaneous writes.** One backend per install. Migration tools (e.g. SiYuan→Notion bulk migration for an installer who switches) are V2.
- **Markdown↔block fidelity is paragraph-only in Notion adapter V1.** Headings + lists are read out cleanly but `create_doc` writes everything as paragraph blocks. Heading/list parsing is a follow-up — current <v1-reference-install> briefings render as plain prose in SiYuan, so feature-parity is preserved.
- **Notion `database.query` filters are `equals`-only on rich_text properties.** Numeric/date filters land in the follow-up; the natural-key upsert flow only needs equality.
- **Notion property type inference is heuristic.** For exact schema fidelity, callers can pass already-shaped Notion property dicts under the `_raw_properties` key (passes through verbatim).

## Migration to V2

Obsidian landed in V1 via [#55](https://github.com/scrollinondubs/behalfbot/issues/55) (`chassis/second_brain/obsidian.py`, direct file IO, `obsidian` branch in `factory.get_adapter`). Still V2:

1. Multi-backend writes / migration tooling - likely `chassis/scripts/sb-migrate.py` running source→destination via the adapter interfaces.
2. Obsidian `database` surface faked via structured frontmatter index files, if an installer needs it.
3. Obsidian Local REST API plugin integration (live-app features like block-level linking), if direct file IO proves insufficient.

Until then, the adapters above cover the V1 installer set (SiYuan / Notion / Obsidian).
