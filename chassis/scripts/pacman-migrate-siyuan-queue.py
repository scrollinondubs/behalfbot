#!/usr/bin/env python3
"""pacman-migrate-siyuan-queue.py - One-time backfill of the live SiYuan queue.

Reads the URL blocks under the SiYuan /To Investigate parent and writes one
chassis_pacman_queue row per URL. Runs once per install; Sean's is the only
install with a populated queue.

Idempotent. Every row it writes carries `legacy_block_id`, and the partial
unique index ux_pacman_queue_legacy_block_url on (legacy_block_id, url) makes
a re-run a no-op rather than a duplicate. Re-running after a partial failure is
the intended recovery, not a hazard.

Nothing is deleted. The SiYuan blocks are left exactly where they are - this
script only reads them. Cleaning them up is a separate, later, manual step
once a full drain cycle has run clean against Postgres. Disk is cheap; an
unrecoverable queue is not.

Nothing is dropped silently. A block whose URL cannot be extracted, or whose
row fails to insert, is reported on stderr and counted in the summary, and the
script exits non-zero. Read the summary before you unfreeze Pacman.

Ordering: created_at is reconstructed from the leading 14 digits of the SiYuan
block ID (YYYYMMDDHHMMSS, install-local time). This is the one place that
encoded timestamp is genuinely useful, and it is why FIFO order survives the
move. Blocks with an unparseable ID prefix fall back to the SiYuan `created`
column, and are reported.

Usage:
    # 1. Freeze first. Both gather-pacman-queue.sh and pacman.sh honor this.
    touch "$CHASSIS_HOME/PACMAN_HARD_PAUSE"

    # 2. Snapshot. This is the rollback, keep the file.
    python3 chassis/scripts/pacman-migrate-siyuan-queue.py --snapshot queue-snapshot.json --dry-run

    # 3. Backfill.
    python3 chassis/scripts/pacman-migrate-siyuan-queue.py --snapshot queue-snapshot.json

Required env:
    SIYUAN_TOKEN                    SiYuan API token
    PACMAN_SIYUAN_QUEUE_BLOCK_ID    /To Investigate parent block ID
    CHASSIS_PG_DSN                  Postgres DSN (or BEHALFBOT_PG_DSN / JAX_PG_DSN)

Optional env:
    SIYUAN_URL                      Default http://localhost:6806
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

_PACKAGE_PARENT = Path(__file__).resolve().parents[2]
if str(_PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_PARENT))

from chassis.db import ChassisDBUnavailable, connect  # noqa: E402
from chassis.pacman.tokens import new_token  # noqa: E402

URL_RE = re.compile(r"https?://[^\s<>\"'`,)\]]+", re.IGNORECASE)
BLOCK_ID_TS_RE = re.compile(r"^(\d{14})-")

DEFAULT_SIYUAN_URL = "http://localhost:6806"
MIGRATION_SOURCE = "siyuan-migration"


def fetch_queue_blocks(siyuan_url: str, token: str, parent_id: str) -> list[dict]:
    """Read the queue blocks from SiYuan, oldest first.

    Same predicate the old gather script counted with, so the snapshot and the
    old count agree. If they do not, that discrepancy is itself the signal.
    """
    stmt = (
        "SELECT id, content, created FROM blocks "
        f"WHERE root_id = '{parent_id}' AND id != '{parent_id}' "
        "AND type IN ('h', 'p', 'l', 'i') AND content LIKE '%http%' "
        "ORDER BY created ASC"
    )
    req = urllib.request.Request(
        f"{siyuan_url.rstrip('/')}/api/query/sql",
        data=json.dumps({"stmt": stmt}).encode(),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Token {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode())
    if payload.get("code") != 0:
        raise RuntimeError(f"SiYuan query failed: {str(payload)[:300]}")
    return payload.get("data") or []


def created_at_from_block_id(block_id: str, fallback: str | None) -> tuple[str | None, bool]:
    """Return (ISO timestamp, used_fallback). See module docstring on ordering."""
    match = BLOCK_ID_TS_RE.match(block_id or "")
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y%m%d%H%M%S").isoformat(), False
        except ValueError:
            pass
    if fallback:
        match = BLOCK_ID_TS_RE.match(str(fallback) + "-")
        if match:
            try:
                return datetime.strptime(match.group(1), "%Y%m%d%H%M%S").isoformat(), True
            except ValueError:
                pass
    return None, True


def plan(blocks: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split blocks into (rows to insert, problems). One row per URL."""
    rows: list[dict] = []
    problems: list[dict] = []
    for block in blocks:
        block_id = block.get("id") or ""
        content = block.get("content") or ""
        urls = []
        seen: set[str] = set()
        for raw in URL_RE.findall(content):
            cleaned = raw.rstrip(".,;:!?)\"'`")
            if cleaned not in seen:
                seen.add(cleaned)
                urls.append(cleaned)
        if not urls:
            # The gather predicate matched '%http%' but no parseable URL came
            # out. Report it - this is exactly the "counted as one entry when
            # it was two" class of failure step 4 of the design note guards.
            problems.append({"block_id": block_id, "reason": "no parseable URL", "content": content[:200]})
            continue
        created_at, used_fallback = created_at_from_block_id(block_id, block.get("created"))
        if created_at is None:
            problems.append({"block_id": block_id, "reason": "unparseable timestamp", "content": content[:200]})
            continue
        if used_fallback:
            problems.append({"block_id": block_id, "reason": "timestamp from SiYuan created column, not block id"})
        # URLs from one block share an entry_group - requirement 5.
        group = str(uuid.uuid4())
        for url in urls:
            rows.append({
                "url": url,
                "legacy_block_id": block_id,
                "entry_group": group,
                "created_at": created_at,
            })
    return rows, problems


