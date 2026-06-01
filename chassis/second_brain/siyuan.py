"""SiYuan adapter — block-based notes via SiYuan's HTTP kernel API.

Ports the call patterns Sean's V1 <v1-reference-install> instance uses (briefing-siyuan-crosslink.py,
generate-dossier.py, pacman-queue-add.py). Every operation hits the local SiYuan
kernel, typically reverse-proxied through `s.grid7.com` for iPhone deeplinks.

Database surface is NOT implemented — SiYuan has SQL search but no native
property/database semantics that match Notion's. Use NotesAdapter only.

Config (chassis.config.yaml):

    second_brain:
      backend: siyuan
      siyuan:
        base_url: http://127.0.0.1:6806     # local kernel
        token: ${SIYUAN_TOKEN}               # from .env
        notebook_id: 20231101120000-abc123    # default notebook for create_doc
        deeplink_template: https://s.grid7.com/?id=
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from chassis.second_brain.base import (
    NotImplementedDatabase,
    NotesAdapter,
    SearchHit,
    SecondBrainAdapter,
)


class SiYuanError(RuntimeError):
    """Raised when SiYuan API returns a non-zero `code`."""


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
            f"WHERE content LIKE '%{self._escape(query)}%' "
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
        # SiYuan SQL is sqlite — naive single-quote escape is sufficient given the
        # adapter only takes input from chassis-internal callers (no user-supplied
        # SQL). Tighten if this surface widens.
        return value.replace("'", "''")


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
