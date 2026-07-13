"""Obsidian adapter - markdown vault over direct file IO.

An Obsidian vault is a plain directory of markdown files, so this adapter
needs no Obsidian process, plugin, or API - it reads and writes the vault
directly. Doc ids are vault-relative paths (e.g. `Briefings/2026-07-09.md`);
the `.md` suffix is optional on input and normalized on output.

Read-only vaults are a first-class case, not an error state. A pull-only
install (e.g. a git clone synced through a read-only deploy key) sets
`read_only: true` in config, and the adapter also checks the filesystem at
write time. Config states intent; the filesystem states truth; when they
disagree the write is refused and the error says which side blocked it.
Writes that do proceed are atomic (temp file in the target directory, then
`os.replace`) so an interrupted write can never truncate an existing note.

Database surface is NOT implemented - Obsidian has no native structured-row
semantics. Use NotesAdapter only.

Config (chassis.config.yaml):

    second_brain:
      backend: obsidian
      obsidian:
        vault_path: /home/user/second-brain   # absolute path to the vault root
        vault_name: second-brain               # for obsidian:// deeplinks; defaults to the directory name
        read_only: false                       # true for pull-only vault clones
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from chassis.second_brain.base import (
    NotImplementedDatabase,
    NotesAdapter,
    SearchHit,
    SecondBrainAdapter,
)

# Vault housekeeping directories that search should never surface.
_SKIP_DIRS = {".obsidian", ".git", ".trash", ".github"}

_SNIPPET_LEN = 200


class ObsidianError(RuntimeError):
    """Raised on vault IO failures - missing docs, bad paths, unreadable files."""


class ObsidianReadOnlyError(ObsidianError):
    """Raised when a write is attempted against a read-only vault.

    Named separately so callers can distinguish "the vault forbids writes"
    (expected on pull-only installs) from genuine IO failures.
    """


class ObsidianNotes(NotesAdapter):
    def __init__(
        self,
        vault_path: str,
        vault_name: str | None = None,
        read_only: bool = False,
    ) -> None:
        if not vault_path:
            raise ObsidianError(
                "second_brain.obsidian.vault_path is not set in chassis.config.yaml. "
                "Point it at the vault's root directory."
            )
        self._vault = Path(vault_path).expanduser().resolve()
        if not self._vault.is_dir():
            raise ObsidianError(
                f"Obsidian vault_path {str(self._vault)!r} does not exist or is not a directory."
            )
        self._vault_name = vault_name or self._vault.name
        self._read_only = read_only

    # -- path safety ---------------------------------------------------------

    def _resolve(self, doc_id: str) -> Path:
        """Map a vault-relative doc id to an absolute path inside the vault.

        Rejects absolute paths and traversal (`../`) - a doc id must never
        resolve outside the vault root, whichever direction it came from.
        """
        if not doc_id or not doc_id.strip():
            raise ObsidianError("doc_id is empty - expected a vault-relative path.")
        raw = doc_id.strip()
        if Path(raw).is_absolute():
            raise ObsidianError(
                f"doc_id {doc_id!r} is an absolute path - doc ids are vault-relative."
            )
        candidate = (self._vault / raw).resolve()
        if candidate != self._vault and self._vault not in candidate.parents:
            raise ObsidianError(
                f"doc_id {doc_id!r} resolves outside the vault root - refusing."
            )
        if candidate == self._vault:
            raise ObsidianError(
                f"doc_id {doc_id!r} resolves to the vault root itself, not a note."
            )
        if candidate.suffix != ".md":
            candidate = candidate.with_suffix(candidate.suffix + ".md")
        return candidate

    def _rel_id(self, path: Path) -> str:
        return path.relative_to(self._vault).as_posix()

    # -- write safety --------------------------------------------------------

    def _ensure_writable(self, op: str, directory: Path) -> None:
        """Refuse writes when config or the filesystem says the vault is read-only.

        Config states intent, the filesystem states truth. Both are checked;
        when they disagree, the error says so, and the write is refused either
        way - a pull-only vault (e.g. synced via a read-only deploy key) must
        fail loudly here, never half-write or silently no-op.
        """
        probe = directory if directory.exists() else self._vault
        fs_writable = os.access(probe, os.W_OK)
        if self._read_only:
            detail = (
                " (the filesystem currently permits writes, but config intent wins)"
                if fs_writable
                else ""
            )
            raise ObsidianReadOnlyError(
                f"NotesAdapter.{op} refused: vault {str(self._vault)!r} is configured "
                f"read_only: true in chassis.config.yaml{detail}. This is expected for "
                f"pull-only vault clones - nothing was written."
            )
        if not fs_writable:
            raise ObsidianReadOnlyError(
                f"NotesAdapter.{op} refused: {str(probe)!r} is not writable by this "
                f"process, even though config does not set read_only: true - the "
                f"filesystem is the source of truth here. Nothing was written."
            )

    @staticmethod
    def _atomic_write(target: Path, text: str) -> None:
        """Write via temp file + rename so a crash can never truncate a note."""
        fd, tmp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=".obsidian-adapter-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # -- NotesAdapter --------------------------------------------------------

    def create_doc(self, parent: str, title: str, body: str) -> str:
        # `parent` is a vault-relative directory like `Briefings/`; empty means
        # the vault root (the configured default for this backend).
        if not title or not title.strip():
            raise ObsidianError("create_doc requires a non-empty title.")
        rel = f"{parent.strip().strip('/')}/{title.strip()}" if parent.strip() else title.strip()
        target = self._resolve(rel)
        if target.exists():
            raise ObsidianError(
                f"create_doc refused: {self._rel_id(target)!r} already exists - "
                f"refusing to overwrite. Use append_to_doc to add to it."
            )
        self._ensure_writable("create_doc", target.parent)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(target, body)
        return self._rel_id(target)

    def append_to_doc(self, doc_id: str, content: str) -> None:
        target = self._resolve(doc_id)
        if not target.is_file():
            raise ObsidianError(
                f"append_to_doc: doc {doc_id!r} not found in vault {str(self._vault)!r}."
            )
        self._ensure_writable("append_to_doc", target.parent)
        existing = target.read_text(encoding="utf-8")
        separator = "" if not existing or existing.endswith("\n") else "\n"
        self._atomic_write(target, existing + separator + content)

    def read_doc(self, doc_id: str) -> str:
        target = self._resolve(doc_id)
        if not target.is_file():
            raise ObsidianError(
                f"read_doc: doc {doc_id!r} not found in vault {str(self._vault)!r}."
            )
        return target.read_text(encoding="utf-8")

    def get_deeplink(self, doc_id: str) -> str:
        rel = self._rel_id(self._resolve(doc_id))
        return (
            f"obsidian://open?vault={quote(self._vault_name, safe='')}"
            f"&file={quote(rel, safe='')}"
        )

    def link_blocks(self, from_id: str, to_id: str) -> None:
        raise NotImplementedError(
            "NotesAdapter.link_blocks is not implemented for backend='obsidian'. "
            "Obsidian has no block ids in the SiYuan sense - fall back to appending "
            "an inline markdown link (append_to_doc with a [[wikilink]] or relative "
            "markdown link). See docs/second-brain-adapters.md."
        )

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        needle = query.lower()
        hits: list[SearchHit] = []
        for path in sorted(self._vault.rglob("*.md")):
            rel_parts = path.relative_to(self._vault).parts
            if any(part in _SKIP_DIRS for part in rel_parts):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            name_match = needle in path.stem.lower()
            body_index = text.lower().find(needle)
            if not name_match and body_index < 0:
                continue
            if body_index >= 0:
                start = max(0, body_index - 60)
                snippet = text[start : start + _SNIPPET_LEN].strip()
            else:
                snippet = text[:_SNIPPET_LEN].strip()
            rel = self._rel_id(path)
            score = (2.0 if name_match else 0.0) + (1.0 if body_index >= 0 else 0.0)
            hits.append(
                SearchHit(
                    id=rel,
                    title=path.stem,
                    snippet=snippet,
                    deeplink=self.get_deeplink(rel),
                    score=score,
                    raw={"path": str(path)},
                )
            )
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[: int(limit)]

    def list_recent(
        self,
        since: datetime,
        until: datetime,
        min_content_len: int = 0,
        limit: int = 50,
    ) -> list[SearchHit]:
        """Notes with filesystem mtime in [since, until), newest first.

        Honest divergence from SiYuan's block timestamps: mtime says a FILE
        changed, not that a HUMAN edited it. A git pull, iCloud resync, or any
        sync tool that rewrites files produces false "activity" here. Callers
        that narrate recent activity (e.g. daily-log prompts) should treat
        these hits as candidates, not facts.

        `min_content_len` is approximated by file size in bytes - frontmatter
        and markdown syntax count toward it, and multi-byte characters count
        per byte, not per character. Naive datetimes are interpreted as local
        time (`datetime.timestamp()` semantics), matching st_mtime's epoch.
        """
        since_ts = since.timestamp()
        until_ts = until.timestamp()
        candidates: list[tuple[float, int, Path]] = []
        for path in self._vault.rglob("*.md"):
            rel_parts = path.relative_to(self._vault).parts
            if any(part in _SKIP_DIRS for part in rel_parts):
                continue
            try:
                stat_result = path.stat()
            except OSError:
                continue
            if not since_ts <= stat_result.st_mtime < until_ts:
                continue
            if stat_result.st_size < min_content_len:
                continue
            candidates.append((stat_result.st_mtime, stat_result.st_size, path))
        candidates.sort(key=lambda item: item[0], reverse=True)
        hits: list[SearchHit] = []
        for mtime, size, path in candidates[: int(limit)]:
            try:
                snippet = path.read_text(encoding="utf-8")[:_SNIPPET_LEN].strip()
            except (OSError, UnicodeDecodeError):
                snippet = ""
            rel = self._rel_id(path)
            hits.append(
                SearchHit(
                    id=rel,
                    title=path.stem,
                    snippet=snippet,
                    deeplink=self.get_deeplink(rel),
                    raw={"path": str(path), "mtime": mtime, "size_bytes": size},
                )
            )
        return hits


class ObsidianAdapter(SecondBrainAdapter):
    backend = "obsidian"

    def __init__(
        self,
        vault_path: str,
        vault_name: str | None = None,
        read_only: bool = False,
    ) -> None:
        self.notes = ObsidianNotes(vault_path, vault_name, read_only)
        self.database = NotImplementedDatabase("obsidian")
