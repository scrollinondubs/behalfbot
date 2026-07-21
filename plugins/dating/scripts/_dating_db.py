#!/usr/bin/env python3
"""DB-backed dating state helpers (replaces cleared-matches.json + pending-instructions.md).

Ported from <v1-reference-install> PR #522. Chassis adaptations vs <v1-reference-install> source:
  - imports _chassis_db from plugins/dating/scripts/ (not from scripts/)
  - BEHALFBOT_PG_DSN env var used by _chassis_db (CHASSIS_PG_DSN accepted as V1-compat alias)
  - No installer-specific personal data; installer-neutral

Public API
----------
get_open_directives()   -> list[dict]   # unactioned rows from dating_directives
get_active_clearances() -> list[dict]   # non-revoked rows from dating_clearances
mark_directive_acted(id, outcome)       # set acted_at + acted_outcome
insert_directive(match_name, platform, directive, ...)  -> int  (new row id)
insert_clearance(match_name, platform, ...)  -> int
revoke_clearance(match_name, platform, via_message, reason)

CLI usage (for gather scripts):
    python3 plugins/dating/scripts/_dating_db.py pending   -> JSON summary
    python3 plugins/dating/scripts/_dating_db.py clearances -> JSON list
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR / "scripts"))

from _chassis_db import connect  # noqa: E402


def get_open_directives() -> list[dict]:
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        """SELECT id, match_name, platform, match_id, directive,
                  source_message, source_channel, notes, created_at, expires_at
           FROM dating_directives
           WHERE acted_at IS NULL
             AND (expires_at IS NULL OR expires_at > NOW())
           ORDER BY created_at""",
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    conn.close()
    for r in rows:
        for k, v in r.items():
            if isinstance(v, datetime):
                r[k] = v.isoformat()
    return rows


def get_active_clearances() -> list[dict]:
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        """SELECT id, match_name, platform, cleared_at, cleared_via_message,
                  channel, vetted_basis, scope_pierced, exchange_at_clearance, notes
           FROM dating_clearances
           WHERE revoked_at IS NULL
           ORDER BY cleared_at""",
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    conn.close()
    for r in rows:
        for k, v in r.items():
            if isinstance(v, datetime):
                r[k] = v.isoformat()
    return rows


def mark_directive_acted(directive_id: int, outcome: str) -> None:
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        "UPDATE dating_directives SET acted_at = NOW(), acted_outcome = %s WHERE id = %s",
        (outcome[:2000], directive_id),
    )
    conn.commit()
    conn.close()


def insert_directive(
    match_name: str,
    platform: str = "Hinge",
    directive: str = "go_dark",
    source_message: str | None = None,
    source_channel: str | None = None,
    notes: str | None = None,
    match_id: str | None = None,
) -> int:
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO dating_directives
           (match_name, platform, match_id, directive, source_message, source_channel, notes)
           VALUES (%s,%s,%s,%s,%s,%s,%s)
           RETURNING id""",
        (match_name, platform, match_id, directive, source_message, source_channel, notes),
    )
    new_id = cur.fetchone()[0]
    conn.commit()
    conn.close()
    return new_id


def insert_clearance(
    match_name: str,
    platform: str = "Hinge",
    cleared_via_message: str | None = None,
    channel: str | None = None,
    vetted_basis: str | None = None,
    scope_pierced: str = "screening_ladder_only",
    exchange_at_clearance: int | None = None,
    notes: str | None = None,
) -> int:
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO dating_clearances
           (match_name, platform, cleared_via_message, channel, vetted_basis,
            scope_pierced, exchange_at_clearance, notes)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
           RETURNING id""",
        (match_name, platform, cleared_via_message, channel, vetted_basis,
         scope_pierced, exchange_at_clearance, notes),
    )
    new_id = cur.fetchone()[0]
    conn.commit()
    conn.close()
    return new_id


def revoke_clearance(
    match_name: str,
    platform: str = "Hinge",
    via_message: str | None = None,
    reason: str | None = None,
) -> int:
    """Soft-delete a clearance. Returns number of rows updated."""
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        """UPDATE dating_clearances
           SET revoked_at = NOW(), revoked_via_message = %s, revoked_reason = %s
           WHERE match_name = %s AND platform = %s AND revoked_at IS NULL""",
        (via_message, reason, match_name, platform),
    )
    count = cur.rowcount
    conn.commit()
    conn.close()
    return count


# ---------------------------------------------------------------------------
# CLI for gather scripts
# ---------------------------------------------------------------------------

def _cli_pending() -> None:
    """Emit JSON matching the shape expected by gather scripts."""
    rows = get_open_directives()
    counts: dict[str, int] = {"hinge_open": 0, "tinder_open": 0, "bumble_open": 0}
    open_entries = []
    soonest_iso: str | None = None
    soonest_dt: datetime | None = None

    for r in rows:
        plat = r["platform"].lower()
        key = f"{plat}_open"
        if key in counts:
            counts[key] += 1

        notes = r.get("notes") or ""
        import re
        date_m = re.search(r"(20\d{2}-\d{2}-\d{2})(?:\s+(\d{1,2}:\d{2}))?", notes)
        ref: str | None = None
        if date_m:
            date_str = date_m.group(1)
            time_str = date_m.group(2)
            try:
                fmt = "%Y-%m-%d %H:%M" if time_str else "%Y-%m-%d"
                raw = f"{date_str} {time_str}" if time_str else date_str
                dt = datetime.strptime(raw, fmt)
                if dt.date() >= datetime.now().date():
                    ref = raw
                    if soonest_dt is None or dt < soonest_dt:
                        soonest_dt = dt
                        soonest_iso = ref
            except ValueError:
                pass

        open_entries.append({
            "heading": f"{r['created_at'][:16]} -- {r['match_name']} ({r['platform']}): {r['directive']}",
            "platform": r["platform"].lower(),
            "referenced_at": ref,
            "directive_id": r["id"],
        })

    hours: float | None = None
    if soonest_dt is not None:
        delta = soonest_dt - datetime.now()
        hours = round(delta.total_seconds() / 3600.0, 2)

    print(json.dumps({
        "total_open": len(open_entries),
        **counts,
        "soonest_referenced_at": soonest_iso,
        "hours_until_soonest": hours,
        "open_entries": open_entries,
    }))


def _cli_clearances() -> None:
    rows = get_active_clearances()
    print(json.dumps(rows, default=str))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "pending"
    if cmd == "pending":
        _cli_pending()
    elif cmd == "clearances":
        _cli_clearances()
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
