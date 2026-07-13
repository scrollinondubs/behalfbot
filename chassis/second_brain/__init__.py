"""Second-brain adapter package.

Provides a backend-agnostic interface for chassis scripts that need to read or
write durable notes/structured rows. Pick the backend in `chassis.config.yaml`
under `second_brain.backend` (one of: `siyuan`, `notion`, `obsidian`).

Typical use:

    from chassis.second_brain import get_adapter

    sb = get_adapter()                       # reads chassis.config.yaml
    block_id = sb.notes.create_doc(
        parent="<parent-id-or-path>",
        title="2026-05-07 morning briefing",
        body=markdown,
    )
    print(sb.notes.get_deeplink(block_id))   # iPhone-clickable URL

See `docs/second-brain-adapters.md` for the full contract and per-backend notes.
"""

from chassis.second_brain.base import (
    DatabaseAdapter,
    NotesAdapter,
    SearchHit,
    SecondBrainAdapter,
)
from chassis.second_brain.factory import get_adapter, get_mode

__all__ = [
    "DatabaseAdapter",
    "NotesAdapter",
    "SearchHit",
    "SecondBrainAdapter",
    "get_adapter",
    "get_mode",
]
