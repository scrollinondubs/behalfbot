#!/usr/bin/env python3
"""second-brain-map.py - load the second brain's map doc, or check it for drift.

The map is a table-of-contents doc in the installer's second brain: one line
and one link per first-level doc in Projects, Areas and Resources (Archives
left out on purpose - archived means not current). The assistant maintains it;
the installer's own priorities doc is a different doc with a different owner.
Model and setup: chassis/docs/second-brain-map.md.

    second-brain-map.py load    SessionStart hook payload: the map as
                                additionalContext JSON, or a loud failure banner
    second-brain-map.py drift   heartbeat gather: JSON array of disagreements
                                between the map and the second brain, [] when
                                they agree

Opt-in. With no map doc configured, `load` prints nothing and `drift` prints
[]: an install without a second brain, or one that has not set up a map, sees
no change at all.

Configured but unreachable is the opposite case and is deliberately loud. The
map is NOT cached: a session that silently got yesterday's copy, or nothing,
would carry on as though it had full context, which is the failure the map
exists to prevent. So `load` emits a banner telling the session to surface the
problem in its first message, and `drift` emits a `probe_failed` element so the
heartbeat fires rather than reporting all clear.

Config, read from $CUSTOMER_HOME/chassis.config.yaml (falling back to
$CHASSIS_HOME's copy):

    second_brain:
      backend: siyuan
      map:
        doc_id: "<map doc id>"            # env SECOND_BRAIN_MAP_DOC overrides
        roots:                            # label: container, for `drift`
          projects: "/Projects"           #   SiYuan: hpath, doc id, or notebook:<id>
          areas: "/Areas"                 #   Notion: parent page id
          resources: "/Resources"         #   Obsidian: vault directory

Credentials come from the environment the adapter already reads (SIYUAN_TOKEN,
NOTION_API_TOKEN, ...). A hook runs with Claude Code's environment, which
usually lacks them, so any var still unset is filled from the install's .env,
then from the env blocks of its .mcp.json.
Always exits 0.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable

_PACKAGE_PARENT = Path(__file__).resolve().parents[2]
if str(_PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_PARENT))

# The map is loaded into every session. Past this it has stopped being an index.
MAX_MAP_CHARS = 20000

# A SiYuan kernel is reached on loopback from the host and through the docker
# host gateway from inside the container, and SIYUAN_URL in .env usually holds
# the container's view. The hook runs wherever Claude Code runs, so on a
# connection failure the other view is tried before giving up.
SIYUAN_FALLBACK_URLS = ("http://127.0.0.1:6806", "http://host.docker.internal:6806")

BACKEND_HINTS = {
    "siyuan": (
        "Check that the SiYuan kernel is running and reachable from where this "
        "session runs (desktop app, Docker container or server - whichever this "
        "install uses), that SIYUAN_URL points at it, and that SIYUAN_TOKEN is set."
    ),
    "notion": (
        "Check that NOTION_API_TOKEN is set and that the map page is shared with "
        "the integration."
    ),
    "obsidian": (
        "Check that the vault at second_brain.obsidian.vault_path exists and is "
        "mounted where this session runs."
    ),
}


def install_homes() -> list[Path]:
    homes = []
    for var in ("CUSTOMER_HOME", "CHASSIS_HOME", "CLAUDE_PROJECT_DIR"):
        value = os.environ.get(var)
        if value and Path(value) not in homes:
            homes.append(Path(value))
    return homes


def find_config() -> Path | None:
    for home in install_homes():
        path = home / "chassis.config.yaml"
        if path.is_file():
            return path
    return None


def fill_env_from_dotenv(home: Path) -> None:
    """Set vars from `<home>/.env` that the environment does not already have."""
    path = home / ".env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) and not os.environ.get(key):
            os.environ[key] = value


def fill_env_from_mcp_json(home: Path) -> None:
    """Set unset vars from the `env` blocks of `<home>/.mcp.json`.

    Some installs keep a backend token only in the hydrated .mcp.json, where
    the backend's MCP server reads it. Same rule as .env: never override.
    """
    try:
        servers = json.loads((home / ".mcp.json").read_text(encoding="utf-8")).get("mcpServers") or {}
    except (OSError, ValueError, AttributeError):
        return
    for server in servers.values():
        env = server.get("env") if isinstance(server, dict) else None
        if not isinstance(env, dict):
            continue
        for key, value in env.items():
            if isinstance(value, str) and value and not value.startswith("<") and not os.environ.get(key):
                os.environ[key] = value


def fill_env(home: Path) -> None:
    fill_env_from_dotenv(home)
    fill_env_from_mcp_json(home)


def load_settings(config_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    from chassis.second_brain.factory import _load_config

    cfg = _load_config(config_path)
    sb = cfg.get("second_brain") or {}
    map_cfg = sb.get("map") or {}
    if not isinstance(map_cfg, dict):
        map_cfg = {}
    return cfg, map_cfg


def map_doc_id(map_cfg: dict[str, Any]) -> str:
    value = os.environ.get("SECOND_BRAIN_MAP_DOC") or map_cfg.get("doc_id") or ""
    value = str(value).strip()
    # An unfilled template placeholder means "not configured", not "broken".
    if not value or (value.startswith("<") and value.endswith(">")):
        return ""
    return value


def _is_connection_failure(exc: Exception) -> bool:
    # The adapter wraps every URLError as "request failed". An HTTP error is a
    # URLError too, but it means the kernel answered (a 401 is a bad token), so
    # another URL would not help and "unreachable" would be the wrong diagnosis.
    msg = str(exc)
    return "request failed" in msg and "HTTP Error" not in msg


def with_url_fallback(adapter: Any, call: Callable[[], Any]) -> Any:
    """Run `call`; on a SiYuan connection failure retry against the other URLs."""
    try:
        return call()
    except Exception as exc:
        if adapter.backend != "siyuan" or not _is_connection_failure(exc):
            raise
        notes = adapter.notes
        tried = [notes._base_url]
        last = exc
        for url in SIYUAN_FALLBACK_URLS:
            if url in tried:
                continue
            tried.append(url)
            notes._base_url = url
            try:
                return call()
            except Exception as retry_exc:
                if not _is_connection_failure(retry_exc):
                    raise
                last = retry_exc
        raise RuntimeError(f"SiYuan unreachable at {', '.join(tried)} ({last})") from exc


def read_map(adapter: Any, doc_id: str) -> str:
    notes = adapter.notes
    reader = getattr(notes, "read_doc_source", None) or notes.read_doc
    return with_url_fallback(adapter, lambda: reader(doc_id)) or ""


def emit_context(text: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": text,
    }}))


def failure_banner(backend: str, doc_id: str, reason: str) -> str:
    where = f"{backend}:{doc_id}" if backend else doc_id
    hint = BACKEND_HINTS.get(backend, "Check the second_brain block in chassis.config.yaml.")
    return (
        f"SECOND BRAIN UNREACHABLE. The second-brain map ({where}) could not be "
        f"loaded: {reason}\n\n"
        "This is not a soft failure to work around. The second brain is the "
        "source of truth for the principal's projects and context, and a session "
        "without its map is working blind - the exact amnesia the map exists to "
        "prevent. Tell the principal in your first message, name this reason, and "
        "do not proceed as though you have full context.\n\n" + hint
    )


def cmd_load() -> int:
    config_path = find_config()
    if config_path is None:
        return 0
    try:
        cfg, map_cfg = load_settings(config_path)
    except Exception:  # noqa: BLE001 - an unreadable config cannot name a map doc
        return 0
    doc_id = map_doc_id(map_cfg)
    if not doc_id:
        return 0
    backend = str((cfg.get("second_brain") or {}).get("backend") or "")
    fill_env(config_path.parent)
    try:
        from chassis.second_brain.factory import get_adapter

        adapter = get_adapter(config_path)
        body = read_map(adapter, doc_id).strip()
    except Exception as exc:  # noqa: BLE001 - every failure becomes the banner
        emit_context(failure_banner(backend, doc_id, f"{type(exc).__name__}: {exc}"[:500]))
        return 0
    if not body:
        emit_context(failure_banner(backend, doc_id, "the map doc is empty"))
        return 0
    if len(body) > MAX_MAP_CHARS:
        body = body[:MAX_MAP_CHARS] + (
            f"\n\n[map truncated at {MAX_MAP_CHARS} characters - it has outgrown "
            "an index. One line per first-level doc; detail belongs in the doc.]"
        )
    emit_context(
        f"# Second brain map ({backend}:{doc_id}, loaded live at session start)\n\n"
        "This is the table of contents for the principal's second brain. Each "
        "entry links a first-level doc. Follow a link with the second-brain MCP "
        "when a task touches it, and read its sub-docs when the parent is not "
        "enough. It is the index, not the full picture - the detail is one hop "
        "away. Search the second brain before your own notes for anything that "
        "originates with the principal.\n\n" + body
    )
    return 0


def _norm(value: str) -> str:
    return value.replace("-", "").lower()


def linked_ids(backend: str, text: str) -> set[str]:
    """Ids of the docs the map links to, normalized for comparison."""
    if backend == "siyuan":
        return {m for m in re.findall(r"\(\((\d{14}-[a-z0-9]{7})[\s)]", text)}
    if backend == "notion":
        return {_norm(m) for m in re.findall(r"notion:([0-9a-fA-F-]{32,36})", text)}
    if backend == "obsidian":
        out = set()
        for target in re.findall(r"\[\[([^\]|#]+)", text):
            target = target.strip()
            out.add(target if target.endswith(".md") else target + ".md")
        for target in re.findall(r"\]\(([^)\s]+\.md)\)", text):
            out.add(target)
        return out
    return set()


def is_listed(backend: str, hit: Any, linked: set[str]) -> bool:
    if backend == "notion":
        return _norm(hit.id) in linked
    if backend == "obsidian":
        # [[Note]] resolves by name anywhere in the vault; [[Dir/Note]] by path.
        return hit.id in linked or Path(hit.id).name in linked
    return hit.id in linked


def drift_report(adapter: Any, doc_id: str, roots: dict[str, Any]) -> list[dict[str, Any]]:
    backend = adapter.backend
    text = read_map(adapter, doc_id)
    if not text.strip():
        return [{"kind": "probe_failed", "detail": "the map doc is empty - it should hold the table of contents"}]
    linked = linked_ids(backend, text)
    drift: list[dict[str, Any]] = []
    current: set[str] = set()
    for label, container in roots.items():
        try:
            children = with_url_fallback(adapter, lambda c=container: adapter.notes.list_children(str(c)))
        except Exception as exc:  # noqa: BLE001
            drift.append({"kind": "probe_failed", "detail": f"root {label!r} ({container}): {type(exc).__name__}: {exc}"[:400]})
            continue
        for hit in children:
            key = _norm(hit.id) if backend == "notion" else (Path(hit.id).name if backend == "obsidian" else hit.id)
            current.add(key)
            if backend == "obsidian":
                current.add(hit.id)
            if _norm(hit.id) == _norm(doc_id):
                continue
            if not is_listed(backend, hit, linked):
                drift.append({"kind": "missing_from_map", "root": label, "doc_id": hit.id, "title": hit.title})
    if any(item["kind"] == "probe_failed" for item in drift):
        # A root that could not be listed makes every one of its docs look like
        # a stale link. Report the probe failure, not a wall of false drift.
        return drift
    for ref in sorted(linked - current):
        if ref in (doc_id, _norm(doc_id)):
            continue
        drift.append({"kind": "stale_ref_in_map", "doc_id": ref,
                      "detail": "linked from the map but not a first-level doc under any configured root"})
    return drift


def cmd_drift() -> int:
    def fail(detail: str) -> int:
        print(json.dumps([{"kind": "probe_failed", "detail": detail}]))
        return 0

    config_path = find_config()
    if config_path is None:
        print("[]")
        return 0
    try:
        _cfg, map_cfg = load_settings(config_path)
    except Exception as exc:  # noqa: BLE001
        return fail(f"chassis.config.yaml not read: {type(exc).__name__}")
    doc_id = map_doc_id(map_cfg)
    if not doc_id:
        print("[]")
        return 0
    roots = map_cfg.get("roots") or {}
    if not isinstance(roots, dict) or not roots:
        return fail("second_brain.map.roots is not set - nothing to compare the map against")
    fill_env(config_path.parent)
    try:
        from chassis.second_brain.factory import get_adapter

        adapter = get_adapter(config_path)
        report = drift_report(adapter, doc_id, roots)
    except Exception as exc:  # noqa: BLE001
        return fail(f"{type(exc).__name__}: {exc}"[:400])
    print(json.dumps(report))
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in ("load", "drift"):
        print("usage: second-brain-map.py load|drift", file=sys.stderr)
        return 2
    return cmd_load() if argv[1] == "load" else cmd_drift()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
