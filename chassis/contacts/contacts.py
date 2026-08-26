"""Contacts operations against Postgres.

The whole surface:

    create(name, ...)               insert a contact, returns its id
    get(contact_id)                 resolve by id, tombstone included
    search(query="", ...)           find live contacts by name/email/phone
    update(contact_id, ...)         change fields on a live contact
    delete(contact_id)              soft delete (sets deleted_at)

Relational integrity, per Sean's answers on scrollinondubs/behalfbot#110:
one-way references (everything else points at Postgres, never the reverse),
soft delete only, and a resolver returns a tombstone rather than an error or a
silent empty result. That is why `get()` has no include_deleted flag - it
always returns the row if the id ever existed, live or tombstoned, so a caller
holding a stale id can tell "deleted" apart from "never existed" apart from
"unreachable" (the last one still raises ChassisDBUnavailable, same contract
as every other chassis-core module).

`search()` defaults to excluding tombstoned rows (Sean's stated default) with
an explicit `include_deleted=True` to opt in. There is no hard-delete path in
this slice - GDPR erasure is a real requirement but a separate issue.
"""

from __future__ import annotations

from typing import Any

from chassis.db import ChassisDBUnavailable, connect

TABLE = "chassis_contacts"

ROW_COLUMNS = ("id", "name", "email", "phone", "notes", "source", "created_at", "updated_at", "deleted_at")

UPDATABLE_FIELDS = ("name", "email", "phone", "notes")


class ContactsError(RuntimeError):
    """A contacts operation failed for a reason that is not DB reachability."""


def _connection(conn: Any | None):
    """Use a caller-supplied connection, or open one. Tests supply their own."""
    if conn is not None:
        return conn, False
    return connect(), True


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _row_to_dict(row: tuple) -> dict[str, Any]:
    data = dict(zip(ROW_COLUMNS, row))
    data["created_at"] = _iso(data["created_at"])
    data["updated_at"] = _iso(data["updated_at"])
    data["deleted_at"] = _iso(data["deleted_at"])
    return data


def create(
    name: str,
    *,
    email: str | None = None,
    phone: str | None = None,
    notes: str | None = None,
    source: str = "manual",
    conn: Any | None = None,
) -> int:
    """Insert a contact. Returns its id.

    Name is the one required field - everything else is optional because a
    contact captured from a Discord mention or a business card photo may only
    have a name at first. Email is deliberately not unique here: see the
    migration comment for why (no-email and shared-email contacts are normal).
    """
    if not name or not name.strip():
        raise ContactsError("name is required")

    connection, owned = _connection(conn)
    try:
        cur = connection.cursor()
        cur.execute(
            f"INSERT INTO {TABLE} (name, email, phone, notes, source) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (name.strip(), email, phone, notes, source),
        )
        row = cur.fetchone()
        connection.commit()
        return int(row[0])
    finally:
        if owned:
            connection.close()


def get(contact_id: int, *, conn: Any | None = None) -> dict[str, Any] | None:
    """Resolve a contact by id.

    Returns the row whether it is live or tombstoned - `deleted_at` in the
    result tells the caller which. Returns None only when the id never
    existed at all, which is the "dangling reference" case the issue asked
    about: a note or Discord message pointing at a real, deleted contact gets
    a tombstone back and can say so; one pointing at an id that was never
    valid gets None and can say that instead.
    """
    connection, owned = _connection(conn)
    try:
        cur = connection.cursor()
        cur.execute(
            f"SELECT {', '.join(ROW_COLUMNS)} FROM {TABLE} WHERE id = %s",
            (contact_id,),
        )
        row = cur.fetchone()
        return _row_to_dict(row) if row else None
    finally:
        if owned:
            connection.close()


def search(
    query: str = "",
    *,
    include_deleted: bool = False,
    limit: int = 25,
    conn: Any | None = None,
) -> list[dict[str, Any]]:
    """Find contacts by name/email/phone. Empty query lists, newest first.

    Excludes tombstoned contacts by default - Sean's stated default in the
    issue thread. Pass include_deleted=True to see them too (e.g. to confirm
    a delete happened, or to audit).
    """
    where = ["deleted_at IS NULL"] if not include_deleted else []
    params: list[Any] = []
    if query.strip():
        where.append("(name ILIKE %s OR email ILIKE %s OR phone ILIKE %s)")
        needle = f"%{query.strip()}%"
        params.extend([needle, needle, needle])

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    params.append(limit)

    connection, owned = _connection(conn)
    try:
        cur = connection.cursor()
        cur.execute(
            f"SELECT {', '.join(ROW_COLUMNS)} FROM {TABLE} {clause} "
            "ORDER BY created_at DESC, id DESC LIMIT %s",
            tuple(params),
        )
        return [_row_to_dict(row) for row in cur.fetchall()]
    finally:
        if owned:
            connection.close()


def update(
    contact_id: int,
    *,
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    notes: str | None = None,
    conn: Any | None = None,
) -> bool:
    """Change fields on a live contact. Returns False if it is deleted or missing.

    Only fields explicitly passed get touched - None means "leave alone", not
    "clear this field", matching the CLI's optional-flag shape. A tombstoned
    contact cannot be updated; nothing outside Postgres writes contact fields
    (per the issue's third open question), and letting an update silently
    resurrect a deleted row would violate that.
    """
    fields = {"name": name, "email": email, "phone": phone, "notes": notes}
    changes = {k: v for k, v in fields.items() if v is not None}
    if not changes:
        raise ContactsError("update requires at least one field")

    set_clause = ", ".join(f"{col} = %s" for col in changes)
    params = list(changes.values()) + [contact_id]

    connection, owned = _connection(conn)
    try:
        cur = connection.cursor()
        cur.execute(
            f"UPDATE {TABLE} SET {set_clause}, updated_at = NOW() "
            "WHERE id = %s AND deleted_at IS NULL",
            params,
        )
        changed = cur.rowcount
        connection.commit()
        return changed > 0
    finally:
        if owned:
            connection.close()


def delete(contact_id: int, *, conn: Any | None = None) -> bool:
    """Soft delete: set deleted_at. Returns False if already deleted or missing.

    No hard-delete path in this slice - see the migration comment. The row
    stays put so `get()` keeps resolving it as a tombstone.
    """
    connection, owned = _connection(conn)
    try:
        cur = connection.cursor()
        cur.execute(
            f"UPDATE {TABLE} SET deleted_at = NOW() WHERE id = %s AND deleted_at IS NULL",
            (contact_id,),
        )
        changed = cur.rowcount
        connection.commit()
        return changed > 0
    finally:
        if owned:
            connection.close()


__all__ = [
    "ChassisDBUnavailable",
    "ContactsError",
    "create",
    "delete",
    "get",
    "search",
    "update",
]
