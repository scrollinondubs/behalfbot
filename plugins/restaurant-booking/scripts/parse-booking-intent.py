#!/usr/bin/env python3
"""parse-booking-intent.py - Parse free-text booking requests into structured JSON.

Reads a plain-English message (from Discord or CLI) and uses Claude Haiku to
extract restaurant, datetime, party size, and notes.

Usage:
    python3 parse-booking-intent.py "book Contrabando Saldanha for 4 tomorrow at 1pm"
    echo "book Contrabando for 8 people at 13:00 on 2026-05-15" | python3 parse-booking-intent.py

Output (stdout): JSON matching the IntentResult schema below.
Exits 0 on success, 1 on parse error (stderr has details).

Schema:
{
  "restaurant_name": str,
  "restaurant_url_hint": str | null,
  "datetime_iso": str (ISO-8601 with tz offset),
  "party_size": int,
  "notes": str | null,
  "intent_confidence": float (0..1)
}

Confidence < 0.7 means the caller should ask the user to clarify before booking.
"""
from __future__ import annotations

import json
import sys
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent.parent.parent
SCRIPTS = REPO / "scripts"

HAIKU_MODEL = "claude-haiku-4-5"

SYSTEM_PROMPT = """\
You are a restaurant booking intent parser. Extract structured booking details from the user's message.

Return ONLY valid JSON with exactly these fields:
{
  "restaurant_name": "<restaurant name as mentioned>",
  "restaurant_url_hint": "<TheFork URL if user included one, otherwise null>",
  "datetime_iso": "<ISO-8601 datetime with timezone offset, e.g. 2026-05-15T13:00:00+01:00>",
  "party_size": <integer>,
  "notes": "<any special requests or null>",
  "intent_confidence": <float 0.0-1.0>
}

Rules:
- "tomorrow" resolves relative to the current_date provided in the user message context.
- If no date is mentioned, assume today.
- If no time is mentioned, set confidence <= 0.5.
- If no party size is mentioned, default to 2 and lower confidence.
- Lisbon timezone offset is +01:00 (WEST in summer) or +00:00 (WET in winter).
  For dates May-October use +01:00. For dates Nov-April use +00:00.
- TheFork URLs look like: https://www.thefork.com/restaurant/<name>-r<id>
- confidence 0.9-1.0: all fields clearly present
- confidence 0.7-0.89: minor ambiguity (e.g. name match uncertain)
- confidence < 0.7: significant ambiguity (missing date, time, or restaurant)
- Do not include any text outside the JSON object. No markdown, no explanation.
"""


def _fetch_anthropic_key() -> str:
    """Get Anthropic API key from env (loaded via _loadenv) or fallback."""
    try:
        sys.path.insert(0, str(SCRIPTS))
        from _loadenv import get as env_get  # type: ignore
        key = env_get("ANTHROPIC_API_KEY")
        if key:
            return key
    except ImportError:
        pass
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key
    raise RuntimeError("ANTHROPIC_API_KEY not available - check _loadenv or env")


def _call_haiku(message: str, today_iso: str) -> dict[str, Any]:
    """Call Claude Haiku with the intent-parse prompt. Returns parsed JSON dict."""
    import urllib.request
    import urllib.error

    api_key = _fetch_anthropic_key()

    user_content = f"Current date: {today_iso}\n\nUser request: {message}"

    payload = {
        "model": HAIKU_MODEL,
        "max_tokens": 512,
        "system": SYSTEM_PROMPT,
        "messages": [
            {"role": "user", "content": user_content}
        ],
    }

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Anthropic API error {e.code}: {err_body}") from e

    content_blocks = data.get("content", [])
    if not content_blocks:
        raise RuntimeError("Anthropic API returned empty content")

    raw_text = content_blocks[0].get("text", "").strip()

    # Strip markdown code fences if Haiku wraps the JSON anyway
    if raw_text.startswith("```"):
        lines = raw_text.splitlines()
        raw_text = "\n".join(
            l for l in lines if not l.startswith("```")
        ).strip()

    try:
        result = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Haiku returned non-JSON text: {raw_text[:200]!r}"
        ) from e

    return result


def _validate_result(result: dict[str, Any]) -> dict[str, Any]:
    """Validate schema and coerce types. Raises ValueError on fatal schema mismatch."""
    required = ["restaurant_name", "datetime_iso", "party_size", "intent_confidence"]
    for field in required:
        if field not in result:
            raise ValueError(f"Missing required field: {field}")

    # Coerce
    result["party_size"] = int(result["party_size"])
    result["intent_confidence"] = float(result["intent_confidence"])
    result.setdefault("restaurant_url_hint", None)
    result.setdefault("notes", None)

    # Clamp confidence
    result["intent_confidence"] = max(0.0, min(1.0, result["intent_confidence"]))

    return result


def parse_intent(message: str) -> dict[str, Any]:
    """Main entry point. Takes free-text, returns structured dict."""
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw = _call_haiku(message, today_iso)
    return _validate_result(raw)


def main() -> None:
    if len(sys.argv) > 1:
        message = " ".join(sys.argv[1:])
    elif not sys.stdin.isatty():
        message = sys.stdin.read().strip()
    else:
        print("Usage: parse-booking-intent.py <free-text booking request>", file=sys.stderr)
        print('  e.g.: parse-booking-intent.py "book Contrabando Saldanha for 4 tomorrow at 1pm"', file=sys.stderr)
        sys.exit(1)

    if not message:
        print("Error: empty input", file=sys.stderr)
        sys.exit(1)

    try:
        result = parse_intent(message)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"Schema error: {e}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, indent=2))

    confidence = result.get("intent_confidence", 0.0)
    if confidence < 0.7:
        print(
            f"\nWARNING: confidence {confidence:.2f} is below 0.7 threshold - "
            "ask user to clarify before booking.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
