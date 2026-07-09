"""Factory — read chassis.config.yaml + .env, return the configured adapter."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from chassis.second_brain.base import SecondBrainAdapter


def _load_config(config_path: Path | None = None) -> dict[str, Any]:
    """Parse chassis.config.yaml (PyYAML if available, fallback parser otherwise).

    Avoids a hard PyYAML dependency for chassis-core operations — the fallback
    handles the simple key/value structure we use. Plugins that need full YAML
    support import yaml directly.
    """
    path = config_path or Path(os.environ.get("CHASSIS_HOME", ".")) / "chassis.config.yaml"
    if not path.exists():
        raise FileNotFoundError(f"chassis.config.yaml not found at {path}")
    raw = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore[import-untyped]

        return yaml.safe_load(raw) or {}
    except ImportError:
        return _parse_yaml_minimal(raw)


def _parse_yaml_minimal(text: str) -> dict[str, Any]:
    """Tiny YAML parser — handles the second_brain.* subtree we need.

    Not a real YAML implementation. Use yaml.safe_load when PyYAML is available.
    Supports: nested mappings, scalar values (str/int/float/bool/null), comments.
    Does not support: lists, anchors, multiline strings, complex types.
    """
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        key, sep, value = line.lstrip().partition(":")
        if not sep:
            continue
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            stack = [(-1, root)]
        parent = stack[-1][1]
        value = value.strip()
        if not value:
            child: dict[str, Any] = {}
            parent[key.strip()] = child
            stack.append((indent, child))
            continue
        parent[key.strip()] = _coerce_scalar(value)
    return root


def _coerce_scalar(raw: str) -> Any:
    text = raw.strip()
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        return text[1:-1]
    lower = text.lower()
    if lower in {"true", "yes"}:
        return True
    if lower in {"false", "no"}:
        return False
    if lower in {"null", "~", ""}:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _resolve_env(value: Any) -> Any:
    """Expand `${VAR}` references in config strings against os.environ."""
    if not isinstance(value, str) or "${" not in value:
        return value
    out = value
    while "${" in out:
        start = out.index("${")
        end = out.index("}", start)
        var = out[start + 2 : end]
        out = out[:start] + os.environ.get(var, "") + out[end + 1 :]
    return out


VALID_MODES = ("direct", "adapter")


def get_mode(config_path: Path | None = None) -> str:
    """Return `second_brain.mode` from chassis.config.yaml.

    Modes:
      - `direct` (default, and the value assumed when the key is absent so
        installs that predate the key see zero behavior change): the backend's
        own MCP server is registered and chassis scripts talk to the backend
        natively - today's behavior.
      - `adapter`: the chassis-owned `secondbrain` MCP server is registered
        INSTEAD of the native backend server, and callers go through
        `get_adapter()`.
    """
    config = _load_config(config_path)
    sb_config = config.get("second_brain") or {}
    mode = sb_config.get("mode") or "direct"
    if mode not in VALID_MODES:
        raise ValueError(
            f"Unsupported second_brain.mode={mode!r} in chassis.config.yaml. "
            f"Supported modes: {', '.join(VALID_MODES)}."
        )
    return mode


def get_adapter(config_path: Path | None = None) -> SecondBrainAdapter:
    """Return the adapter configured in chassis.config.yaml.

    Reads `second_brain.backend` and the matching backend block (siyuan / notion / obsidian).
    Resolves `${VAR}` env-var references against the chassis .env (assumed loaded
    by the caller — chassis bootstrap does this in its launchd-equivalent unit).
    """
    config = _load_config(config_path)
    sb_config = config.get("second_brain") or {}
    backend = sb_config.get("backend")
    if not backend:
        raise ValueError(
            "chassis.config.yaml is missing second_brain.backend. "
            "Set it to one of: siyuan, notion, obsidian."
        )
    backend_config = sb_config.get(backend) or {}
    if backend == "siyuan":
        from chassis.second_brain.siyuan import SiYuanAdapter

        return SiYuanAdapter(
            base_url=_resolve_env(backend_config.get("base_url", "http://127.0.0.1:6806")),
            token=_resolve_env(backend_config.get("token", "")),
            notebook_id=_resolve_env(backend_config.get("notebook_id", "")),
            deeplink_template=_resolve_env(backend_config.get("deeplink_template", "siyuan://blocks/")),
        )
    if backend == "notion":
        from chassis.second_brain.notion import NotionAdapter

        return NotionAdapter(
            token=_resolve_env(backend_config.get("token", "")),
            notes_root=_resolve_env(backend_config.get("notes_root", "")),
            databases={k: _resolve_env(v) for k, v in (backend_config.get("databases") or {}).items()},
            natural_keys=backend_config.get("natural_keys") or {},
            active_database=backend_config.get("active_database"),
        )
    if backend == "obsidian":
        from chassis.second_brain.obsidian import ObsidianAdapter

        return ObsidianAdapter(
            vault_path=_resolve_env(backend_config.get("vault_path", "")),
            vault_name=_resolve_env(backend_config.get("vault_name", "")) or None,
            read_only=bool(backend_config.get("read_only", False)),
        )
    raise ValueError(
        f"Unsupported second_brain.backend={backend!r}. "
        f"Supported backends: siyuan, notion, obsidian."
    )
