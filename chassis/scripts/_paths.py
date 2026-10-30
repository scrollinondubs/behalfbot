"""Customer, chassis and plugin root resolution for Python callers.

Mirrors chassis/scripts/_env.sh so a Python process started by launchd,
systemd or by hand finds the same directories a sourced shell would. Customer
paths are never derived from this file's location: the chassis tree and the
customer tree are separate directories on every supported layout.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

CHASSIS_ROOT = Path(__file__).resolve().parent.parent


def customer_home() -> Path:
    """CUSTOMER_HOME, else CHASSIS_HOME (legacy co-located layout), else
    ~/.behalfbot, else /app/customer. Same order as _env.sh."""
    for var in ("CUSTOMER_HOME", "CHASSIS_HOME"):
        val = os.environ.get(var, "").strip()
        if val:
            return Path(val).expanduser()
    home = Path.home() / ".behalfbot"
    if home.is_dir():
        return home
    return Path("/app/customer")


def plugins_root() -> Path | None:
    """The plugin root the rest of the chassis would use.

    1. CHASSIS_PLUGINS_ROOT, honoured verbatim (operator override).
    2. resolved_root from $CUSTOMER_HOME/plugins-root.state.json, the root
       resolve-plugin-root.sh last picked (baked plus fetched overlay).
       Read rather than re-run: the resolver rewrites state and swaps
       symlink dirs, which a web process has no business doing.
    3. $CHASSIS_HOME/plugins, then /app/plugins.
    """
    val = os.environ.get("CHASSIS_PLUGINS_ROOT", "").strip()
    if val:
        return Path(val).expanduser()
    try:
        state = json.loads((customer_home() / "plugins-root.state.json").read_text())
        resolved = state.get("resolved_root")
        if resolved and Path(resolved).is_dir():
            return Path(resolved)
    except (OSError, ValueError, AttributeError):
        pass
    ch = os.environ.get("CHASSIS_HOME", "").strip()
    if ch and (Path(ch) / "plugins").is_dir():
        return Path(ch) / "plugins"
    if Path("/app/plugins").is_dir():
        return Path("/app/plugins")
    return None
