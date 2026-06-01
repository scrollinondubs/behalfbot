#!/usr/bin/env python3
"""welfare-check-replay.py - Offline classification replay for welfare check reasoning.

Takes a signal-panel JSON (from a fixture or from welfare-check-gather.sh output)
and runs the classification logic offline without invoking Claude or sending any
notifications. Used to verify that historical signal panels classify correctly
before enabling the live system.

Usage:
  python3 scripts/welfare-check-replay.py tests/welfare/fixtures/2026-05-16-birthday-party.json
  python3 scripts/welfare-check-replay.py - < /tmp/signal-panel.json  # stdin

Exit codes:
  0 - NORMAL or AMBIGUOUS classification
  1 - CONCERN classification (would trigger escalation)
  2 - Error reading input

The classification logic here mirrors the rules in scheduled-tasks/welfare-check-prompt.md
and is intentionally deterministic (no LLM call). The LLM's value is its reasoning;
this replay verifies the signal conditions that SHOULD drive the outcome.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


def classify(panel: dict) -> tuple[str, str]:
    """Classify a signal panel as NORMAL, AMBIGUOUS, or CONCERN.

    Returns (classification, reasoning_summary).
    """
    reasons: list[str] = []
    evidence_for_normal: list[str] = []
    evidence_for_concern: list[str] = []

    hours_since = panel.get("hours_since_anything", 0)
    is_quiet_hours = panel.get("is_quiet_hours", False)
    icloud_is_home = panel.get("icloud_is_home", False)
    motion_events = panel.get("ivideon_motion_events_last_24h", [])
    oura_sleep = panel.get("oura_last_sleep_session")
    calendar_today = panel.get("calendar_today", [])
    calendar_yesterday = panel.get("calendar_yesterday", [])
    location_meta = panel.get("icloud_location_meta", {})

    # ── Check quiet hours ────────────────────────────────────────────────────
    if is_quiet_hours:
        reasons.append("is_quiet_hours=true - dispatcher would not have fired")
        return "NORMAL", " | ".join(reasons)

    # ── Calendar context ─────────────────────────────────────────────────────
    all_calendar = list(calendar_today) + list(calendar_yesterday)
    calendar_titles = [e.get("title", "").lower() for e in all_calendar if e]
    party_keywords = ["party", "birthday", "wedding", "festival", "concert", "trip", "travel",
                      "camping", "flight", "vacation", "holiday", "event", "dinner"]
    has_social_event = any(any(kw in t for kw in party_keywords) for t in calendar_titles)
    if has_social_event:
        matched = [t for t in calendar_titles if any(kw in t for kw in party_keywords)]
        evidence_for_normal.append(f"Calendar shows social event(s): {matched}")

    # ── Location ─────────────────────────────────────────────────────────────
    if icloud_is_home:
        age_min = location_meta.get("age_min") if location_meta else None
        staleness_note = f" (last-known, {age_min}min ago)" if location_meta and location_meta.get("is_stale") else ""
        evidence_for_normal.append(f"iCloud location confirms home{staleness_note}")
    else:
        evidence_for_concern.append("iCloud location NOT at home (or unavailable)")

    # ── Camera motion ────────────────────────────────────────────────────────
    if motion_events:
        latest_event = max(motion_events, key=lambda e: e.get("ts", ""))
        evidence_for_normal.append(f"iVideon motion detected - latest at {latest_event.get('ts')} ({latest_event.get('camera')})")
    else:
        evidence_for_concern.append("No iVideon motion events in last 24h")

    # ── Oura sleep ───────────────────────────────────────────────────────────
    if oura_sleep and oura_sleep.get("start"):
        hrs = oura_sleep.get("hrs", 0)
        evidence_for_normal.append(f"Oura sleep session present: {oura_sleep['start']} -> {oura_sleep.get('end')} ({hrs}h)")
    else:
        evidence_for_concern.append("No Oura sleep session found")

    # ── Hours since anything ─────────────────────────────────────────────────
    reasons.append(f"hours_since_anything={hours_since}")

    # ── Classification logic ─────────────────────────────────────────────────
    normal_count = len(evidence_for_normal)
    concern_count = len(evidence_for_concern)

    all_reasons = reasons + [f"FOR_NORMAL: {r}" for r in evidence_for_normal] + [f"FOR_CONCERN: {r}" for r in evidence_for_concern]

    # NORMAL: at least 2 ambient signals support innocent explanation, OR
    # calendar has a social event AND at least 1 ambient signal present
    if normal_count >= 2:
        return "NORMAL", " | ".join(all_reasons)

    if has_social_event and normal_count >= 1:
        return "NORMAL", " | ".join(all_reasons)

    # AMBIGUOUS: at least 1 ambient signal but no full explanation
    if normal_count >= 1:
        return "AMBIGUOUS", " | ".join(all_reasons)

    # CONCERN: no ambient signals and no calendar explanation
    return "CONCERN", " | ".join(all_reasons)


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] == "-":
        try:
            panel = json.load(sys.stdin)
        except json.JSONDecodeError as e:
            print(f"ERROR: failed to parse JSON from stdin: {e}", file=sys.stderr)
            return 2
    else:
        path = Path(sys.argv[1])
        if not path.exists():
            print(f"ERROR: fixture file not found: {path}", file=sys.stderr)
            return 2
        try:
            panel = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            print(f"ERROR: failed to parse {path}: {e}", file=sys.stderr)
            return 2

    classification, reasoning = classify(panel)

    print(f"Classification: {classification}")
    print(f"Reasoning: {reasoning}")
    print()

    if panel.get("_fixture_note"):
        print(f"Fixture note: {panel['_fixture_note']}")
        print()

    if classification == "CONCERN":
        print("RESULT: CONCERN - would trigger escalation cascade", file=sys.stderr)
        return 1
    else:
        print(f"RESULT: {classification} - no escalation")
        return 0


if __name__ == "__main__":
    sys.exit(main())
