#!/usr/bin/env python3
"""secondbrain MCP server - ONE tool namespace over whichever backend is configured.

Why this exists
===============
Prompts name concrete MCP tools. If each backend registers its own MCP server
(`mcp__siyuan__*`, `mcp__notion__*`, ...), the same prompt text cannot work
across installs - three servers means three tool namespaces. This server is
the fix ratified in the second-brain cutover plan: one chassis-owned server,
registered under the fixed name `secondbrain` on every install running
`second_brain.mode: adapter`, resolving its backend at startup via
`chassis.second_brain.get_adapter()`. The per-backend complexity lives in the
adapter classes (siyuan.py / notion.py / obsidian.py), not in per-backend MCP
surfaces.

In adapter mode the native backend MCP server is NOT registered at all - tool
availability is the guardrail. If `mcp__siyuan__*` stayed on the menu, models
would keep reaching for it and the abstraction would be silently bypassed.

Tools (mirroring the NotesAdapter protocol in base.py):

    create_doc(parent, title, body) -> {id, deeplink}
    append_to_doc(doc_id, content)
    read_doc(doc_id) -> markdown
    search(query, limit) -> hits[]
    list_recent(since, until, min_content_len, limit) -> hits[]
    get_deeplink(doc_id) -> url

Doc ids are backend-specific opaque strings (SiYuan block id, Notion page id,
Obsidian vault-relative path). Callers must treat them as opaque - pass back
what a tool returned, never construct one.

Runtime
=======
Uses the official MCP Python SDK (`mcp` in requirements.txt) over stdio - the
repo had no prior MCP server implementation to follow. Configuration comes
from chassis.config.yaml, located via CHASSIS_HOME exactly like
`factory._load_config` (the .mcp.json entry passes the env through).

Run directly:

    python3 chassis/second_brain/mcp_server.py

Registered by chassis/.mcp.json.template under `_enable_when:
second_brain.mode == 'adapter'`.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Make `chassis.second_brain` importable when run as a script from any cwd.
# parents[2] is the directory that CONTAINS the `chassis` package, in both the
# standalone-repo and vendored-subtree layouts, because the path is resolved
# relative to this file.
_PACKAGE_PARENT = Path(__file__).resolve().parents[2]
if str(_PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_PARENT))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from chassis.second_brain.base import SearchHit, SecondBrainAdapter  # noqa: E402
from chassis.second_brain.factory import get_adapter  # noqa: E402

mcp = FastMCP("secondbrain")

_adapter: SecondBrainAdapter | None = None


def _notes():
    global _adapter
    if _adapter is None:
        _adapter = get_adapter()
    return _adapter.notes


def _hit_to_dict(hit: SearchHit) -> dict[str, Any]:
    return {
        "id": hit.id,
        "title": hit.title,
        "snippet": hit.snippet,
        "deeplink": hit.deeplink,
        "score": hit.score,
    }


def _parse_iso(label: str, raw: str) -> datetime:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"{label}={raw!r} is not an ISO-8601 datetime "
            f"(expected e.g. '2026-07-09T02:00:00' or '2026-07-09T02:00:00+01:00')."
        ) from exc


@mcp.tool()
def create_doc(parent: str, title: str, body: str) -> dict[str, str]:
    """Create a new doc/page under `parent` in the second brain.

    `parent` is backend-specific (SiYuan hpath like '/Briefings', Notion parent
    page id, Obsidian vault-relative directory like 'Briefings/'); empty string
    means the configured default location. Returns the new doc's id and a
    clickable deeplink.
    """
    notes = _notes()
    doc_id = notes.create_doc(parent, title, body)
    return {"id": doc_id, "deeplink": notes.get_deeplink(doc_id)}


@mcp.tool()
def append_to_doc(doc_id: str, content: str) -> str:
    """Append markdown `content` to the existing doc identified by `doc_id`."""
    _notes().append_to_doc(doc_id, content)
    return f"appended {len(content)} chars to {doc_id}"


@mcp.tool()
def read_doc(doc_id: str) -> str:
    """Return the doc's markdown body. Errors if the doc does not exist."""
    return _notes().read_doc(doc_id)


@mcp.tool()
def search(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Full-text search the second brain. Returns hits ordered by relevance."""
    return [_hit_to_dict(hit) for hit in _notes().search(query, limit=limit)]


@mcp.tool()
def list_recent(
    since: str,
    until: str,
    min_content_len: int = 0,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Docs created or modified in [since, until), newest first.

    `since` / `until` are ISO-8601 datetimes (naive values are interpreted as
    local time). `min_content_len` filters short docs using the closest measure
    the backend offers - see docs/second-brain-adapters.md for per-backend
    divergences (Obsidian uses file mtime and size, which are noisier than
    SiYuan's block timestamps; Notion's last_edited_time has minute
    granularity).
    """
    hits = _notes().list_recent(
        _parse_iso("since", since),
        _parse_iso("until", until),
        min_content_len=min_content_len,
        limit=limit,
    )
    return [_hit_to_dict(hit) for hit in hits]


@mcp.tool()
def get_deeplink(doc_id: str) -> str:
    """Return a URL that opens the doc in the user's app/device."""
    return _notes().get_deeplink(doc_id)


def main() -> None:
    # Resolve the adapter eagerly so a broken config fails the server at
    # startup (visible in `claude mcp list` / server logs) instead of on the
    # first tool call mid-task.
    adapter = _notes()
    print(
        f"secondbrain MCP server: backend={getattr(_adapter, 'backend', '?')} "
        f"({type(adapter).__name__})",
        file=sys.stderr,
    )
    mcp.run()


if __name__ == "__main__":
    main()
