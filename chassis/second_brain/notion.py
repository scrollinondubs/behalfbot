"""Notion adapter — page+block model via Notion's REST API.

Implements both surfaces:

- `notes` over the Pages + Blocks endpoints (free-form prose pages with
  appendable block children).
- `database` over the Databases endpoint (structured rows with property schemas).

Per-installer config (chassis.config.yaml):

    second_brain:
      backend: notion
      notion:
        token: ${NOTION_API_TOKEN}        # from .env / Vaultwarden
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

import email.utils
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from chassis.second_brain.base import (
    DatabaseAdapter,
    NotesAdapter,
    SearchHit,
    SecondBrainAdapter,
)

NOTION_API_VERSION = "2022-06-28"
NOTION_API_ROOT = "https://api.notion.com/v1"

# list_recent scans the /search endpoint newest-first until it walks past the
# window. Cap the scan so a huge workspace cannot turn one call into an
# unbounded crawl - 500 pages (5 API calls) covers any sane daily window.
_LIST_RECENT_SCAN_CAP = 500

# Notion API hard ceilings (https://developers.notion.com/reference/request-limits):
# at most 100 block children per create/append request, and at most 2000
# characters per rich_text content field. Exceeding either is an opaque 400.
_MAX_BLOCKS_PER_REQUEST = 100
_MAX_RICH_TEXT_CHARS = 2000
# A block also caps its rich_text array at 100 elements, so one paragraph tops
# out at 200_000 chars. Lines beyond that are truncated - a single 200k-char
# line is a pathological input, not a briefing.
_MAX_RICH_TEXT_PARTS = 100

# 429 handling: Notion averages ~3 requests/second per integration. Retries
# are bounded and each sleep is capped so a hostile Retry-After header cannot
# park the process for minutes.
_MAX_RATE_LIMIT_RETRIES = 5
_MAX_RETRY_AFTER_SECONDS = 30.0


def _to_utc(value: datetime) -> datetime:
    """Normalize to aware-UTC. Naive datetimes are interpreted as local time
    (that is `datetime.astimezone`'s behavior for naive input)."""
    return value.astimezone(timezone.utc)


def _parse_notion_ts(raw: str | None) -> datetime | None:
    """Parse Notion's ISO-8601 timestamps (e.g. `2026-07-09T10:00:00.000Z`)."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


class NotionError(RuntimeError):
    """Raised when Notion API returns a non-2xx response."""


class NotionPartialWriteError(NotionError):
    """A chunked write failed after some chunks already landed.

    Chunked create/append is NOT atomic - Notion has no batch-write
    transaction, so a failure partway through leaves a half-written page.
    We deliberately leave the partial content in place rather than trying to
    roll it back (a rollback delete can itself fail, and for append_to_doc the
    adapter cannot tell its own blocks from pre-existing ones). The error
    carries enough state for a non-blind retry: the page id and how many
    chunks landed out of how many.
    """

    def __init__(self, doc_id: str, chunks_written: int, chunks_total: int, cause: str) -> None:
        self.doc_id = doc_id
        self.chunks_written = chunks_written
        self.chunks_total = chunks_total
        super().__init__(
            f"Notion chunked write to {doc_id} landed {chunks_written} of "
            f"{chunks_total} chunks (up to {_MAX_BLOCKS_PER_REQUEST} blocks each) "
            f"before failing: {cause}. The page holds the first {chunks_written} "
            f"chunks - retry by appending only the remaining content, or delete "
            f"the page and re-create it. Do not blindly re-send the full body: "
            f"that duplicates what already landed."
        )


def _retry_after_seconds(exc: urllib.error.HTTPError, attempt: int) -> float:
    """Delay before retrying a 429. Honours Retry-After (seconds or HTTP-date),
    falls back to linear backoff, and is always capped."""
    raw = (exc.headers.get("Retry-After") or "").strip() if exc.headers else ""
    delay: float | None = None
    if raw:
        try:
            delay = float(raw)
        except ValueError:
            try:
                parsed = email.utils.parsedate_to_datetime(raw)
                delay = (parsed - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError):
                delay = None
    if delay is None or delay < 0:
        delay = float(attempt + 1)
    return min(delay, _MAX_RETRY_AFTER_SECONDS)


def _request(token: str, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """One Notion API call, with bounded retry on 429 only.

    Retrying a rate-limited request is safe even for non-idempotent writes:
    a 429 means Notion's limiter rejected the request BEFORE executing it, so
    a retried append cannot double-write. Network errors and 5xx responses are
    deliberately NOT retried - there the request may have been applied before
    the failure surfaced, and a blind retry of an append double-writes.
    """
    url = NOTION_API_ROOT + path
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
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
            if exc.code == 429:
                if attempt < _MAX_RATE_LIMIT_RETRIES:
                    time.sleep(_retry_after_seconds(exc, attempt))
                    continue
                raise NotionError(
                    f"Notion {method} {path} → HTTP 429 (rate limited) after "
                    f"{_MAX_RATE_LIMIT_RETRIES + 1} attempts, giving up: {detail}"
                ) from exc
            raise NotionError(f"Notion {method} {path} → HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise NotionError(f"Notion {method} {path} request failed: {exc}") from exc
    raise NotionError(f"Notion {method} {path}: retry loop exited without a response")


def _markdown_to_blocks(markdown: str) -> list[dict[str, Any]]:
    """Convert markdown to Notion blocks (line-by-line, paragraph-only).

    V1 deliberately naive — paragraph blocks for every non-empty line. Sufficient
    for briefings + Pacman proposals, which read fine as flat prose. Heading and
    list parsing is a follow-up; the current <v1-reference-install> briefings already render as
    plain prose in SiYuan, so feature-parity is preserved.

    Lines over 2000 chars are split across multiple rich_text parts within the
    same paragraph (Notion's per-field ceiling) rather than truncated.
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
                    "rich_text": _split_rich_text(text),
                },
            }
        )
    return blocks


