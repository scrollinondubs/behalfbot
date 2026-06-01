#!/usr/bin/env python3
"""pacman-queue-add.py — Append a URL to the SiYuan /To Investigate queue.

Helper called inline by other gather scripts when an installer triggers
the "queue this for Pacman review" pattern (e.g. a 👀 reaction in a
Telegram group). POSTs an h2 block with the URL to the configured
SiYuan parent block.

Usage:
    python3 pacman-queue-add.py <url> [--source <source-tag>]

Required env (chassis bootstrap hydrates from chassis.config.yaml or .env):
    SIYUAN_TOKEN                    SiYuan API token
    PACMAN_SIYUAN_QUEUE_BLOCK_ID    Parent block ID for the /To Investigate queue

Optional env:
    SIYUAN_URL                      SiYuan API endpoint (default: http://localhost:6806)
    CHASSIS_HOME / CHASSIS_HOME         Install root (used for log path resolution)

Exits 0 on success, 1 on any error. Designed to fail silently (caller
continues if URL append fails — bad URL, SiYuan unreachable, missing
config, etc.).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def _resolve_repo() -> Path:
    """Resolve chassis root from env vars or compute from script location."""
    for var in ("CHASSIS_HOME", "CHASSIS_HOME"):
        value = os.environ.get(var)
        if value:
            return Path(value)
    return Path(__file__).resolve().parent.parent.parent


REPO = _resolve_repo()
LOG_DIR = REPO / "logs" / "pacman"
LOG_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_SIYUAN_URL = "http://localhost:6806"


def load_env_fallback() -> dict[str, str]:
    """Fallback env loader: reads $REPO/.env as flat key=value if present.

    Container/install bootstraps typically hydrate the env before script
    invocation, so os.environ is the primary source. This fallback covers
    the case where the script is invoked outside the dispatcher loop.
    """
    env: dict[str, str] = {}
    env_file = REPO / ".env"
    if not env_file.exists():
        return env
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def env_get(key: str, default: str | None = None) -> str | None:
    """os.environ first, then $REPO/.env fallback."""
    value = os.environ.get(key)
    if value is not None:
        return value
    return load_env_fallback().get(key, default)


def log(record: dict) -> None:
    record["ts"] = datetime.now(timezone.utc).isoformat()
    record["script"] = "pacman-queue-add"
    log_file = LOG_DIR / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.jsonl"
    with log_file.open("a") as f:
        f.write(json.dumps(record) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="URL to append to /To Investigate queue")
    parser.add_argument("--source", default="manual", help="Source tag for the log entry (e.g. 'telegram-react', 'manual')")
    args = parser.parse_args()

    if not args.url.lower().startswith(("http://", "https://")):
        log({"event": "invalid_url", "url": args.url[:200]})
        return 1

    sy_token = env_get("SIYUAN_TOKEN")
    sy_url = (env_get("SIYUAN_URL", DEFAULT_SIYUAN_URL) or DEFAULT_SIYUAN_URL).rstrip("/")
    queue_block_id = env_get("PACMAN_SIYUAN_QUEUE_BLOCK_ID")

    if not sy_token:
        log({"event": "no_siyuan_token"})
        return 1
    if not queue_block_id:
        log({"event": "no_queue_block_id", "hint": "set PACMAN_SIYUAN_QUEUE_BLOCK_ID in env"})
        return 1

    payload = {
        "dataType": "markdown",
        "data": f"## {args.url}",
        "parentID": queue_block_id,
    }

    req = urllib.request.Request(
        f"{sy_url}/api/block/appendBlock",
        data=json.dumps(payload).encode(),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Token {sy_token}")
    req.add_header("User-Agent", "chassis-pacman/1.0")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            if data.get("code") == 0:
                log({"event": "url_queued", "url": args.url, "source": args.source})
                return 0
            log({"event": "siyuan_append_failed", "url": args.url, "resp": str(data)[:200]})
            return 1
    except Exception as e:
        log({"event": "siyuan_post_exception", "url": args.url, "err": str(e)[:200]})
        return 1


if __name__ == "__main__":
    sys.exit(main())
