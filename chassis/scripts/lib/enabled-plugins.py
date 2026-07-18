#!/usr/bin/env python3
"""Print the enabled plugin/module names from chassis.config.yaml, one per line.

Shared by merge-plugin-triggers.sh and activate-plugins.sh so the two callers
cannot drift (extracted per the behalfbot#53 design, section 4).

Tiny YAML reader sufficient for the chassis.config.yaml modules block - avoids
a PyYAML hard dep. Handles both shapes:

    modules:
      admin: true                 # inline boolean
      loom-vision:
        enabled: true             # nested enabled key

Usage: enabled-plugins.py <path-to-chassis.config.yaml>
"""
import re
import sys


def enabled_modules(path):
    in_modules = False
    plugin = None
    plugins = []
    for raw in open(path):
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if indent == 0:
            in_modules = stripped == "modules:"
            plugin = None
            continue
        if not in_modules:
            continue
        if indent == 2:
            m = re.match(r"^([A-Za-z0-9_-]+):\s*(true|false)\s*(#.*)?$", stripped)
            if m:
                if m.group(2) == "true":
                    plugins.append(m.group(1))
                plugin = None
                continue
            if stripped.endswith(":"):
                plugin = stripped[:-1]
                continue
            plugin = None
            continue
        if indent == 4 and plugin is not None:
            m = re.match(r"enabled:\s*(true|false)\b", stripped)
            if m:
                if m.group(1) == "true":
                    plugins.append(plugin)
                plugin = None  # one shot per plugin
    return sorted(set(plugins))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: enabled-plugins.py <chassis.config.yaml>", file=sys.stderr)
        sys.exit(2)
    print("\n".join(enabled_modules(sys.argv[1])))
