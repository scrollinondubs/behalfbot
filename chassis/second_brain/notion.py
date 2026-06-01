"""Notion adapter — page+block model via Notion's REST API.

Implements both surfaces:

- `notes` over the Pages + Blocks endpoints (free-form prose pages with
  appendable block children).
- `database` over the Databases endpoint (structured rows with property schemas).

Per-installer config (chassis.config.yaml):

    second_brain:
      backend: notion
      notion:
        token: ${NOTION_INTEGRATION_TOKEN}        # from .env / Vaultwarden
        notes_root: <page-id>                       # parent page for create_doc
        databases:                                  # named handles → uuid
          lp_crm: <database-id>
          startup_pipeline: <database-id>
        natural_keys:                               # which property is the
          lp_crm: email                             # uniqueness key per DB
          startup_pipeline: deal_name

Notion's notes are pages-of-blocks — `create_doc` creates a new child page
under `notes_root` and seeds its block children from the markdown body.
`append_to_doc` adds blocks to an existing page. `read_doc` reconstructs
markdown from the page's block tree.

For databases the natural-key map drives `upsert_row` semantics: query by the
natural-key value, patch if found, create if not.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from chassis.second_brain.base import (
    DatabaseAdapter,
    NotesAdapter,
    SearchHit,
    SecondBrainAdapter,
)

NOTION_API_VERSION = "2022-06-28"
NOTION_API_ROOT = "https://api.notion.com/v1"


class NotionError(RuntimeError):
    """Raised when Notion API returns a non-2xx response."""


def _request(token: str, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    url = NOTION_API_ROOT + path
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_API_VERSION,
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise NotionError(f"Notion {method} {path} → HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise NotionError(f"Notion {method} {path} request failed: {exc}") from exc


def _markdown_to_blocks(markdown: str) -> list[dict[str, Any]]:
    """Convert markdown to Notion blocks (line-by-line, paragraph-only).

    V1 deliberately naive — paragraph blocks for every non-empty line. Sufficient
    for briefings + Pacman proposals, which read fine as flat prose. Heading and
    list parsing is a follow-up; the current <v1-reference-install> briefings already render as
    plain prose in SiYuan, so feature-parity is preserved.
    """
    blocks: list[dict[str, Any]] = []
    for line in markdown.splitlines():
        text = line.rstrip()
        if not text:
            continue
        blocks.append(
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": text[:2000]}}],
                },
            }
        )
    return blocks


def _blocks_to_markdown(blocks: list[dict[str, Any]]) -> str:
    """Reconstruct markdown from Notion block children. Inverse of `_markdown_to_blocks` for paragraphs."""
    lines: list[str] = []
    for block in blocks:
        block_type = block.get("type")
        if block_type == "paragraph":
            rich_text = block.get("paragraph", {}).get("rich_text", [])
            lines.append("".join(part.get("plain_text", "") for part in rich_text))
        elif block_type in ("heading_1", "heading_2", "heading_3"):
            level = int(block_type[-1])
            rich_text = block.get(block_type, {}).get("rich_text", [])
            text = "".join(part.get("plain_text", "") for part in rich_text)
            lines.append(f"{'#' * level} {text}")
        elif block_type in ("bulleted_list_item", "numbered_list_item"):
            rich_text = block.get(block_type, {}).get("rich_text", [])
            text = "".join(part.get("plain_text", "") for part in rich_text)
            prefix = "- " if block_type == "bulleted_list_item" else "1. "
            lines.append(f"{prefix}{text}")
        else:
            for value in block.get(block_type or "", {}).values():
                if isinstance(value, list):
                    lines.append(
                        "".join(part.get("plain_text", "") for part in value if isinstance(part, dict))
                    )
    return "\n".join(lines)


class NotionNotes(NotesAdapter):
    def __init__(self, token: str, notes_root: str) -> None:
        self._token = token
        self._notes_root = notes_root

    def create_doc(self, parent: str, title: str, body: str) -> str:
        parent_id = parent or self._notes_root
        result = _request(
            self._token,
            "POST",
            "/pages",
            {
                "parent": {"page_id": parent_id},
                "properties": {
                    "title": [{"type": "text", "text": {"content": title[:200]}}],
                },
                "children": _markdown_to_blocks(body),
            },
        )
        return result.get("id", "")

    def append_to_doc(self, doc_id: str, content: str) -> None:
        _request(
            self._token,
            "PATCH",
            f"/blocks/{doc_id}/children",
            {"children": _markdown_to_blocks(content)},
        )

    def read_doc(self, doc_id: str) -> str:
        children = _request(self._token, "GET", f"/blocks/{doc_id}/children?page_size=100")
        return _blocks_to_markdown(children.get("results", []))

    def get_deeplink(self, doc_id: str) -> str:
        return f"https://www.notion.so/{doc_id.replace('-', '')}"

    def link_blocks(self, from_id: str, to_id: str) -> None:
        _request(
            self._token,
            "PATCH",
            f"/blocks/{from_id}/children",
            {
                "children": [
                    {
                        "object": "block",
                        "type": "link_to_page",
                        "link_to_page": {"type": "page_id", "page_id": to_id},
                    }
                ]
            },
        )

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        result = _request(
            self._token,
            "POST",
            "/search",
            {"query": query, "page_size": limit, "filter": {"value": "page", "property": "object"}},
        )
        hits: list[SearchHit] = []
        for page in result.get("results", [])[:limit]:
            page_id = page.get("id", "")
            title = _extract_page_title(page)
            hits.append(
                SearchHit(
                    id=page_id,
                    title=title,
                    snippet="",
                    deeplink=f"https://www.notion.so/{page_id.replace('-', '')}",
                    raw=page,
                )
            )
        return hits


def _extract_page_title(page: dict[str, Any]) -> str:
    properties = page.get("properties", {})
    for prop in properties.values():
        if prop.get("type") == "title":
            rich_text = prop.get("title", [])
            return "".join(part.get("plain_text", "") for part in rich_text)
    return "(untitled)"


class NotionDatabase(DatabaseAdapter):
    def __init__(
        self,
        token: str,
        databases: dict[str, str],
        natural_keys: dict[str, str],
        active_database: str | None = None,
    ) -> None:
        self._token = token
        self._databases = databases
        self._natural_keys = natural_keys
        self._active = active_database

    def _resolve_database(self, properties: dict[str, Any] | None) -> str:
        name = (properties or {}).pop("_database", None) if properties else None
        chosen = name or self._active
        if not chosen:
            raise ValueError(
                "NotionDatabase: no active database set and properties._database not provided"
            )
        if chosen not in self._databases:
            raise KeyError(
                f"NotionDatabase: unknown database name {chosen!r}. "
                f"Configured: {sorted(self._databases)}"
            )
        return self._databases[chosen]

    def query(self, filters: dict[str, Any] | None = None, limit: int = 50) -> list[SearchHit]:
        db_id = self._resolve_database(filters)
        payload: dict[str, Any] = {"page_size": min(limit, 100)}
        if filters:
            and_clauses = [
                {"property": key, "rich_text": {"equals": str(value)}}
                for key, value in filters.items()
            ]
            if and_clauses:
                payload["filter"] = {"and": and_clauses} if len(and_clauses) > 1 else and_clauses[0]
        result = _request(self._token, "POST", f"/databases/{db_id}/query", payload)
        hits: list[SearchHit] = []
        for row in result.get("results", []):
            row_id = row.get("id", "")
            hits.append(
                SearchHit(
                    id=row_id,
                    title=_extract_page_title(row),
                    snippet="",
                    deeplink=f"https://www.notion.so/{row_id.replace('-', '')}",
                    raw=row,
                )
            )
        return hits

    def upsert_row(self, properties: dict[str, Any]) -> str:
        properties = dict(properties)
        db_id = self._resolve_database(properties)
        friendly_name = next(
            (name for name, uuid in self._databases.items() if uuid == db_id),
            None,
        )
        natural_key = self._natural_keys.get(friendly_name or "")
        if not natural_key or natural_key not in properties:
            return self._create_row(db_id, properties)
        existing = self.query(
            filters={natural_key: properties[natural_key], "_database": friendly_name},
            limit=1,
        )
        if existing:
            for key, value in properties.items():
                self.update_property(existing[0].id, key, value)
            return existing[0].id
        return self._create_row(db_id, properties)

    def _create_row(self, db_id: str, properties: dict[str, Any]) -> str:
        result = _request(
            self._token,
            "POST",
            "/pages",
            {
                "parent": {"database_id": db_id},
                "properties": _properties_to_notion(properties),
            },
        )
        return result.get("id", "")

    def update_property(self, row_id: str, key: str, value: Any) -> None:
        _request(
            self._token,
            "PATCH",
            f"/pages/{row_id}",
            {"properties": _properties_to_notion({key: value})},
        )


def _properties_to_notion(properties: dict[str, Any]) -> dict[str, Any]:
    """Convert flat key/value dict to Notion's property shape.

    V1 strategy: infer property type from value Python type. Strings → rich_text;
    ints/floats → number; bools → checkbox; lists → multi_select. Notion's
    actual schema lookup (which property is `title`, etc.) is deferred — callers
    that need exact schema fidelity can pass already-shaped Notion property dicts
    via the `_raw_properties` key, which is passed through verbatim.
    """
    if "_raw_properties" in properties:
        return properties["_raw_properties"]  # type: ignore[no-any-return]
    out: dict[str, Any] = {}
    for key, value in properties.items():
        if key.startswith("_"):
            continue
        if isinstance(value, bool):
            out[key] = {"checkbox": value}
        elif isinstance(value, (int, float)):
            out[key] = {"number": value}
        elif isinstance(value, list):
            out[key] = {"multi_select": [{"name": str(item)[:100]} for item in value]}
        else:
            out[key] = {"rich_text": [{"type": "text", "text": {"content": str(value)[:2000]}}]}
    return out


class NotionAdapter(SecondBrainAdapter):
    backend = "notion"

    def __init__(
        self,
        token: str,
        notes_root: str,
        databases: dict[str, str] | None = None,
        natural_keys: dict[str, str] | None = None,
        active_database: str | None = None,
    ) -> None:
        self.notes = NotionNotes(token, notes_root)
        self.database = NotionDatabase(
            token=token,
            databases=databases or {},
            natural_keys=natural_keys or {},
            active_database=active_database,
        )
