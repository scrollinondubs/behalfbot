#!/usr/bin/env python3
"""discord-react.py — add a reaction emoji to a Discord message via the bot API.

Used by chassis/scripts/dispatch-trigger.sh to acknowledge inbound trigger
messages without requiring the calling heartbeat to round-trip through claude
-p just to react.

Usage:
    discord-react.py <channel-id> <message-id> <emoji>

Environment:
    DISCORD_BOT_TOKEN — required. Standard bot token (NOT a webhook URL —
                        webhooks can post but cannot react).

Exit codes:
    0  reaction added
    1  HTTP error / network failure
    2  bad usage / token missing

Discord reaction API:
    PUT /channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me

The emoji must be URL-encoded. Unicode emoji pass through as-is; custom server
emoji require name:id form (this V1 helper handles unicode only — extend
later if installers need custom emoji).
"""

from __future__ import annotations

import os
import sys
import urllib.parse
import urllib.request


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: discord-react.py <channel-id> <message-id> <emoji>", file=sys.stderr)
        return 2

    channel_id, message_id, emoji = sys.argv[1], sys.argv[2], sys.argv[3]

    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        print("DISCORD_BOT_TOKEN must be set", file=sys.stderr)
        return 2

    encoded_emoji = urllib.parse.quote(emoji, safe="")
    url = (
        f"https://discord.com/api/v10/channels/{channel_id}"
        f"/messages/{message_id}/reactions/{encoded_emoji}/@me"
    )

    req = urllib.request.Request(url, method="PUT")
    req.add_header("Authorization", f"Bot {token}")
    req.add_header("User-Agent", "behalfbot (dispatch-trigger.sh)")
    req.add_header("Content-Length", "0")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if 200 <= resp.status < 300:
                return 0
            print(f"HTTP {resp.status}", file=sys.stderr)
            return 1
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        print(f"HTTP {e.code}: {body}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