def insert_rows(conn, rows: list[dict]) -> tuple[int, int]:
    """Insert rows, skipping ones already migrated. Returns (inserted, skipped)."""
    cur = conn.cursor()
    inserted = 0
    for row in rows:
        cur.execute(
            "INSERT INTO chassis_pacman_queue "
            "(token, url, source, source_ref, entry_group, created_at, legacy_block_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (legacy_block_id, url) WHERE legacy_block_id IS NOT NULL "
            "DO NOTHING RETURNING token",
            (
                new_token(),
                row["url"],
                MIGRATION_SOURCE,
                row["legacy_block_id"],
                row["entry_group"],
                row["created_at"],
                row["legacy_block_id"],
            ),
        )
        if cur.fetchone():
            inserted += 1
    conn.commit()
    return inserted, len(rows) - inserted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill the SiYuan Pacman queue into Postgres.")
    parser.add_argument("--dry-run", action="store_true", help="Read and report, insert nothing.")
    parser.add_argument("--snapshot", type=Path, default=None, help="Write the raw SiYuan rows here before inserting.")
    args = parser.parse_args(argv)

    token = os.environ.get("SIYUAN_TOKEN")
    parent_id = os.environ.get("PACMAN_SIYUAN_QUEUE_BLOCK_ID")
    siyuan_url = os.environ.get("SIYUAN_URL", DEFAULT_SIYUAN_URL)
    if not token or not parent_id:
        print("ERROR: SIYUAN_TOKEN and PACMAN_SIYUAN_QUEUE_BLOCK_ID must both be set.", file=sys.stderr)
        return 2

    try:
        blocks = fetch_queue_blocks(siyuan_url, token, parent_id)
    except Exception as exc:
        print(f"ERROR: could not read the SiYuan queue: {exc}", file=sys.stderr)
        return 2

    if args.snapshot:
        args.snapshot.write_text(json.dumps(blocks, indent=2, default=str), encoding="utf-8")
        print(f"snapshot: {len(blocks)} block(s) written to {args.snapshot}")

    rows, problems = plan(blocks)

    print(f"blocks read:      {len(blocks)}")
    print(f"rows to insert:   {len(rows)}")
    print(f"problems:         {len(problems)}")
    for problem in problems:
        print(f"  PROBLEM {problem.get('block_id')}: {problem.get('reason')}", file=sys.stderr)

    if args.dry_run:
        print("dry run - nothing inserted")
        return 1 if problems else 0

    try:
        conn = connect()
    except ChassisDBUnavailable as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    try:
        inserted, skipped = insert_rows(conn, rows)
    finally:
        conn.close()

    print(f"inserted:         {inserted}")
    print(f"already present:  {skipped}")
    print("SiYuan blocks were NOT deleted. Verify a full drain cycle before cleaning them up.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
