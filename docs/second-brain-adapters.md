# Second-brain adapters

Per-installer chassis writes prose (briefings, content stubs, daily logs) and structured rows (LP CRMs, deal pipelines, contact records) to a backend the installer chose at install time. The chassis abstracts the backend so plugins don't carry per-installer branches.

> **Source-of-truth issue:** [<v1-reference-install> #512](<v1-reference-install>#512). Design rationale: `project_behalfbot_install_architecture.md` (Sean voice memo 2026-05-02 13:54).

## Two surfaces

A second brain has both prose and structured semantics. We split them into two cooperating adapters rather than collapsing into one shape:

| Surface | Use cases | Backed by |
|---|---|---|
| `notes` | Briefings, content stubs, daily logs, Pacman proposals, free-form prose | Page/block APIs |
| `database` | LP CRM rows, deal pipelines, contacts, tasks, scheduling | Database / property APIs |

Notion implements both natively. SiYuan implements `notes` natively and raises `NotImplementedError` on `database`. Obsidian (V2) will likely fake `database` via structured frontmatter index files.

## V1 backends

| Backend | Status | Notes |
|---|---|---|
| `siyuan` | ✅ V1 (notes only; `database` raises `NotImplementedError`) | Sean's primary; HTTP kernel API + SQL search |
| `notion` | ✅ V1 (both surfaces) | V1 installer #1 primary; Notion REST API |
| `obsidian` | ❌ V2 | Marc's Protocol Labs leaning; deferred |

## Configuration

Pick the backend in `chassis.config.yaml`:

### SiYuan

```yaml
second_brain:
  backend: siyuan
  siyuan:
    base_url: http://127.0.0.1:6806             # local kernel
    token: ${SIYUAN_TOKEN}                       # from .env (or VW)
    notebook_id: 20231101120000-abc123            # default notebook for create_doc
    deeplink_template: https://s.grid7.com/?id=  # for iPhone-clickable links
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
```

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

When Obsidian + multi-backend writes land:

1. Add `chassis/second_brain/obsidian.py` implementing `NotesAdapter` over the Local REST API plugin or direct file IO.
2. Extend `factory.get_adapter` with the `obsidian` branch.
3. Decide migration tool placement — likely `chassis/scripts/sb-migrate.py` running source→destination via the adapter interfaces.

Until then, the adapters above cover the V1 installer set (SiYuan / Notion).
