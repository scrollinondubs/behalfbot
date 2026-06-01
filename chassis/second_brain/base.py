"""Adapter contracts for second-brain backends.

Two surfaces, not one — second brains have prose semantics (briefings, content
stubs, daily logs, free-form proposals) AND structured semantics (CRM rows,
contact records, deal pipelines). One interface can't model both cleanly:

- `notes` is hierarchical, free-form, addressable by block/page id.
- `database` is row-oriented, schema-shaped, queried by property filter.

A backend may implement only `notes` for V1 (Obsidian, naive SiYuan) and fake
`database` via structured-frontmatter index files when needed. Notion implements
both natively. SiYuan implements `notes` natively and `database` via SQL queries
against its underlying sqlite store.

Per-backend caveats live in each implementation module's docstring.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class SearchHit:
    """Result row from `NotesAdapter.search` or `DatabaseAdapter.query`."""

    id: str
    title: str
    snippet: str
    deeplink: str
    score: float = 0.0
    raw: dict[str, Any] | None = None


@runtime_checkable
class NotesAdapter(Protocol):
    """Free-form prose surface — briefings, content stubs, daily logs."""

    def create_doc(self, parent: str, title: str, body: str) -> str:
        """Create a new doc/page under `parent`. Return its id (block_id or page_id).

        `parent` is a backend-specific identifier:
          - SiYuan: hpath like `/Briefings` or block id like `20260507083000-abcd123`
          - Notion: parent page id (32-char uuid)
          - Obsidian: vault-relative directory like `Briefings/`
        """
        ...

    def append_to_doc(self, doc_id: str, content: str) -> None:
        """Append markdown `content` to an existing doc identified by `doc_id`."""
        ...

    def read_doc(self, doc_id: str) -> str:
        """Return the doc's markdown body. Raise on not-found."""
        ...

    def get_deeplink(self, doc_id: str) -> str:
        """Return a URL that, when clicked, opens the doc in the user's app/device.

        Per-backend conventions:
          - SiYuan: typically `<deeplink_template>{id}` where the template is set
            in chassis.config.yaml (e.g. `https://s.grid7.com/?id=`)
          - Notion: `https://www.notion.so/<workspace>/<page-id-without-hyphens>`
          - Obsidian: `obsidian://open?vault=<vault>&file=<path>`
        """
        ...

    def link_blocks(self, from_id: str, to_id: str) -> None:
        """Create a cross-reference from one doc/block to another.

        Optional capability — adapters that don't support real linking SHOULD
        raise `NotImplementedError` rather than silently no-op. Callers that
        need link semantics can fall back to inline markdown links.
        """
        ...

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        """Full-text search prose. Returns hits ordered by relevance."""
        ...


@runtime_checkable
class DatabaseAdapter(Protocol):
    """Structured-row surface — CRM, contacts, tasks, deal pipelines.

    V1 chassis ships `database` only for Notion. SiYuan + Obsidian raise
    NotImplementedError; callers either fall back to `notes` representation or
    short-circuit on `isinstance(adapter.database, NotImplementedDatabase)`.
    """

    def query(self, filters: dict[str, Any] | None = None, limit: int = 50) -> list[SearchHit]:
        """Query rows matching `filters`. Schema is per-database/per-installer."""
        ...

    def upsert_row(self, properties: dict[str, Any]) -> str:
        """Insert or update a row keyed by the natural unique field for the database.

        Returns the row id. The natural-key strategy is database-config-driven —
        e.g. for an LP CRM, the unique key is `email` (per the Notion schema);
        for a deal-pipeline DB it might be `deal_name + investor`.
        """
        ...

    def update_property(self, row_id: str, key: str, value: Any) -> None:
        """Patch a single property on an existing row."""
        ...


@runtime_checkable
class SecondBrainAdapter(Protocol):
    """Top-level adapter exposing both surfaces.

    Use `chassis.second_brain.get_adapter()` to instantiate; do not construct
    backend classes directly from plugin code (couples the plugin to a backend).
    """

    backend: str  # 'siyuan' | 'notion' | 'obsidian'
    notes: NotesAdapter
    database: DatabaseAdapter


class NotImplementedDatabase:
    """Sentinel database adapter for backends that don't support structured rows.

    All methods raise NotImplementedError with a clear backend-specific message.
    Used by SiYuan + Obsidian adapters in V1.
    """

    def __init__(self, backend: str) -> None:
        self._backend = backend

    def _raise(self, op: str) -> None:
        raise NotImplementedError(
            f"DatabaseAdapter.{op} is not implemented for backend={self._backend!r}. "
            f"Use NotesAdapter for free-form writes, or switch to a backend that "
            f"supports structured rows (e.g. notion). See docs/second-brain-adapters.md."
        )

    def query(self, filters: dict[str, Any] | None = None, limit: int = 50) -> list[SearchHit]:
        self._raise("query")
        return []

    def upsert_row(self, properties: dict[str, Any]) -> str:
        self._raise("upsert_row")
        return ""

    def update_property(self, row_id: str, key: str, value: Any) -> None:
        self._raise("update_property")
