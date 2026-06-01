#!/usr/bin/env python3
"""confirm-via-discord.py - Send a soft-confirm message to Discord and wait for reaction.

Sends a screenshot + summary to Sean's Discord channel, then polls for a
reaction (checkmark to confirm, X to abort) with a 10-minute timeout.

Usage:
    python3 confirm-via-discord.py \
        --restaurant "Contrabando Saldanha" \
        --datetime "2026-05-15 13:00" \
        --party-size 4 \
        --screenshot /path/to/preconfirm.png

Output (stdout):
    JSON: {"confirmed": true/false, "reason": "user_approved|user_cancelled|timeout"}

Exit codes:
    0 - completed (check "confirmed" in JSON output)
    1 - error sending message or polling

The Discord bot token is loaded from env (DISCORD_BOT_TOKEN) via _loadenv.
Channel ID is read from config/restaurant-booking.yaml (default: Sean's #<primary> channel).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent.parent.parent
SCRIPTS = REPO / "scripts"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent

DISCORD_API = "https://discord.com/api/v10"

# Default to Sean's main #<primary> channel (1487190325394014432)
DEFAULT_CHANNEL_ID = "1487190325394014432"

CONFIRM_EMOJI = "✅"
CANCEL_EMOJI = "❌"
POLL_INTERVAL_SECONDS = 10
TIMEOUT_SECONDS = 600  # 10 minutes


def _load_env() -> dict[str, str]:
    """Load env from _loadenv or fallback."""
    try:
        sys.path.insert(0, str(SCRIPTS))
        from _loadenv import load_env as _unified  # type: ignore
        return dict(_unified())
    except ImportError:
        pass
    env: dict[str, str] = {}
    env_file = REPO / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _load_config() -> dict[str, Any]:
    """Load plugin config YAML."""
    config_path = PLUGIN_ROOT / "config" / "restaurant-booking.yaml"
    config: dict[str, Any] = {}
    if not config_path.exists():
        return config
    # Minimal YAML parser - we only need scalar values
    for line in config_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            v = v.strip().strip('"').strip("'")
            if v:
                config[k.strip()] = v
    return config


def _get_token() -> str:
    """Get Discord bot token."""
    env = _load_env()
    token = env.get("DISCORD_BOT_TOKEN", "") or os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        raise RuntimeError(
            "DISCORD_BOT_TOKEN not found in env. "
            "Make sure _loadenv can source $CHASSIS_HOME/.env and the Vaultwarden item 'discord-bot-token' is accessible."
        )
    return token


def _discord_request(
    method: str,
    path: str,
    token: str,
    data: dict | None = None,
    files: dict | None = None,
) -> dict[str, Any]:
    """Make a Discord REST API request."""
    url = f"{DISCORD_API}{path}"

    if files:
        # Multipart form data for file uploads
        boundary = "----DiscordFormBoundary7MA4YWxkTrZu0gW"
        body_parts: list[bytes] = []

        if data:
            payload_json = json.dumps(data).encode("utf-8")
            body_parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="payload_json"\r\nContent-Type: application/json\r\n\r\n'.encode()
                + payload_json
                + b"\r\n"
            )

        for field_name, (filename, content_bytes, content_type) in files.items():
            body_parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode()
                + content_bytes
                + b"\r\n"
            )

        body_parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(body_parts)
        content_type = f"multipart/form-data; boundary={boundary}"
    elif data is not None:
        body = json.dumps(data).encode("utf-8")
        content_type = "application/json"
    else:
        body = b""
        content_type = "application/json"

    req = urllib.request.Request(
        url,
        data=body if body else None,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": content_type,
            "User-Agent": "${ASSISTANT_NAME}/1.0",
        },
        method=method,
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            response_body = resp.read().decode("utf-8")
            if response_body:
                return json.loads(response_body)
            return {}
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Discord API {method} {path} failed {e.code}: {err}") from e


def _send_message_with_screenshot(
    token: str,
    channel_id: str,
    restaurant_name: str,
    datetime_str: str,
    party_size: int,
    screenshot_path: str | None,
) -> str:
    """Send the soft-confirm message to Discord. Returns the message ID."""

    text = (
        f"About to book **{restaurant_name}** at {datetime_str}, party of {party_size}.\n\n"
        f"React with {CONFIRM_EMOJI} to confirm the booking or {CANCEL_EMOJI} to cancel.\n"
        f"Auto-aborts in 10 minutes if no response."
    )

    payload = {"content": text}

    if screenshot_path and Path(screenshot_path).exists():
        img_bytes = Path(screenshot_path).read_bytes()
        files = {
            "files[0]": ("booking-preview.png", img_bytes, "image/png"),
        }
        msg = _discord_request("POST", f"/channels/{channel_id}/messages", token, data=payload, files=files)
    else:
        msg = _discord_request("POST", f"/channels/{channel_id}/messages", token, data=payload)

    message_id = msg.get("id", "")
    if not message_id:
        raise RuntimeError(f"Discord message send failed - no ID in response: {msg}")

    return message_id


def _add_reactions(token: str, channel_id: str, message_id: str) -> None:
    """Add the confirm/cancel reactions to the message so Sean can tap them."""
    for emoji in [CONFIRM_EMOJI, CANCEL_EMOJI]:
        encoded = urllib.parse.quote(emoji)
        try:
            _discord_request(
                "PUT",
                f"/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me",
                token,
            )
            time.sleep(0.5)  # avoid Discord rate limiting
        except RuntimeError as e:
            print(f"[confirm-via-discord] Warning: could not add reaction {emoji}: {e}", file=sys.stderr)


def _check_reaction(
    token: str,
    channel_id: str,
    message_id: str,
    emoji: str,
) -> bool:
    """Check if any user (other than the bot itself) reacted with the given emoji."""
    encoded = urllib.parse.quote(emoji)
    try:
        users = _discord_request(
            "GET",
            f"/channels/{channel_id}/messages/{message_id}/reactions/{encoded}",
            token,
        )
        if not isinstance(users, list):
            return False
        # The bot adds its own reaction as a prompt - we want OTHER users to react
        # Discord returns all reactors including the bot
        # Check if there's at least one non-bot reactor
        # Discord bot accounts have is_bot=True; the bot's own reaction has the bot's user ID
        # For simplicity in V1: if there are 2+ reactors, a human has also reacted
        # (1 = just the bot's own prompt reaction, 2+ = human reacted too)
        return len(users) >= 2
    except RuntimeError:
        return False


def _poll_for_reaction(
    token: str,
    channel_id: str,
    message_id: str,
    timeout: int,
    poll_interval: int,
) -> str:
    """Poll for a confirm or cancel reaction. Returns 'confirmed', 'cancelled', or 'timeout'."""
    deadline = time.time() + timeout
    elapsed_log = 0

    while time.time() < deadline:
        if _check_reaction(token, channel_id, message_id, CONFIRM_EMOJI):
            return "confirmed"
        if _check_reaction(token, channel_id, message_id, CANCEL_EMOJI):
            return "cancelled"

        remaining = int(deadline - time.time())
        if elapsed_log % 60 == 0:  # log every ~60s
            print(f"[confirm-via-discord] Waiting for reaction... {remaining}s remaining", file=sys.stderr)

        time.sleep(poll_interval)
        elapsed_log += poll_interval

    return "timeout"


def run_confirm(
    restaurant_name: str,
    datetime_str: str,
    party_size: int,
    screenshot_path: str | None = None,
    channel_id: str | None = None,
    timeout: int = TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Full soft-confirm flow. Returns {"confirmed": bool, "reason": str}."""
    config = _load_config()
    effective_channel = channel_id or config.get("discord_channel_id", DEFAULT_CHANNEL_ID)
    effective_timeout = int(config.get("confirm_timeout_seconds", timeout))

    token = _get_token()
    print(f"[confirm-via-discord] Sending soft-confirm to channel {effective_channel}...", file=sys.stderr)

    message_id = _send_message_with_screenshot(
        token=token,
        channel_id=effective_channel,
        restaurant_name=restaurant_name,
        datetime_str=datetime_str,
        party_size=party_size,
        screenshot_path=screenshot_path,
    )
    print(f"[confirm-via-discord] Message sent (id: {message_id}). Adding reactions...", file=sys.stderr)

    _add_reactions(token, effective_channel, message_id)

    print(f"[confirm-via-discord] Polling for reaction (timeout: {effective_timeout}s)...", file=sys.stderr)
    outcome = _poll_for_reaction(
        token=token,
        channel_id=effective_channel,
        message_id=message_id,
        timeout=effective_timeout,
        poll_interval=POLL_INTERVAL_SECONDS,
    )

    confirmed = outcome == "confirmed"
    result = {"confirmed": confirmed, "reason": outcome}

    # Post a follow-up message so Sean sees the outcome
    follow_up = "Booking confirmed - proceeding!" if confirmed else f"Booking aborted ({outcome})."
    try:
        _discord_request(
            "POST",
            f"/channels/{effective_channel}/messages",
            token,
            data={"content": follow_up, "message_reference": {"message_id": message_id}},
        )
    except RuntimeError:
        pass  # follow-up message is best-effort

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Discord soft-confirm for restaurant booking",
    )
    parser.add_argument("--restaurant", required=True, help="Restaurant name")
    parser.add_argument("--datetime", required=True, dest="datetime_str", help="Booking datetime string")
    parser.add_argument("--party-size", type=int, required=True, help="Number of guests")
    parser.add_argument("--screenshot", default=None, help="Path to pre-confirm screenshot (optional)")
    parser.add_argument("--channel-id", default=None, help="Discord channel ID override")
    parser.add_argument("--timeout", type=int, default=TIMEOUT_SECONDS, help=f"Timeout in seconds (default: {TIMEOUT_SECONDS})")

    args = parser.parse_args()

    try:
        result = run_confirm(
            restaurant_name=args.restaurant,
            datetime_str=args.datetime_str,
            party_size=args.party_size,
            screenshot_path=args.screenshot,
            channel_id=args.channel_id,
            timeout=args.timeout,
        )
        print(json.dumps(result))
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
