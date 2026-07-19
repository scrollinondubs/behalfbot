"""Pacman queue operations against Postgres.

Replaces the SiYuan-block-backed queue. Every operation here is one round
trip and none of them touch a second brain - see docs/pacman-queue-storage.md
for why the queue is not notes.

The whole surface, in the order the pipeline uses it:

    add(url, source=...)        enqueue, returns the approval token
    pending_count()             cheap gate for the dispatcher tick
    claim(limit)                dequeue oldest-first, marks claimed_at
    complete(token, verdict)    exactly-once removal from the pending set
    release(token)              hand a claimed row back, for a drain that bailed
    pending(limit)              read-only listing, for the installer

Two predicates matter and they must agree: `pending_count` must count exactly
the rows `claim` would hand out, or the dispatcher fires Claude for work that
does not exist (wasted tokens) or skips work that does (a stuck queue). They
share PENDING_PREDICATE below for that reason, and a test asserts both SQL
strings contain it.

Stale claims: a drain that dies mid-batch leaves rows with claimed_at set and
processed_at null. Those become claimable again after PACMAN_CLAIM_TIMEOUT_MINUTES
(default 60) rather than being stranded forever. The old SiYuan design could
not express "being processed" at all, so a dead drain either lost URLs or
reprocessed them; this is the fix for that.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Iterable

from chassis.db import ChassisDBUnavailable, connect
from chassis.pacman.tokens import new_token

TABLE = "chassis_pacman_queue"

VALID_VERDICTS = ("drop", "proposal", "fetch_failed")

DEFAULT_CLAIM_TIMEOUT_MINUTES = 60

# A row is available for work when it has not been processed AND it is not
# currently claimed by a live drain. `%(stale)s` is the reclaim cutoff in
# minutes; it is a parameter rather than a literal so count and claim get the
# same value from the same helper.
PENDING_PREDICATE = (
    "processed_at IS NULL "
    "AND (claimed_at IS NULL OR claimed_at < NOW() - (%(stale)s || ' minutes')::interval)"
)

# Insert retries on token collision. 19**6 against a queue of tens means a
# collision is vanishingly unlikely, but the UNIQUE constraint is the authority
# and this loop is what respects it.
TOKEN_INSERT_ATTEMPTS = 5


class QueueError(RuntimeError):
    """A queue operation failed for a reason that is not DB reachability."""


def claim_timeout_minutes(env: dict[str, str] | None = None) -> int:
    """Minutes after which a claimed-but-unprocessed row is reclaimable."""
    source = env if env is not None else os.environ
    raw = (source.get("PACMAN_CLAIM_TIMEOUT_MINUTES") or "").strip()
    if not raw:
        return DEFAULT_CLAIM_TIMEOUT_MINUTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_CLAIM_TIMEOUT_MINUTES
    return value if value > 0 else DEFAULT_CLAIM_TIMEOUT_MINUTES


def _connection(conn: Any | None):
    """Use a caller-supplied connection, or open one. Tests supply their own."""
    if conn is not None:
        return conn, False
    return connect(), True


def add(
    url: str,
    *,
    source: str = "manual",
    source_ref: str | None = None,
    entry_group: str | None = None,
    conn: Any | None = None,
) -> str:
    """Enqueue one URL. Returns its approval token.

    Raises on failure rather than swallowing it. The previous SiYuan helper was
    explicitly "designed to fail silently (caller continues if URL append
    fails)", which was survivable when the URL was also sitting in a Discord
    message the installer could scroll back to. It is not survivable now: the
    queue row is the only durable record, so the write has to succeed before
    anything acknowledges the submission.
    """
    if not url.lower().startswith(("http://", "https://")):
        raise QueueError(f"not an http(s) URL: {url[:200]}")

    group = entry_group or str(uuid.uuid4())
    connection, owned = _connection(conn)
    try:
        cur = connection.cursor()
        last_exc: Exception | None = None
        for _ in range(TOKEN_INSERT_ATTEMPTS):
            token = new_token()
            try:
                cur.execute(
                    f"INSERT INTO {TABLE} (token, url, source, source_ref, entry_group) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING token",
                    (token, url, source, source_ref, group),
                )
                row = cur.fetchone()
                connection.commit()
                return row[0] if row else token
            except Exception as exc:  # token collision or a real error
                connection.rollback()
                last_exc = exc
                if "token" not in str(exc).lower():
                    raise
        raise QueueError(f"could not allocate a unique approval token: {last_exc}")
    finally:
        if owned:
            connection.close()


def add_many(
    urls: Iterable[str],
    *,
    source: str = "manual",
    source_ref: str | None = None,
    conn: Any | None = None,
) -> list[str]:
    """Enqueue several URLs as one entry group. Returns tokens in input order.

    Requirement 5 from the design note: URLs pasted in one message belong
    together, and the group is only fully done once every URL in it is done.
    """
    group = str(uuid.uuid4())
    connection, owned = _connection(conn)
    try:
        return [
            add(u, source=source, source_ref=source_ref, entry_group=group, conn=connection)
            for u in urls
        ]
    finally:
        if owned:
            connection.close()


def pending_count(*, conn: Any | None = None, env: dict[str, str] | None = None) -> int:
    """Count rows a drain could pick up right now. Runs every dispatcher tick."""
    connection, owned = _connection(conn)
    try:
        cur = connection.cursor()
        cur.execute(
            f"SELECT COUNT(*) FROM {TABLE} WHERE {PENDING_PREDICATE}",
            {"stale": claim_timeout_minutes(env)},
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0
    finally:
        if owned:
            connection.close()


def pending(
    limit: int = 25, *, conn: Any | None = None, env: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """List pending rows without claiming them.

    This is what replaces the installer's ability to open /To Investigate and
    see what is queued. The design note flags that visibility as a real feature
    being traded away; this is the trade being paid back.
    """
    connection, owned = _connection(conn)
    try:
        cur = connection.cursor()
        cur.execute(
            f"SELECT token, url, source, created_at, claimed_at FROM {TABLE} "
            f"WHERE {PENDING_PREDICATE} ORDER BY created_at ASC, id ASC LIMIT %(limit)s",
            {"stale": claim_timeout_minutes(env), "limit": limit},
        )
        return [
            {
                "token": r[0],
                "url": r[1],
                "source": r[2],
                "created_at": r[3].isoformat() if hasattr(r[3], "isoformat") else r[3],
                "claimed_at": r[4].isoformat() if hasattr(r[4], "isoformat") else r[4],
            }
            for r in cur.fetchall()
        ]
    finally:
        if owned:
            connection.close()


def claim(
    limit: int = 10, *, conn: Any | None = None, env: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """Dequeue up to `limit` rows oldest-first and mark them claimed.

    FOR UPDATE SKIP LOCKED makes two concurrent drains safe - they take
    disjoint batches instead of both taking the same one. The SiYuan design
    could not express this at all, so overlapping drains double-processed.

    Two ordering subtleties, both found by the live-Postgres test in
    tests/test_queue.py and neither visible to a fake connection:

    1. `UPDATE ... RETURNING` does NOT preserve the inner subquery's ORDER BY.
       Rows come back in update order, which is unspecified. The first version
       of this returned a rotated batch, so the drain processed out of FIFO
       order while every non-DB test passed. Hence the CTE: the ordering that
       the caller sees has to be applied in an outer SELECT.
    2. `created_at` alone is not a total order. `add_many` inserts a pasted
       batch in a tight loop, and two rows can land in the same microsecond;
       the tie then breaks arbitrarily. `id` is BIGSERIAL and monotonic, so it
       is the real insertion order and the correct tiebreak.
    """
    connection, owned = _connection(conn)
    try:
        cur = connection.cursor()
        cur.execute(
            "WITH claimed AS ("
            f"  UPDATE {TABLE} SET claimed_at = NOW() WHERE id IN ("
            f"    SELECT id FROM {TABLE} WHERE {PENDING_PREDICATE}"
            "     ORDER BY created_at ASC, id ASC LIMIT %(limit)s FOR UPDATE SKIP LOCKED"
            "  ) RETURNING id, token, url, source, entry_group, created_at"
            ") SELECT token, url, source, entry_group, created_at FROM claimed "
            "ORDER BY created_at ASC, id ASC",
            {"stale": claim_timeout_minutes(env), "limit": limit},
        )
        rows = cur.fetchall()
        connection.commit()
        return [
            {
                "token": r[0],
                "url": r[1],
                "source": r[2],
                "entry_group": str(r[3]),
                "created_at": r[4].isoformat() if hasattr(r[4], "isoformat") else r[4],
            }
            for r in rows
        ]
    finally:
        if owned:
            connection.close()


def complete(
    token: str,
    verdict: str,
    *,
    gate: int | None = None,
    proposal_doc_id: str | None = None,
    conn: Any | None = None,
) -> bool:
    """Mark a row processed. Returns False if the token was already processed.

    This is the operation the skill calls non-negotiable: an entry that
    survives processing gets re-processed forever. `processed_at IS NULL` in
    the WHERE clause is what makes it exactly-once - a second call for the same
    token updates zero rows and returns False rather than resetting the verdict.
    """
    if verdict not in VALID_VERDICTS:
        raise QueueError(f"verdict must be one of {VALID_VERDICTS}, got {verdict!r}")

    connection, owned = _connection(conn)
    try:
        cur = connection.cursor()
        cur.execute(
            f"UPDATE {TABLE} SET processed_at = NOW(), verdict = %s, gate = %s, "
            "proposal_doc_id = %s WHERE token = %s AND processed_at IS NULL",
            (verdict, gate, proposal_doc_id, token),
        )
        changed = cur.rowcount
        connection.commit()
        return changed > 0
    finally:
        if owned:
            connection.close()


def release(token: str, *, conn: Any | None = None) -> bool:
    """Un-claim a row so the next drain picks it up. For a drain that bailed."""
    connection, owned = _connection(conn)
    try:
        cur = connection.cursor()
        cur.execute(
            f"UPDATE {TABLE} SET claimed_at = NULL WHERE token = %s AND processed_at IS NULL",
            (token,),
        )
        changed = cur.rowcount
        connection.commit()
        return changed > 0
    finally:
        if owned:
            connection.close()


__all__ = [
    "ChassisDBUnavailable",
    "PENDING_PREDICATE",
    "QueueError",
    "add",
    "add_many",
    "claim",
    "claim_timeout_minutes",
    "complete",
    "pending",
    "pending_count",
    "release",
]
