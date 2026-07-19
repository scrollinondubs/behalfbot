#!/usr/bin/env python3
"""pacman-queue-add.py - Append a URL to the Pacman queue.

Helper called inline by other gather scripts when an installer triggers the
"queue this for Pacman review" pattern (e.g. a 👀 reaction in a Telegram
group). Kept as its own entry point because existing callers invoke it by
name; the queue logic itself lives in chassis/pacman/queue.py and this is a
thin wrapper over `queue.add`.

What changed (2026-07-19, docs/pacman-queue-storage.md): the URL used to be
POSTed as an h2 block under PACMAN_SIYUAN_QUEUE_BLOCK_ID. It now becomes a row
in chassis_pacman_queue, so this works identically on SiYuan, Obsidian, Notion,
and adapter-mode installs.

Behaviour change worth reading before you rely on it: this script used to be
"designed to fail silently (caller continues if URL append fails)". It is not
any more. The queue row is the only durable record that a URL was ever
submitted - once the source Discord or Telegram message scrolls out of reach
there is nothing to recover it from - so a failed write must be visible to the
caller and must happen before anything acknowledges the submission. Exit codes:

    0  queued; the approval token is printed to stdout
    1  bad URL, or the write failed for a non-connectivity reason
    2  Postgres unconfigured or unreachable

Usage:
    python3 pacman-queue-add.py <url> [--source <source-tag>] [--source-ref <id>]

Required env (chassis bootstrap hydrates from chassis.config.yaml or .env):
    CHASSIS_PG_DSN (or BEHALFBOT_PG_DSN / JAX_PG_DSN)   Postgres DSN

Optional env:
    CHASSIS_HOME    Install root, used for log path resolution
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_PACKAGE_PARENT = Path(__file__).resolve().parents[2]
if str(_PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_PARENT))

from chassis.db import ChassisDBUnavailable  # noqa: E402
from chassis.pacman import queue  # noqa: E402


def _resolve_repo() -> Path:
    value = os.environ.get("CHASSIS_HOME") or os.environ.get("CUSTOMER_HOME")
    if value:
        return Path(value)
    return Path(__file__).resolve().parents[2]


REPO = _resolve_repo()
LOG_DIR = REPO / "logs" / "pacman"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def log(record: dict) -> None:
    record["ts"] = datetime.now(timezone.utc).isoformat()
    record["script"] = "pacman-queue-add"
    log_file = LOG_DIR / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.jsonl"
    with log_file.open("a") as f:
        f.write(json.dumps(record) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="URL to append to the Pacman queue")
    parser.add_argument(
        "--source",
        default="manual",
        help="Source tag stored on the row (e.g. 'telegram-react-123', 'manual')",
    )
    parser.add_argument(
        "--source-ref",
        default=None,
        help="Originating message id, kept for audit",
    )
    args = parser.parse_args(argv)

    try:
        token = queue.add(args.url, source=args.source, source_ref=args.source_ref)
    except queue.QueueError as exc:
        log({"event": "queue_add_rejected", "url": args.url[:200], "err": str(exc)[:200]})
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except ChassisDBUnavailable as exc:
        log({"event": "queue_db_unavailable", "url": args.url[:200], "err": str(exc)[:300]})
        print(f"ERROR: pacman queue unavailable, URL NOT queued: {exc}", file=sys.stderr)
        return 2

    log({"event": "url_queued", "url": args.url, "source": args.source, "token": token})
    print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