def _split_rich_text(text: str) -> list[dict[str, Any]]:
    """Split one line into rich_text parts of at most 2000 chars each.

    Notion renders consecutive parts inside one paragraph with no visible
    seam, so a long line survives intact instead of being truncated at 2000.
    Capped at 100 parts (Notion's per-block rich_text ceiling); anything
    beyond 200k chars in a single line is dropped.
    """
    parts = [
        {"type": "text", "text": {"content": text[i : i + _MAX_RICH_TEXT_CHARS]}}
        for i in range(0, len(text), _MAX_RICH_TEXT_CHARS)
    ]
    return parts[:_MAX_RICH_TEXT_PARTS]


def _chunk_blocks(blocks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split a block list into request-sized chunks (at most 100 blocks each).

    Notion caps block children at 100 per create/append request; an unchunked
    briefing-sized document is an opaque HTTP 400.
    """
    return [
        blocks[i : i + _MAX_BLOCKS_PER_REQUEST]
        for i in range(0, len(blocks), _MAX_BLOCKS_PER_REQUEST)
    ]


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
        chunks = _chunk_blocks(_markdown_to_blocks(body))
        first = chunks[0] if chunks else []
        result = _request(
            self._token,
            "POST",
            "/pages",
            {
                "parent": {"page_id": parent_id},
                "properties": {
                    "title": [{"type": "text", "text": {"content": title[:200]}}],
                },
                "children": first,
            },
        )
        page_id = result.get("id", "")
        if len(chunks) > 1:
            # Not atomic: the page exists with chunk 1 already in it. A failure
            # below raises NotionPartialWriteError carrying the page id and the
            # count of landed chunks - see that class for the retry contract.
            self._append_chunks(page_id, chunks[1:], chunks_done=1, chunks_total=len(chunks))
        return page_id

    def append_to_doc(self, doc_id: str, content: str) -> None:
        chunks = _chunk_blocks(_markdown_to_blocks(content))
        if not chunks:
            return  # nothing but blank lines - Notion 400s on an empty children array
        self._append_chunks(doc_id, chunks, chunks_done=0, chunks_total=len(chunks))

    def _append_chunks(
        self,
        doc_id: str,
        chunks: list[list[dict[str, Any]]],
        chunks_done: int,
        chunks_total: int,
    ) -> None:
        """Append chunks sequentially (order matters - blocks land append-only).

        Sequential on purpose: parallel appends would interleave chunks and
        scramble the document. On failure, raise with exact progress so the
        caller's retry is not blind.
        """
        for offset, chunk in enumerate(chunks):
            try:
                _request(
                    self._token,
                    "PATCH",
                    f"/blocks/{doc_id}/children",
                    {"children": chunk},
                )
            except NotionError as exc:
                raise NotionPartialWriteError(
                    doc_id=doc_id,
                    chunks_written=chunks_done + offset,
                    chunks_total=chunks_total,
                    cause=str(exc),
                ) from exc

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

    def list_recent(
        self,
        since: datetime,
        until: datetime,
        min_content_len: int = 0,
        limit: int = 50,
    ) -> list[SearchHit]:
        """Pages with `last_edited_time` in [since, until), newest first.

        Honest divergences from the SiYuan implementation:
          - Notion's /search endpoint has NO timestamp filter, only a sort.
            This scans newest-first and windows client-side, stopping as soon
            as results predate `since` (or after `_LIST_RECENT_SCAN_CAP`
            pages, whichever comes first).
          - `last_edited_time` has MINUTE granularity - Notion truncates
            seconds. Edits within the same minute as a window boundary can
            fall on either side of it.
          - Only pages shared with the integration are visible at all.
          - `min_content_len` costs one extra API call per candidate page
            (Notion does not expose content length on search results); the
            length measured is the reconstructed-markdown length of the first
            100 blocks.
        """
        since_utc = _to_utc(since)
        until_utc = _to_utc(until)
        hits: list[SearchHit] = []
        cursor: str | None = None
        scanned = 0
        while scanned < _LIST_RECENT_SCAN_CAP:
            payload: dict[str, Any] = {
                "page_size": 100,
                "sort": {"direction": "descending", "timestamp": "last_edited_time"},
                "filter": {"value": "page", "property": "object"},
            }
            if cursor:
                payload["start_cursor"] = cursor
            result = _request(self._token, "POST", "/search", payload)
            for page in result.get("results", []):
                scanned += 1
                edited = _parse_notion_ts(page.get("last_edited_time"))
                if edited is None or edited >= until_utc:
                    continue
                if edited < since_utc:
                    return hits  # sorted descending - everything after is older
                if min_content_len > 0:
                    body = self.read_doc(page.get("id", ""))
                    if len(body) < min_content_len:
                        continue
                page_id = page.get("id", "")
                hits.append(
                    SearchHit(
                        id=page_id,
                        title=_extract_page_title(page),
                        snippet="",
                        deeplink=f"https://www.notion.so/{page_id.replace('-', '')}",
                        raw=page,
                    )
                )
                if len(hits) >= limit:
                    return hits
            if not result.get("has_more"):
                break
            cursor = result.get("next_cursor")
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
