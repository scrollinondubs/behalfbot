#!/usr/bin/env python3
"""pacman-process-reactions.py — Process Telegram reaction events for Pacman.

Called inline from a Telegram gather script after a getUpdates call. Reads
the raw `result` array (JSON) from stdin and:
1. Caches every message body under `<chat_id>:<message_id>` for later lookup
2. For each `message_reaction` update: if 👀 was added by the configured
   trigger user in one of the configured admin chats, look up the cached
   source message, extract URLs, and POST each URL to the SiYuan
   /To Investigate queue via pacman-queue-add.py.

Required env (chassis bootstrap hydrates from chassis.config.yaml or .env):
    PACMAN_TELEGRAM_TRIGGER_USER_ID   Telegram user_id whose 👀 fires the trigger
    PACMAN_ADMIN_CHAT_IDS             Comma-separated chat_ids to watch
    SIYUAN_TOKEN                      SiYuan API token (used by queue-add helper)
    PACMAN_SIYUAN_QUEUE_BLOCK_ID      Queue parent block ID (used by helper)

If any required env is missing the script silently no-ops (so chassis
installs without Telegram integration can ship the script without
configuring it).

Cache file: $CHASSIS_HOME/scheduled-tasks/telegram-message-cache.json
(last 200 messages by date). Logs to $CHASSIS_HOME/logs/pacman/YYYY-MM-DD.jsonl.

Exits 0 always (caller continues regardless of reaction-processing outcome).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _resolve_repo() -> Path:
    for var in ("CHASSIS_HOME", "CHASSIS_HOME"):
        value = os.environ.get(var)
        if value:
            return Path(value)
    return Path(__file__).resolve().parent.parent.parent


REPO = _resolve_repo()
CACHE_FILE = REPO / "scheduled-tasks" / "telegram-message-cache.json"
LOG_DIR = REPO / "logs" / "pacman"
LOG_DIR.mkdir(parents=True, exist_ok=True)

CACHE_MAX = 200
URL_RE = re.compile(r"https?://[^\s<>\"\'`]+", re.IGNORECASE)
WATCH_EMOJI = "👀"


def load_env_fallback() -> dict[str, str]:
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
    value = os.environ.get(key)
    if value is not None:
        return value
    return load_env_fallback().get(key, default)


def log(record: dict) -> None:
    record["ts"] = datetime.now(timezone.utc).isoformat()
    record["script"] = "pacman-process-reactions"
    log_file = LOG_DIR / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.jsonl"
    with log_file.open("a") as f:
        f.write(json.dumps(record) + "\n")


def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_cache(cache: dict) -> None:
    if len(cache) > CACHE_MAX:
        sorted_items = sorted(cache.items(), key=lambda kv: kv[1].get("date", 0), reverse=True)[:CACHE_MAX]
        cache = dict(sorted_items)
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2))


def queue_url(url: str, source_tag: str) -> bool:
    """Call pacman-queue-add.py to append URL to SiYuan queue. Returns True on success."""
    helper = Path(__file__).parent / "pacman-queue-add.py"
    if not helper.exists():
        log({"event": "queue_helper_missing", "path": str(helper)})
        return False
    try:
        result = subprocess.run(
            ["python3", str(helper), url, "--source", source_tag],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode == 0:
            return True
        log({"event": "queue_helper_failed", "url": url, "rc": result.returncode, "stderr": result.stderr[:200]})
        return False
    except Exception as e:
        log({"event": "queue_helper_exception", "url": url, "err": str(e)[:200]})
        return False


def main() -> int:
    trigger_user_id_raw = env_get("PACMAN_TELEGRAM_TRIGGER_USER_ID", "") or ""
    admin_chat_ids_raw = env_get("PACMAN_ADMIN_CHAT_IDS", "") or ""

    if not trigger_user_id_raw or not admin_chat_ids_raw:
        return 0

    try:
        trigger_user_id = int(trigger_user_id_raw)
        admin_chat_ids = {int(s) for s in admin_chat_ids_raw.split(",") if s.strip()}
    except ValueError as e:
        log({"event": "invalid_config", "err": str(e)})
        return 0

    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        updates = json.loads(raw)
    except json.JSONDecodeError:
        log({"event": "stdin_not_json", "preview": raw[:120]})
        return 0
    if not isinstance(updates, list):
        return 0

    cache = load_cache()

    for u in updates:
        msg = u.get("message")
        if not msg:
            continue
        chat_id = msg.get("chat", {}).get("id")
        message_id = msg.get("message_id")
        text = msg.get("text") or msg.get("caption") or ""
        if not chat_id or not message_id:
            continue
        key = f"{chat_id}:{message_id}"
        cache[key] = {
            "text": text[:2000],
            "date": msg.get("date", 0),
            "from_id": msg.get("from", {}).get("id"),
        }

    queued = []
    for u in updates:
        rxn = u.get("message_reaction")
        if not rxn:
            continue
        chat_id = rxn.get("chat", {}).get("id")
        if chat_id not in admin_chat_ids:
            continue
        user_id = rxn.get("user", {}).get("id")
        if user_id != trigger_user_id:
            continue
        new_reactions = rxn.get("new_reaction", [])
        old_reactions = rxn.get("old_reaction", [])
        added_emojis = {r.get("emoji") for r in new_reactions if r.get("type") == "emoji"} - {
            r.get("emoji") for r in old_reactions if r.get("type") == "emoji"
        }
        if WATCH_EMOJI not in added_emojis:
            continue
        message_id = rxn.get("message_id")
        key = f"{chat_id}:{message_id}"
        cached = cache.get(key)
        if not cached:
            log({"event": "reaction_source_not_cached", "chat_id": chat_id, "message_id": message_id})
            continue
        urls = URL_RE.findall(cached.get("text", ""))
        if not urls:
            log({
                "event": "reaction_no_urls",
                "chat_id": chat_id,
                "message_id": message_id,
                "text_preview": cached.get("text", "")[:120],
            })
            continue
        seen_in_run = set()
        for raw_url in urls:
            cleaned = raw_url.rstrip(".,;:!?)\"'`")
            if cleaned in seen_in_run:
                continue
            seen_in_run.add(cleaned)
            if queue_url(cleaned, source_tag=f"telegram-react-{chat_id}"):
                queued.append({"url": cleaned, "chat_id": chat_id, "message_id": message_id})

    save_cache(cache)

    if queued:
        log({"event": "run_complete", "queued_count": len(queued), "queued": queued})

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log({"event": "uncaught_exception", "err": str(e)[:500]})
        sys.exit(0)
