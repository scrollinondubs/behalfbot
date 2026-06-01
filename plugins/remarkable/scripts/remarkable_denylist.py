"""Pure-Python classifier for reMarkable document paths.

Enforces the denylist + suspicious-folder approval model defined in
plugins/remarkable/config/remarkable_denylist_config.py.

Call `classify_path(path)` to decide whether a document can be read, must be
skipped, or needs the installer's approval first.

No MCP, no DB, no side effects. Testable in isolation.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from config.remarkable_denylist_config import (  # noqa: E402
    HARD_DENY_FOLDERS,
    SUSPICIOUS_FOLDER_KEYWORDS,
)

Classification = Literal["allow", "hard_deny", "needs_approval"]


@dataclass(frozen=True)
class ClassifyResult:
    classification: Classification
    reason: str


def _split_folders(path: str) -> list[str]:
    """Return the folder components of a reMarkable path, dropping the document name.

    Examples:
      "/Business Books/Designing Data Intensive Applications"
          -> ["Business Books"]
      "/Private/Diary 2026/entry-01"
          -> ["Private", "Diary 2026"]
      "/some-book" -> []
    """
    parts = [p for p in path.split("/") if p]
    return parts[:-1] if len(parts) >= 1 else parts


def classify_path(path: str) -> ClassifyResult:
    """Decide whether a document at `path` should be ingested.

    Rules (first match wins):
      1. Any folder in the path (case-insensitive) matches a hard-deny folder
         -> hard_deny.
      2. Any folder NAME in the path (case-insensitive) exactly matches a
         suspicious keyword -> needs_approval.
      3. Otherwise -> allow.
    """
    folders = _split_folders(path)
    folder_names_lower = [f.lower() for f in folders]

    for deny in HARD_DENY_FOLDERS:
        deny_lower = deny.lower()
        if deny_lower in folder_names_lower:
            return ClassifyResult("hard_deny", f"hard_deny_folders:{deny}")

    for folder in folder_names_lower:
        if folder in SUSPICIOUS_FOLDER_KEYWORDS:
            return ClassifyResult("needs_approval", f"suspicious_folder_keywords:{folder}")

    return ClassifyResult("allow", "default_allow")


def _run_tests() -> int:
    cases: list[tuple[str, Classification, str]] = [
        ("/Private/doc", "hard_deny", "root-level Private"),
        ("/Non-indexed Books/War and Peace", "hard_deny", "Non-indexed Books root"),
        ("/private/something", "hard_deny", "lowercase variant"),
        ("/PRIVATE/something", "hard_deny", "uppercase variant"),
        ("/Diary/entry", "needs_approval", "folder literally named Diary"),
        ("/Journal/entry", "needs_approval", "folder literally named Journal"),
        ("/personal/doc", "needs_approval", "folder literally named personal"),
        ("/Journal 2027/entry", "allow", "'Journal 2027' is not exact match for 'journal'"),
        ("/Business Books/Designing Data Intensive Applications", "allow", "normal business book"),
        ("/top-level-doc", "allow", "root-level doc with no parent folder"),
    ]

    passed = 0
    failed = 0
    for path, expected, description in cases:
        result = classify_path(path)
        if result.classification == expected:
            passed += 1
            print(f"  ok    {path!r:<55} -> {result.classification:<14}  ({description})")
        else:
            failed += 1
            print(
                f"  FAIL  {path!r:<55} -> {result.classification:<14}  expected {expected}: {description}",
                file=sys.stderr,
            )

    print()
    print(f"{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_run_tests())
