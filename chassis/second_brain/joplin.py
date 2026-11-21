"""Joplin adapter - notes and notebooks over the Joplin Data API.

Joplin exposes a local REST API (the "Data API", served by the Web Clipper
service) at http://localhost:41184 by default. This adapter talks to it
directly with the standard library; it does not go through any MCP server.

Where the API comes from - the headless caveat
==============================================
The Data API is not a cloud service. Something on the install has to be
serving it, and there are exactly two options:

  - Joplin DESKTOP, with Tools > Options > Web Clipper > "Enable Web Clipper
    Service" switched on. The app must be running, which on a Mac means a
    logged-in GUI session. Token: same screen, "Advanced options".
  - The Joplin TERMINAL client (npm `joplin`), started with
    `joplin --profile <dir> server start`. Headless, no GUI. Joplin labels
    this "an experimental feature" and recommends a profile no other client
    opens at the same time. Token: `api.token` in that profile.

Both bind 127.0.0.1 only. Joplin Server and Joplin Cloud (the SYNC targets)
do not serve this API at all - they are reached by the client, never by us.
From inside the chassis container the API is on the HOST: set
JOPLIN_HOST=host.docker.internal. Verified on Colima (macOS), which forwards
that name to the host loopback. Plain Docker on Linux does NOT forward to a
loopback-bound service; run the terminal client inside the container or put
a relay in front of it.

Data model mapping
==================
  - A note is a doc. Its id is a 32-char hex string. Treat it as opaque.
  - A notebook (Joplin "folder") is a CONTAINER, not a doc. Notes cannot hold
    notes. PARA maps to notebooks: `Projects`, `Areas`, ... are notebooks and
    the docs live inside them.
  - `parent` (create_doc / list_children) is a notebook: either its 32-hex id,
    or a `/`-separated title path from the top of the notebook tree, like
    `Projects` or `/Projects/Client X`. `""` means the configured root
    notebook. `create_doc` creates missing notebooks on the path, the same
    way Obsidian creates missing directories.
  - Tags exist in Joplin but have no slot in the NotesAdapter contract, so
    this adapter does not touch them.

Search does NOT use Joplin's /search endpoint
=============================================
Joplin's search index is built asynchronously by the desktop app, so a note
created a second ago is not yet searchable - and under the headless terminal
client's `server start` the index was never built at all (observed: zero
hits after two minutes for a title that existed). The seeder's idempotency
check searches for a sentinel it may have written moments earlier, so an
index-backed search would make re-runs duplicate everything. `search` scans
notes client-side instead (case-insensitive substring over title and body,
like the Obsidian adapter). It reads every note over the local API, which is
fine for a personal notebook and bounded by `_SCAN_CAP`.

Security note
=============
The Data API takes its token as a `token=` QUERY parameter, so the request
URL carries the secret. Every error message is built from the path, never
the URL, and any echo of the token in a response body is scrubbed.

Database surface is NOT implemented - Joplin has no structured rows.

Config (chassis.config.yaml; credentials come from .env):

    second_brain:
      backend: joplin
      databases:
        notes_root: "Second Brain"   # root notebook: id or title path
      joplin:                         # optional overrides only
        base_url: http://localhost:41184     # default: http://$JOPLIN_HOST:$JOPLIN_PORT
        token: ${JOPLIN_TOKEN}               # default: env JOPLIN_TOKEN
        notebook: "Second Brain"             # default: databases.notes_root
        deeplink_template: joplin://x-callback-url/openNote?id=
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

from chassis.second_brain.base import (
    NotImplementedDatabase,
    NotesAdapter,
    SearchHit,
    SecondBrainAdapter,
)

DEFAULT_BASE_URL = "http://localhost:41184"
# Opens the note in Joplin DESKTOP. There is no web UI to link to, and the
# mobile apps do not register this scheme.
DEFAULT_DEEPLINK_TEMPLATE = "joplin://x-callback-url/openNote?id="

_ID_PAT = re.compile(r"^[0-9a-f]{32}$")
_PAGE_LIMIT = 100  # the API's maximum page size
_SCAN_CAP = 20000  # notes read by one search / list_recent call, at most
_SNIPPET_LEN = 200
_HTTP_TIMEOUT = 30


class JoplinError(RuntimeError):
    """Raised on Data API failures - missing notes, bad ids, HTTP errors."""


class JoplinNotFoundError(JoplinError):
    """The API answered 404."""


def _scrub(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


def _request(
    base_url: str,
    token: str,
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> Any:
    """One Data API call. Returns the decoded JSON body, or None when empty.

    The token rides in the query string because that is the only auth the
    Data API accepts. Error text is built from `path` alone so the token
    never lands in an exception, a log line or a transcript.
    """
    query = dict(params or {})
    query["token"] = token
    url = f"{base_url.rstrip('/')}{path}?{urllib.parse.urlencode(query)}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = _scrub(exc.read().decode("utf-8", errors="replace"), token)
        # The API's error body is a JSON object whose `error` carries a JS
        # stack trace; the first line is the useful part.
        try:
            detail = str(json.loads(detail).get("error", detail))
        except (ValueError, AttributeError):
            pass
        detail = detail.strip().splitlines()[0][:300] if detail.strip() else ""
        error_cls = JoplinNotFoundError if exc.code == 404 else JoplinError
        hint = " (wrong JOPLIN_TOKEN?)" if exc.code == 403 else ""
        raise error_cls(f"Joplin {method} {path} -> HTTP {exc.code}{hint}: {detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = _scrub(str(getattr(exc, "reason", exc)), token)
        raise JoplinError(
            f"Joplin {method} {path} request failed: {reason}. Is the Joplin Data API "
            f"running at {base_url}? (desktop Web Clipper service, or "
            f"`joplin server start`)"
        ) from None
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise JoplinError(f"Joplin {method} {path} returned non-JSON") from exc


def _check_id(kind: str, value: str) -> str:
    ident = (value or "").strip()
    if not _ID_PAT.match(ident):
        raise JoplinError(
            f"{kind} {value!r} is not a Joplin id (32 lowercase hex chars). "
            f"Pass back an id a previous call returned."
        )
    return ident


def _ms_to_iso(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _snippet(body: str, index: int = -1) -> str:
    if index >= 0:
        start = max(0, index - 60)
        return body[start : start + _SNIPPET_LEN].strip()
    return body[:_SNIPPET_LEN].strip()


class JoplinNotes(NotesAdapter):
    def __init__(
        self,
        base_url: str,
        token: str,
        root_notebook: str,
        deeplink_template: str = DEFAULT_DEEPLINK_TEMPLATE,
    ) -> None:
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._token = token
        self._root = (root_notebook or "").strip()
        self._deeplink_template = deeplink_template or DEFAULT_DEEPLINK_TEMPLATE

    # -- transport -----------------------------------------------------------

    def _call(self, method: str, path: str, params: dict[str, Any] | None = None,
              payload: dict[str, Any] | None = None) -> Any:
        return _request(self._base_url, self._token, method, path, params, payload)

    def _paginate(self, path: str, params: dict[str, Any] | None = None,
                  cap: int = _SCAN_CAP, stop=None) -> list[dict[str, Any]]:
        """Every item of a paginated listing, up to `cap`.

        `stop(item)` returning True ends the walk early - list_recent uses it
        to stop once a newest-first listing passes the window.
        """
        items: list[dict[str, Any]] = []
        page = 1
        while len(items) < cap:
            query = dict(params or {})
            query.update({"page": page, "limit": _PAGE_LIMIT})
            result = self._call("GET", path, query) or {}
            batch = result.get("items") or []
            for item in batch:
                if item.get("deleted_time"):
                    continue
                if stop is not None and stop(item):
                    return items
                items.append(item)
            if not result.get("has_more") or not batch:
                break
            page += 1
        return items[:cap]

    # -- notebooks -----------------------------------------------------------

    def _folders(self) -> list[dict[str, Any]]:
        return self._paginate("/folders", {"fields": "id,title,parent_id,deleted_time"})

    def _folder_paths(self) -> dict[str, str]:
        """Folder id -> `/A/B` title path, for hpath-style display."""
        folders = {f["id"]: f for f in self._folders()}
        paths: dict[str, str] = {}

        def path_of(fid: str, depth: int = 0) -> str:
            if fid in paths:
                return paths[fid]
            folder = folders.get(fid)
            if folder is None or depth > 50:
                return ""
            parent = folder.get("parent_id") or ""
            prefix = path_of(parent, depth + 1) if parent else ""
            paths[fid] = f"{prefix}/{folder.get('title', '')}"
            return paths[fid]

        for fid in folders:
            path_of(fid)
        return paths

    def _resolve_notebook(self, ref: str, create: bool) -> str:
        """Notebook id for a 32-hex id or a `/`-separated title path.

        `""` resolves to the configured root notebook. A missing notebook is
        created when `create` is true (create_doc) and raises otherwise
        (list_children), which keeps "empty" and "not there" distinct. Two
        sibling notebooks with the same title are refused rather than guessed
        between - Joplin allows it, and a wrong guess writes into the wrong
        place silently.
        """
        raw = (ref or "").strip()
        if not raw:
            raw = self._root
            if not raw:
                raise JoplinError(
                    "No root notebook configured. Set second_brain.databases.notes_root "
                    "(or second_brain.joplin.notebook) to a notebook id or title path."
                )
        if _ID_PAT.match(raw):
            try:
                self._call("GET", f"/folders/{raw}", {"fields": "id"})
            except JoplinNotFoundError:
                raise JoplinError(f"Joplin notebook {raw!r} not found.") from None
            return raw
        segments = [seg.strip() for seg in raw.strip("/").split("/") if seg.strip()]
        if not segments:
            raise JoplinError(f"Notebook path {ref!r} is empty.")
        folders = self._folders()
        parent = ""
        walked: list[str] = []
        for seg in segments:
            walked.append(seg)
            matches = [
                f for f in folders
                if (f.get("parent_id") or "") == parent and f.get("title") == seg
            ]
            if len(matches) > 1:
                raise JoplinError(
                    f"Notebook path '/{'/'.join(walked)}' is ambiguous: "
                    f"{len(matches)} sibling notebooks share that title. Rename one "
                    f"in Joplin, or pass the notebook id instead."
                )
            if matches:
                parent = matches[0]["id"]
                continue
            if not create:
                raise JoplinError(f"Joplin notebook '/{'/'.join(walked)}' not found.")
            made = self._call("POST", "/folders", payload={"title": seg, "parent_id": parent}) or {}
            if not made.get("id"):
                raise JoplinError(f"Joplin did not return an id creating notebook {seg!r}.")
            folders.append({"id": made["id"], "title": seg, "parent_id": parent})
            parent = made["id"]
        return parent

    # -- notes ---------------------------------------------------------------

    def _get_note(self, doc_id: str, fields: str) -> dict[str, Any]:
        note_id = _check_id("doc_id", doc_id)
        try:
            note = self._call("GET", f"/notes/{note_id}", {"fields": fields}) or {}
        except JoplinNotFoundError:
            raise JoplinError(f"Joplin note {note_id!r} not found.") from None
        if note.get("deleted_time"):
            raise JoplinError(f"Joplin note {note_id!r} is in the trash.")
        return note

    def create_doc(self, parent: str, title: str, body: str) -> str:
        if not title or not title.strip():
            raise JoplinError("create_doc requires a non-empty title.")
        folder_id = self._resolve_notebook(parent, create=True)
        existing = self._paginate(f"/folders/{folder_id}/notes", {"fields": "id,title,deleted_time"})
        if any(note.get("title") == title.strip() for note in existing):
            raise JoplinError(
                f"create_doc refused: a note titled {title.strip()!r} already exists in "
                f"that notebook. Use append_to_doc to add to it."
            )
        made = self._call(
            "POST", "/notes",
            payload={"title": title.strip(), "body": body, "parent_id": folder_id},
        ) or {}
        if not made.get("id"):
            raise JoplinError("Joplin did not return an id for the new note.")
        return made["id"]

    def append_to_doc(self, doc_id: str, content: str) -> None:
        # Read-modify-write: the Data API has no append. An edit made in the
        # Joplin app between the GET and the PUT is overwritten.
        note = self._get_note(doc_id, "id,body,deleted_time")
        existing = note.get("body") or ""
        separator = "" if not existing or existing.endswith("\n") else "\n"
        self._call("PUT", f"/notes/{note['id']}", payload={"body": existing + separator + content})

    def read_doc(self, doc_id: str) -> str:
        return self._get_note(doc_id, "id,body,deleted_time").get("body") or ""

    def read_doc_source(self, doc_id: str) -> str:
        # Joplin stores plain markdown; links stay `[title](:/<id>)` either way.
        return self.read_doc(doc_id)

    def get_deeplink(self, doc_id: str) -> str:
        return f"{self._deeplink_template}{urllib.parse.quote(doc_id.strip(), safe='')}"

    def link_blocks(self, from_id: str, to_id: str) -> None:
        """Append a Joplin note link (`[title](:/<id>)`) to `from_id`.

        This is Joplin's native note-to-note link, the same thing the app
        inserts, so it renders as a link and shows up in backlinks plugins.
        Granularity is the note: Joplin has no block ids to link into.
        """
        target = self._get_note(to_id, "id,title,deleted_time")
        title = (target.get("title") or target["id"]).replace("]", "\\]")
        self.append_to_doc(from_id, f"\n[{title}](:/{target['id']})\n")

    def _hit(self, note: dict[str, Any], snippet: str, score: float = 0.0,
             paths: dict[str, str] | None = None) -> SearchHit:
        raw = {
            "parent_id": note.get("parent_id", ""),
            "updated": _ms_to_iso(note.get("user_updated_time") or note.get("updated_time")),
            "updated_time": note.get("user_updated_time") or note.get("updated_time"),
            "body_len": len(note.get("body") or ""),
        }
        if paths is not None:
            raw["hpath"] = f"{paths.get(note.get('parent_id', ''), '')}/{note.get('title', '')}"
        return SearchHit(
            id=note["id"],
            title=note.get("title") or "(untitled)",
            snippet=snippet,
            deeplink=self.get_deeplink(note["id"]),
            score=score,
            raw=raw,
        )

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        """Case-insensitive substring match over every note's title and body.

        Client-side on purpose - see the module docstring: Joplin's own
        index lags new notes and is not built at all under the headless
        terminal server. Ranking matches the Obsidian adapter: a title hit
        scores 2, a body hit 1; ties go to the most recently edited note.
        """
        needle = query.lower()
        notes = self._paginate(
            "/notes",
            {"fields": "id,title,body,parent_id,updated_time,user_updated_time,deleted_time",
             "order_by": "user_updated_time", "order_dir": "DESC"},
        )
        hits: list[SearchHit] = []
        for note in notes:
            title = note.get("title") or ""
            body = note.get("body") or ""
            title_match = needle in title.lower()
            body_index = body.lower().find(needle)
            if not title_match and body_index < 0:
                continue
            score = (2.0 if title_match else 0.0) + (1.0 if body_index >= 0 else 0.0)
            hits.append(self._hit(note, _snippet(body, body_index), score))
        # Stable sort keeps the newest-first order within a score.
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[: int(limit)]

    def list_children(self, parent: str, limit: int = 200) -> list[SearchHit]:
        """Notes sitting directly in the `parent` notebook, ascending by title.

        Sub-notebooks are NOT returned: a notebook is not a doc on this
        backend and has no id `read_doc` accepts - the same rule Obsidian
        applies to folders. A missing notebook raises; an empty one returns
        `[]`. Trashed notes are excluded (the API leaves them out of folder
        listings). Ordering is title then id, code-point order.
        """
        folder_id = self._resolve_notebook(parent, create=False)
        notes = self._paginate(
            f"/folders/{folder_id}/notes",
            {"fields": "id,title,body,parent_id,updated_time,user_updated_time,deleted_time"},
        )
        hits = [self._hit(note, _snippet(note.get("body") or "")) for note in notes]
        hits.sort(key=lambda hit: (hit.title, hit.id))
        return hits[: int(limit)]

    def list_recent(
        self,
        since: datetime,
        until: datetime,
        min_content_len: int = 0,
        limit: int = 50,
    ) -> list[SearchHit]:
        """Notes whose `user_updated_time` is in [since, until), newest first.

        `user_updated_time` is the edit time Joplin itself shows, millisecond
        precision. It is used instead of `updated_time`, which also moves when
        sync rewrites a note, so a sync pass does not read as fresh activity.
        Body length is the markdown body's character count (title excluded).
        Naive datetimes are local time (`datetime.timestamp()` semantics).
        """
        since_ms = since.timestamp() * 1000
        until_ms = until.timestamp() * 1000
        paths = self._folder_paths()

        def edited(note: dict[str, Any]) -> float:
            return float(note.get("user_updated_time") or note.get("updated_time") or 0)

        notes = self._paginate(
            "/notes",
            {"fields": "id,title,body,parent_id,updated_time,user_updated_time,deleted_time",
             "order_by": "user_updated_time", "order_dir": "DESC"},
            stop=lambda note: edited(note) < since_ms,
        )
        hits: list[SearchHit] = []
        for note in notes:
            if not since_ms <= edited(note) < until_ms:
                continue
            body = note.get("body") or ""
            if len(body) < min_content_len:
                continue
            hits.append(self._hit(note, _snippet(body), paths=paths))
            if len(hits) >= int(limit):
                break
        return hits


class JoplinAdapter(SecondBrainAdapter):
    backend = "joplin"

    def __init__(
        self,
        base_url: str,
        token: str,
        root_notebook: str,
        deeplink_template: str = DEFAULT_DEEPLINK_TEMPLATE,
    ) -> None:
        self.notes = JoplinNotes(base_url, token, root_notebook, deeplink_template)
        self.database = NotImplementedDatabase("joplin")
