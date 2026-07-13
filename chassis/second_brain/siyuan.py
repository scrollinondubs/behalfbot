"""SiYuan adapter — block-based notes via SiYuan's HTTP kernel API.

Ports the call patterns Sean's V1 <v1-reference-install> instance uses (briefing-siyuan-crosslink.py,
generate-dossier.py, pacman-queue-add.py). Every operation hits the local SiYuan
kernel, typically reverse-proxied through `s.grid7.com` for iPhone deeplinks.

Database surface is NOT implemented — SiYuan has SQL search but no native
property/database semantics that match Notion's. Use NotesAdapter only.

Credentials come from the chassis .env - SIYUAN_URL and SIYUAN_TOKEN, the same
two vars direct mode passes to the native siyuan MCP server. The factory reads
them; nothing has to be duplicated into YAML. `notebook_id` defaults to
`second_brain.databases.notes_root`, the canonical write target.

Every key below is an OPTIONAL override in chassis.config.yaml:

    second_brain:
      backend: siyuan
      siyuan:
        base_url: http://127.0.0.1:6806        # default; env SIYUAN_URL wins over this default
        token: ${SIYUAN_TOKEN}                  # default: env SIYUAN_TOKEN
        notebook_id: 20231101120000-abc123      # default: second_brain.databases.notes_root
        deeplink_template: siyuan://blocks/     # default; set to a reverse-proxy URL
                                                # (e.g. https://siyuan.example.com/?id=)
                                                # for phone-clickable links
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

from chassis.second_brain.base import (
    NotImplementedDatabase,
    NotesAdapter,
    SearchHit,
    SecondBrainAdapter,
)


class SiYuanError(RuntimeError):
    """Raised when SiYuan API returns a non-zero `code`."""


# Backslash is not special inside a sqlite string literal, so it is a safe
# LIKE escape character and needs no doubling in the emitted SQL.
LIKE_ESCAPE_CHAR = "\\"


def _siyuan_stamp(value: datetime) -> str:
    """Format a datetime as SiYuan's `YYYYMMDDHHMMSS` block-timestamp string.

    Aware datetimes are converted to this process's local timezone (SiYuan
    stores kernel-local wall-clock time); naive datetimes pass through as-is.
    """
    if value.tzinfo is not None:
        value = value.astimezone()
    return value.strftime("%Y%m%d%H%M%S")


class SiYuanNotes(NotesAdapter):
    def __init__(
        self,
        base_url: str,
        token: str,
        notebook_id: str,
        deeplink_template: str,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._notebook_id = notebook_id
        self._deeplink_template = deeplink_template

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        url = self._base_url + path
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Token {self._token}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise SiYuanError(f"SiYuan {path} request failed: {exc}") from exc
        if data.get("code") != 0:
            raise SiYuanError(f"SiYuan {path} returned code={data.get('code')}: {data.get('msg')!r}")
        return data.get("data")

    def create_doc(self, parent: str, title: str, body: str) -> str:
        # `parent` is interpreted as the SiYuan hpath (e.g. "/Briefings"). If it
        # looks like a block id, we resolve to its hpath via SQL.
        hpath = parent if parent.startswith("/") else self._block_to_hpath(parent)
        target_path = f"{hpath.rstrip('/')}/{title}"
        result = self._post(
            "/api/filetree/createDocWithMd",
            {
                "notebook": self._notebook_id,
                "path": target_path,
                "markdown": body,
            },
        )
        # createDocWithMd returns the new doc's root block id (string)
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            return result.get("id", "")
        return ""

    def append_to_doc(self, doc_id: str, content: str) -> None:
        self._post(
            "/api/block/appendBlock",
            {"dataType": "markdown", "data": content, "parentID": doc_id},
        )

    def read_doc(self, doc_id: str) -> str:
        result = self._post("/api/export/exportMdContent", {"id": doc_id})
        return result.get("content", "") if isinstance(result, dict) else ""

    def get_deeplink(self, doc_id: str) -> str:
        return f"{self._deeplink_template}{doc_id}"

    def link_blocks(self, from_id: str, to_id: str) -> None:
        # Append a markdown block-ref link; SiYuan renders ((id 'anchor')) as an
        # embedded reference. Use the to-block's title as the anchor when known.
        anchor = self._block_title(to_id) or to_id
        self.append_to_doc(from_id, f"(({to_id} '{anchor}'))")

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        sql = (
            "SELECT id, content, hpath FROM blocks "
            f"WHERE content LIKE '%{self._escape_like(query)}%' ESCAPE '{LIKE_ESCAPE_CHAR}' "
            f"ORDER BY updated DESC LIMIT {int(limit)}"
        )
        result = self._post("/api/query/sql", {"stmt": sql})
        rows = result if isinstance(result, list) else []
        return [
            SearchHit(
                id=row.get("id", ""),
                title=row.get("hpath", "").rsplit("/", 1)[-1] or "(untitled)",
                snippet=(row.get("content") or "")[:200],
                deeplink=self.get_deeplink(row.get("id", "")),
                raw=row,
            )
            for row in rows
        ]

    def list_recent(
        self,
        since: datetime,
        until: datetime,
        min_content_len: int = 0,
        limit: int = 50,
    ) -> list[SearchHit]:
        """Docs updated in [since, until), newest first, via SQL on the block table.

        Timestamps: SiYuan's `blocks.updated` column stores the KERNEL's local
        clock as a `YYYYMMDDHHMMSS` string. Naive datetimes are passed through
        as-is (assumed to be in the kernel's timezone); aware datetimes are
        converted to this process's local time first, which matches the kernel
        only when both run on the same host - the chassis default.

        Body length: the doc row's own `content` column holds the TITLE, not
        the body (verified against a live kernel - max LENGTH(content) over
        283 type='d' rows was 81). `min_content_len` therefore filters on
        SUM(LENGTH(content)) over the doc's child blocks via a correlated
        subquery.
        """
        since_stamp = _siyuan_stamp(since)
        until_stamp = _siyuan_stamp(until)
        body_len_sql = (
            "(SELECT COALESCE(SUM(LENGTH(b2.content)), 0) FROM blocks b2 "
            "WHERE b2.root_id = blocks.id AND b2.type != 'd')"
        )
        sql = (
            f"SELECT id, hpath, content, updated, created, {body_len_sql} AS body_len "
            "FROM blocks "
            "WHERE type = 'd' "
            f"AND updated >= '{since_stamp}' "
            f"AND updated < '{until_stamp}' "
            f"AND {body_len_sql} >= {int(min_content_len)} "
            "ORDER BY updated DESC "
            f"LIMIT {int(limit)}"
        )
        result = self._post("/api/query/sql", {"stmt": sql})
        rows = result if isinstance(result, list) else []
        return [
            SearchHit(
                id=row.get("id", ""),
                title=(row.get("hpath") or "").rsplit("/", 1)[-1]
                or (row.get("content") or "(untitled)"),
                snippet=(row.get("content") or "")[:200],
                deeplink=self.get_deeplink(row.get("id", "")),
                raw=row,
            )
            for row in rows
        ]

    def _block_to_hpath(self, block_id: str) -> str:
        sql = f"SELECT hpath FROM blocks WHERE id = '{self._escape(block_id)}' LIMIT 1"
        result = self._post("/api/query/sql", {"stmt": sql})
        rows = result if isinstance(result, list) else []
        return rows[0].get("hpath", "/") if rows else "/"

    def _block_title(self, block_id: str) -> str:
        sql = f"SELECT content FROM blocks WHERE id = '{self._escape(block_id)}' LIMIT 1"
        result = self._post("/api/query/sql", {"stmt": sql})
        rows = result if isinstance(result, list) else []
        return rows[0].get("content", "") if rows else ""

    @staticmethod
    def _escape(value: str) -> str:
        # SiYuan SQL is sqlite. Escape for a single-quoted string literal.
        return value.replace("'", "''")

    @classmethod
    def _escape_like(cls, value: str) -> str:
        # For values interpolated into a LIKE pattern. search() is exposed as an
        # MCP tool by second_brain/mcp_server.py, so `query` is arbitrary
        # model-supplied or user-supplied text - not the chassis-internal-only
        # surface this adapter originally had. A bare quote-escape leaves the
        # LIKE wildcards live: '%' and '_' in a query would silently widen the
        # match. Escape the escape char first, then the wildcards, then quote
        # for the literal. Callers MUST pair this with an ESCAPE clause.
        escaped = value.replace(LIKE_ESCAPE_CHAR, LIKE_ESCAPE_CHAR * 2)
        for wildcard in ("%", "_"):
            escaped = escaped.replace(wildcard, LIKE_ESCAPE_CHAR + wildcard)
        return cls._escape(escaped)


class SiYuanAdapter(SecondBrainAdapter):
    backend = "siyuan"

    def __init__(
        self,
        base_url: str,
        token: str,
        notebook_id: str,
        deeplink_template: str = "siyuan://blocks/",
    ) -> None:
        self.notes = SiYuanNotes(base_url, token, notebook_id, deeplink_template)
        self.database = NotImplementedDatabase("siyuan")
