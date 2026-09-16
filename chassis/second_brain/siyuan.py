"""SiYuan adapter — block-based notes via SiYuan's HTTP kernel API.

Ports the call patterns the V1 <v1-reference-install> instance uses (briefing-siyuan-crosslink.py,
generate-dossier.py, pacman-queue-add.py). Every operation hits the local SiYuan
kernel, typically reverse-proxied through a per-install host for phone-clickable
deeplinks (see SIYUAN_DEEPLINK_BASE below - the host is never hardcoded here).

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
        deeplink_template: siyuan://blocks/     # default: env SIYUAN_DEEPLINK_BASE, else this

`deeplink_template` is a PREFIX - a block id is appended verbatim, so it keeps its
trailing separator. The `siyuan://blocks/` default opens the SiYuan DESKTOP APP and
does NOT open on a phone. Installs that need mobile-clickable links set
SIYUAN_DEEPLINK_BASE in .env to their web-UI prefix
(https://<siyuan-host>:6806/stage/build/desktop/?id=). The host is per-install and
it moves, so it lives in .env, never in code.
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
    """Raised when SiYuan API returns a non-zero `code`, or refuses a SQL query."""


# list_children asks SQL for a SUPERSET (SiYuan's LIKE is case-insensitive and
# cannot escape `_`/`%`), then re-checks the prefix in Python. So the SQL row
# cap has to be generous enough that the exact matches are never crowded out by
# near-misses. 2000 first-level docs under one container is already far past
# anything a human maintains by hand.
_CHILD_SCAN_CAP = 2000


def _siyuan_stamp(value: datetime) -> str:
    """Format a datetime as SiYuan's `YYYYMMDDHHMMSS` block-timestamp string.

    Aware datetimes are converted to this process's local timezone (SiYuan
    stores kernel-local wall-clock time); naive datetimes pass through as-is.
    """
    if value.tzinfo is not None:
        value = value.astimezone()
    return value.strftime("%Y%m%d%H%M%S")


def _is_direct_child(hpath: str, prefix: str) -> bool:
    """True when `hpath` sits exactly one level below `prefix`.

    The exactness SQL cannot give us: a case-sensitive prefix test, and no
    further `/` in what remains. See `SiYuanNotes.list_children` for why LIKE
    alone is not enough.
    """
    if not hpath.startswith(prefix):
        return False
    remainder = hpath[len(prefix) :]
    return bool(remainder) and "/" not in remainder


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

    def _query_sql(self, stmt: str) -> list[dict[str, Any]]:
        """Run a SQL statement against /api/query/sql. Raise on a refused query.

        SiYuan answers a query it will not run with `{"code": 0, "msg": "",
        "data": null}` - success-shaped, but null. The old `result if
        isinstance(result, list) else []` at each call site turned that into an
        empty result set, so an unsupported statement looked exactly like "no
        matches". That is how an ESCAPE clause SiYuan does not accept made
        search() silently return zero hits against a kernel holding 397.

        A genuinely empty result comes back as `[]`, not null (verified against
        a live kernel), so treating null as an error is safe. Only the SQL path
        is hardened - other endpoints return null legitimately (appendBlock).
        """
        result = self._post("/api/query/sql", {"stmt": stmt})
        if result is None:
            raise SiYuanError(
                "SiYuan refused the SQL query (code=0 but data=null - it answers a "
                "statement it will not run this way, and an empty result set would "
                f"have been []). Statement: {stmt[:300]!r}"
            )
        if not isinstance(result, list):
            raise SiYuanError(
                f"SiYuan /api/query/sql returned {type(result).__name__}, expected a "
                f"list of rows. Statement: {stmt[:300]!r}"
            )
        return result

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
        """Substring search over block content, newest first.

        WILDCARDS PASS THROUGH. `%` and `_` in `query` are live LIKE wildcards:
        a search for `50%` also matches `50 percent`, and `a_b` matches `axb`.
        This is a deliberate, known tradeoff, not an oversight.

        SiYuan's SQL endpoint does NOT accept an `ESCAPE` clause - verified
        against a live kernel, where `LIKE '%Vibecode%' ESCAPE '\\'` returns
        `data: null` (zero rows) for any escape character, while the same query
        without it returns 397 rows. So the wildcards cannot be escaped, and the
        query is passed as-is.

        This is safe: the single-quote escaping in `_escape` is what prevents
        injection (the query can never break out of the string literal), and the
        result set is LIMIT-capped. The only cost is that a query containing a
        wildcard matches more broadly than the caller may have intended.
        """
        sql = (
            "SELECT id, content, hpath FROM blocks "
            f"WHERE content LIKE '%{self._escape(query)}%' "
            f"ORDER BY updated DESC LIMIT {int(limit)}"
        )
        rows = self._query_sql(sql)
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

    def list_children(self, parent: str, limit: int = 200) -> list[SearchHit]:
        """Docs directly under `parent`, ascending by hpath, code-point order.

        `parent` is an hpath (`/1 Projects`), a doc block id, or `""`/`"/"` for
        the notebook root - the same forms `create_doc` accepts. It is resolved
        first, and a parent that does not exist raises rather than returning an
        empty list, so a typo cannot masquerade as an empty container.

        Scoped to ONE notebook, unlike `search` and `list_recent`. hpath is only
        unique within a notebook (`/1 Projects` can exist in several), so a
        container-relative query has to choose, and the only defensible choice
        is the notebook the parent itself lives in. For the root the notebook is
        the adapter's configured `notebook_id`; without one, every notebook has
        a root and merging them would silently return a union nobody asked for,
        so that raises too.

        The `hpath LIKE` prefix is re-checked in Python, and this is load-
        bearing rather than belt-and-braces:

          - SiYuan's LIKE is sqlite's, which is ASCII case-insensitive, so
            `/3 resources/%` matches `/3 Resources/Foo`.
          - `_` and `%` inside the parent path are live wildcards. A container
            named `_MAP` would otherwise match `XMAP` as well, and `_MAP` is a
            real doc name in the drift-detection use case this method exists
            for. ESCAPE is not available - SiYuan answers any statement
            carrying an ESCAPE clause with `data: null`, verified against a
            live kernel; see `search` for the same constraint.

        Grandchildren are excluded twice over for the same reason: `NOT LIKE
        '<prefix>%/%'` narrows the scan, and the Python check on the remaining
        segment is what actually guarantees it.
        """
        hpath, box = self._parent_container(parent)
        prefix = hpath.rstrip("/") + "/"
        escaped = self._escape(prefix)
        sql = (
            "SELECT id, hpath, content, updated, created FROM blocks "
            "WHERE type = 'd' "
            f"AND box = '{self._escape(box)}' "
            f"AND hpath LIKE '{escaped}%' "
            f"AND hpath NOT LIKE '{escaped}%/%' "
            "ORDER BY hpath ASC, id ASC "
            f"LIMIT {_CHILD_SCAN_CAP}"
        )
        rows = [
            row
            for row in self._query_sql(sql)
            if _is_direct_child(row.get("hpath") or "", prefix)
        ]
        # Re-sorted in Python rather than trusting the ORDER BY: the documented
        # contract is code-point order, and a column collation is a property of
        # SiYuan's schema, not something this adapter controls.
        rows.sort(key=lambda row: ((row.get("hpath") or ""), row.get("id") or ""))
        return [
            SearchHit(
                id=row.get("id", ""),
                title=(row.get("hpath") or "").rsplit("/", 1)[-1]
                or (row.get("content") or "(untitled)"),
                snippet=(row.get("content") or "")[:200],
                deeplink=self.get_deeplink(row.get("id", "")),
                raw=row,
            )
            for row in rows[: int(limit)]
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
        rows = self._query_sql(sql)
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
        rows = self._query_sql(sql)
        return rows[0].get("hpath", "/") if rows else "/"

    def _parent_container(self, parent: str) -> tuple[str, str]:
        """Resolve a `list_children` parent to its `(hpath, notebook_id)` pair.

        Separate from `_block_to_hpath`, which answers `/` when the block is
        missing. That fallback is harmless where it is used (create_doc lands
        the doc at the notebook root) and actively wrong here, where it would
        turn "no such container" into "here is the whole notebook".

        The two parent forms are scoped differently on purpose, and the
        asymmetry is worth knowing about:

          - An HPATH is only unique within a notebook, so it is resolved
            against the adapter's configured `notebook_id`. A `/1 Projects`
            that exists only in some OTHER notebook raises "no doc at hpath"
            rather than silently listing a container the adapter never writes
            to.
          - A BLOCK ID is globally unique, so it is resolved without a
            notebook filter, and its children come from whichever notebook it
            turns out to live in. A caller holding an id already knows exactly
            which doc it means; second-guessing that against config would only
            reject a question that had one correct answer.
        """
        raw = (parent or "").strip()
        if raw in ("", "/"):
            if not self._notebook_id:
                raise SiYuanError(
                    "list_children('/') needs a notebook to scope to, and this "
                    "adapter has no notebook_id. Every notebook has a root, so "
                    "there is no single correct answer - set "
                    "second_brain.databases.notes_root (or "
                    "second_brain.siyuan.notebook_id) in chassis.config.yaml."
                )
            return "/", self._notebook_id
        if raw.startswith("/"):
            hpath = raw.rstrip("/") or "/"
            box_clause = (
                f" AND box = '{self._escape(self._notebook_id)}'" if self._notebook_id else ""
            )
            sql = (
                "SELECT id, hpath, box FROM blocks "
                f"WHERE type = 'd' AND hpath = '{self._escape(hpath)}'{box_clause} "
                "LIMIT 1"
            )
            rows = self._query_sql(sql)
            if not rows:
                raise SiYuanError(
                    f"list_children: no doc at hpath {hpath!r}"
                    + (f" in notebook {self._notebook_id!r}" if self._notebook_id else "")
                    + ". Note the SQL index is eventually consistent, so a doc "
                    "created seconds ago may not be queryable yet - see "
                    "docs/second-brain-adapters.md."
                )
            return rows[0].get("hpath") or hpath, rows[0].get("box") or self._notebook_id
        sql = (
            "SELECT id, hpath, box FROM blocks "
            f"WHERE type = 'd' AND id = '{self._escape(raw)}' LIMIT 1"
        )
        rows = self._query_sql(sql)
        if not rows:
            raise SiYuanError(
                f"list_children: no doc block with id {raw!r}. A parent that is "
                "not an hpath is read as a block id; ids that exist but are not "
                "type='d' (a paragraph, say) do not have children in the "
                "document sense and are rejected here too."
            )
        return rows[0].get("hpath") or "/", rows[0].get("box") or self._notebook_id

    def _block_title(self, block_id: str) -> str:
        sql = f"SELECT content FROM blocks WHERE id = '{self._escape(block_id)}' LIMIT 1"
        rows = self._query_sql(sql)
        return rows[0].get("content", "") if rows else ""

    @staticmethod
    def _escape(value: str) -> str:
        # SiYuan SQL is sqlite. Escape for a single-quoted string literal. This
        # is the whole injection defense and it is sufficient: a value can never
        # terminate the literal it sits in. LIKE wildcards inside `value` stay
        # live - see search() for why they cannot be escaped on this backend.
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
