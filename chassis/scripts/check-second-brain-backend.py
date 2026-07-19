#!/usr/bin/env python3
"""check-second-brain-backend.py - one reachability check per second-brain backend.

Called by chassis/scripts/smoke-test.sh. Emits exactly one line on stdout:

    <PASS|FAIL|SKIP>|<message>

and always exits 0 - the caller decides what a status means for the run's exit
code, the same contract every other `record` line in smoke-test.sh follows.

Why this is a separate file
===========================
The check it replaces lived inline in smoke-test.sh and knew only Notion: it
curled api.notion.com and recorded SKIP for anything else. Two consequences,
both bad.

1. `chassis.config.yaml` lists `second_brain_read` under
   `success_criteria.smoke_tests`, but the inline check recorded itself as
   `notion_read`. The criterion named a check that did not exist, so it could
   never be satisfied - not on Obsidian, not on SiYuan, not even on Notion.
2. An Obsidian install SKIPped. A vault path that is missing, is a file, or is
   root-owned and unreadable by the container user is the single most likely
   thing to be wrong with an Obsidian install, and the smoke test said nothing
   about it. Reporting SKIP for the one condition worth checking is worse than
   having no check, because it reads as "nothing to verify here".

Per-backend semantics
=====================
  obsidian - filesystem only, no network. vault_path must exist, be a
             directory, and be readable (R_OK for listing entries, X_OK for
             traversing into them - a directory missing either is unusable
             even though both look like "readable"). Writability must MATCH
             the declared intent: `read_only: true` says pull-only, so a
             writable vault is a config/filesystem disagreement worth
             surfacing, and a read-only vault without the flag is a FAIL
             because every write will be refused at runtime.
  siyuan   - POST /api/system/version with the configured token. Chosen over
             a bare TCP connect because a kernel that is up but rejecting the
             token answers "Auth failed [session]" on every real call, and
             that is precisely the failure a smoke test exists to catch.
  notion   - GET /v1/users/me, unchanged from the inline check.

Missing/unknown backend or missing config is SKIP, not FAIL: a chassis install
is allowed to run without a second brain.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Make `chassis.second_brain` importable regardless of cwd - same resolution
# mcp_server.py uses, and for the same reason (this runs from the entrypoint,
# from a shell, and from tests, all with different working directories).
_PACKAGE_PARENT = Path(__file__).resolve().parents[2]
if str(_PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_PARENT))

HTTP_TIMEOUT = 10


def emit(status: str, message: str) -> int:
    print(f"{status}|{message}")
    return 0


def load_second_brain_config() -> dict[str, Any]:
    """Return the `second_brain` block, or {} when it cannot be read."""
    try:
        from chassis.second_brain.factory import _load_config
    except ImportError:
        return {}
    try:
        config = _load_config()
    except Exception:  # noqa: BLE001 - missing/malformed config is a SKIP, not a crash
        return {}
    return config.get("second_brain") or {}


def check_obsidian(backend_config: dict[str, Any]) -> tuple[str, str]:
    raw_path = str(backend_config.get("vault_path") or "").strip()
    raw_path = os.path.expandvars(raw_path)
    if not raw_path:
        return "FAIL", "second_brain.obsidian.vault_path is not set in chassis.config.yaml"
    vault = Path(raw_path).expanduser()
    if not vault.exists():
        return "FAIL", f"Obsidian vault_path does not exist: {vault}"
    if not vault.is_dir():
        return "FAIL", f"Obsidian vault_path is not a directory: {vault}"
    if not os.access(vault, os.R_OK) or not os.access(vault, os.X_OK):
        return "FAIL", f"Obsidian vault_path is not readable by this user: {vault}"

    declared_read_only = bool(backend_config.get("read_only", False))
    actually_writable = os.access(vault, os.W_OK)
    note_count = sum(1 for _ in vault.rglob("*.md"))

    if declared_read_only:
        # Not a FAIL. The install told us it never writes, and it does not.
        # A writable vault here is worth saying out loud so a misconfigured
        # read_only flag is visible, but nothing is broken either way.
        detail = "writable on disk despite read_only: true" if actually_writable else "read-only as declared"
        return "PASS", f"Obsidian vault readable at {vault} ({note_count} notes), {detail}"
    if not actually_writable:
        return "FAIL", (
            f"Obsidian vault {vault} is not writable and read_only is not set - "
            f"every write will be refused at runtime. Fix permissions, or declare "
            f"second_brain.obsidian.read_only: true."
        )
    return "PASS", f"Obsidian vault readable + writable at {vault} ({note_count} notes)"


def check_siyuan(backend_config: dict[str, Any]) -> tuple[str, str]:
    base_url = (
        str(backend_config.get("base_url") or "").strip()
        or os.environ.get("SIYUAN_URL", "").strip()
        or "http://127.0.0.1:6806"
    )
    token = (
        str(backend_config.get("token") or "").strip()
        or os.environ.get("SIYUAN_TOKEN", "").strip()
    )
    if not token:
        return "SKIP", "SIYUAN_TOKEN not set"
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/system/version",
        data=b"{}",
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Token {token}")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        return "FAIL", f"SiYuan API unreachable at {base_url} ({type(e).__name__}: {e})"
    except json.JSONDecodeError:
        return "FAIL", f"SiYuan API at {base_url} returned non-JSON"
    if body.get("code") != 0:
        # code -1 with "Auth failed [session]" is the wrong-token case.
        return "FAIL", f"SiYuan API rejected the call: code={body.get('code')} msg={body.get('msg')}"
    return "PASS", f"SiYuan API reached at {base_url}, kernel {body.get('data')}"


def check_notion(backend_config: dict[str, Any]) -> tuple[str, str]:
    token = (
        str(backend_config.get("token") or "").strip()
        or os.environ.get("NOTION_API_TOKEN", "").strip()
    )
    if not token:
        return "SKIP", "NOTION_API_TOKEN not set"
    req = urllib.request.Request("https://api.notion.com/v1/users/me")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Notion-Version", "2022-06-28")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        return "FAIL", f"Notion API call failed ({type(e).__name__}: {e})"
    except json.JSONDecodeError:
        return "FAIL", "Notion API returned non-JSON"
    bot_id = body.get("id")
    if not bot_id:
        return "FAIL", "Notion API returned no bot id (token wrong / scope issue)"
    return "PASS", f"Notion API reached, bot id {bot_id}"


CHECKS = {
    "obsidian": check_obsidian,
    "siyuan": check_siyuan,
    "notion": check_notion,
}


def main() -> int:
    sb_config = load_second_brain_config()
    backend = str(sb_config.get("backend") or "").strip().lower()
    if not backend:
        return emit("SKIP", "second_brain.backend not set in chassis.config.yaml")
    check = CHECKS.get(backend)
    if check is None:
        return emit("SKIP", f"no reachability check for second_brain.backend={backend!r}")
    status, message = check(sb_config.get(backend) or {})
    return emit(status, message)


if __name__ == "__main__":
    sys.exit(main())
