#!/usr/bin/env python3
"""dating-recovery-list.py - emit + consume the Hinge passed-profile recovery queue.

Companion to dating-reconcile.py. Each pending entry is a profile that:
  - The subagent originally passed on
  - The installer later marked as a like or super-like via the RHL flow
  - Has not yet been re-liked on a "show passed profiles" pass

The dating swipe subagent invokes this script when Hinge offers the
"show passed profiles" end-of-feed prompt - the returned list tells the
subagent which profiles to re-like + send openers to.

Ported from <v1-reference-install> PR #534. Chassis adaptations: CHASSIS_HOME env var used;
recovery queue stored at ${CHASSIS_HOME}/logs/dating/recovery_queue.jsonl.

Usage:
    python plugins/dating/scripts/dating-recovery-list.py           # JSON list of pending
    python plugins/dating/scripts/dating-recovery-list.py --pretty  # human-readable summary
    python plugins/dating/scripts/dating-recovery-list.py --max-age-days 7
    python plugins/dating/scripts/dating-recovery-list.py --mark-recovered "Adriana_unk_Hinge_2026-05-02"

The queue file is `${CHASSIS_HOME}/logs/dating/recovery_queue.jsonl` - append-only.
Status flips from `pending` to `recovered` by atomic file rewrite.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

CHASSIS_HOME = Path(os.environ.get("CHASSIS_HOME", Path(__file__).resolve().parent.parent.parent.parent))
RECOVERY_QUEUE = CHASSIS_HOME / "logs" / "dating" / "recovery_queue.jsonl"


def load_queue() -> list[dict]:
    if not RECOVERY_QUEUE.exists():
        return []
    entries = []
    for line in RECOVERY_QUEUE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as e:
            print(f"WARN: malformed queue line: {line[:80]}... ({e})", file=sys.stderr)
    return entries


def save_queue(entries: list[dict]) -> None:
    """Atomic rewrite - write to temp file, then rename over."""
    RECOVERY_QUEUE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix="recovery_queue.", suffix=".jsonl", dir=str(RECOVERY_QUEUE.parent)
    )
    try:
        with open(fd, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")
        Path(tmp_path).replace(RECOVERY_QUEUE)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def filter_pending(entries: list[dict], max_age_days: int | None = None) -> list[dict]:
    pending = [e for e in entries if e.get("status") == "pending"]
    if max_age_days is None:
        return pending
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    out = []
    for e in pending:
        date_str = e.get("date")
        if not date_str:
            continue
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if d >= cutoff:
            out.append(e)
    return out


def mark_recovered(entries: list[dict], screenshot_basename: str) -> bool:
    """Find a pending entry by screenshot_basename and flip its status.
    Returns True if updated, False if not found or already recovered."""
    now_iso = datetime.now(timezone.utc).isoformat()
    for entry in entries:
        if (
            entry.get("screenshot_basename") == screenshot_basename
            and entry.get("status") == "pending"
        ):
            entry["status"] = "recovered"
            entry["recovered_at"] = now_iso
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--max-age-days", type=int, default=None,
        help="Only return entries with date within last N days (Hinge passed-profile UI window)",
    )
    ap.add_argument("--pretty", action="store_true", help="Human-readable output instead of JSON")
    ap.add_argument(
        "--mark-recovered", metavar="SCREENSHOT_BASENAME",
        help="Flip a specific entry's status to 'recovered'",
    )
    ap.add_argument(
        "--all-statuses", action="store_true",
        help="Include recovered entries in output (default: pending only)",
    )
    args = ap.parse_args()

    entries = load_queue()

    if args.mark_recovered:
        if mark_recovered(entries, args.mark_recovered):
            save_queue(entries)
            print(f"Marked recovered: {args.mark_recovered}")
            return 0
        else:
            print(f"No pending entry found for: {args.mark_recovered}", file=sys.stderr)
            return 1

    if args.all_statuses:
        results = entries
    else:
        results = filter_pending(entries, args.max_age_days)

    if args.pretty:
        if not results:
            print("Recovery queue: empty (no pending entries).")
            return 0
        print(f"Recovery queue: {len(results)} pending entries\n")
        for e in results:
            tag = "* SUPER" if e.get("installer_bucket") == "super-like" else "  like "
            print(
                f"  [{tag}] {e['name']:20s} age={e.get('age', '?'):4s} "
                f"{e['platform']:8s} swiped {e['date']}  "
                f"basename={e.get('screenshot_basename')}"
            )
    else:
        json.dump(results, sys.stdout, indent=2)
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
