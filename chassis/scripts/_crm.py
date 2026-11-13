"""CRM: contacts, lists and memberships in the chassis Postgres.

A list is anything people are tracked against (an event, an outreach batch).
Status, headcount and follow-up dates sit on the membership, so a contact is
entered once and reused by every list. Schema: chassis/db/migrations/003_crm.sql.

The dashboard tab (chassis/dashboard/core/crm.py) is the main caller; an
install-side import script can use Store directly. Connection and DSN handling
come from chassis.db, so the env vars are the ones it documents
(CHASSIS_PG_DSN, then BEHALFBOT_PG_DSN, then JAX_PG_DSN).
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))
from chassis.db.connection import DSN_ENV_VARS, ChassisDBUnavailable  # noqa: E402

STATUSES = ("invited", "yes", "maybe", "no", "unknown")
COMING = ("yes", "maybe")
MAX_HEADCOUNT = 999
MAX_TEXT = 4000
CONTACT_FIELDS = ("name", "email", "phone", "whatsapp", "notes")
LIST_FIELDS = ("name", "event_date", "notes")
MEMBER_FIELDS = ("status", "headcount", "channel", "invited_on", "last_followup_on", "notes")

UNDEFINED_TABLE = "42P01"


class CrmError(ValueError):
    """Bad input, or a change the data does not allow. Shown to the user."""


class CrmUnavailable(RuntimeError):
    """The CRM cannot reach its tables. reason is one of chassis.db's
    ('unconfigured', 'driver_missing', 'unreachable') or 'no_tables'."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


# ---- input cleaning ------------------------------------------------------------

def _text(value: Any, label: str, required: bool = False) -> str | None:
    text = "" if value is None else str(value).strip()
    if not text:
        if required:
            raise CrmError(f"{label} is required")
        return None
    if len(text) > MAX_TEXT:
        raise CrmError(f"{label} is too long")
    return text


def _date(value: Any, label: str) -> date | None:
    text = _text(value, label)
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise CrmError(f"{label} must be a date like 2026-01-31") from None


def clean_contact(form: dict) -> dict:
    out = {f: _text(form.get(f), f.capitalize()) for f in CONTACT_FIELDS}
    out["name"] = _text(form.get("name"), "Name", required=True)
    return out


def clean_list(form: dict) -> dict:
    return {"name": _text(form.get("name"), "Name", required=True),
            "event_date": _date(form.get("event_date"), "Date"),
            "notes": _text(form.get("notes"), "Notes")}


def clean_member(form: dict, partial: bool = False) -> dict:
    """Validated membership fields. partial keeps only the fields the form sent."""
    status = (form.get("status") or "invited").strip().lower()
    if status not in STATUSES:
        raise CrmError(f"status must be one of {', '.join(STATUSES)}")
    raw = (form.get("headcount") or "").strip()
    if not raw:
        headcount = 1
    else:
        try:
            headcount = int(raw)
        except ValueError:
            raise CrmError("headcount must be a whole number") from None
        if not 0 <= headcount <= MAX_HEADCOUNT:
            raise CrmError(f"headcount must be between 0 and {MAX_HEADCOUNT}")
    out = {"status": status, "headcount": headcount,
           "channel": _text(form.get("channel"), "Channel"),
           "invited_on": _date(form.get("invited_on"), "Invited on"),
           "last_followup_on": _date(form.get("last_followup_on"), "Last follow-up"),
           "notes": _text(form.get("notes"), "Notes")}
    return {k: v for k, v in out.items() if k in form} if partial else out


def parse_id(value: Any, label: str = "id") -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        raise CrmError(f"{label} is not valid") from None
    if n <= 0:
        raise CrmError(f"{label} is not valid")
    return n


def summarize(members: list[dict]) -> dict:
    """Counts per status, and how many people the yes and maybe answers add up to."""
    counts = {s: 0 for s in STATUSES}
    heads = {s: 0 for s in STATUSES}
    for m in members:
        s = m.get("status") if m.get("status") in counts else "unknown"
        counts[s] += 1
        heads[s] += int(m.get("headcount") or 0)
    return {"counts": counts, "total": len(members),
            "headcount_yes": heads["yes"], "headcount_maybe": heads["maybe"],
            "headcount_coming": heads["yes"] + heads["maybe"]}


# ---- store ---------------------------------------------------------------------

def default_connect():
    from chassis.db.connection import connect
    return connect(dict_rows=True)


def unavailable_hint(reason: str) -> str:
    if reason == "unconfigured":
        return (f"Set {DSN_ENV_VARS[0]} (or {' / '.join(DSN_ENV_VARS[1:])}) in the dashboard's "
                "environment to a Postgres DSN reachable from where the dashboard runs, then restart it.")
    if reason == "driver_missing":
        return "Install the driver for the python that runs the dashboard: pip install 'psycopg[binary]'."
    if reason == "no_tables":
        return "The CRM tables are missing. From the chassis checkout, with the same DSN set, run: python3 -m chassis.db.migrate"
    return "Check that Postgres is running and that the DSN host and port are reachable from the dashboard."


class Store:
    """One short connection per call. connect returns a psycopg connection
    with dict rows; tests and other apps can pass their own."""

    def __init__(self, connect: Callable[[], Any] | None = None) -> None:
        self._connect = connect or default_connect

    def _run(self, fn: Callable[[Any], Any]) -> Any:
        try:
            conn = self._connect()
        except ChassisDBUnavailable as e:
            raise CrmUnavailable(str(e), e.reason) from e
        try:
            with conn.cursor() as cur:
                result = fn(cur)
            conn.commit()
            return result
        except Exception as e:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            state = getattr(e, "sqlstate", None) or ""
            if state == UNDEFINED_TABLE:
                raise CrmUnavailable(str(e), "no_tables") from e
            if state.startswith("23"):
                raise CrmError("that change conflicts with the data already stored") from e
            if state.startswith("08") or type(e).__name__ == "OperationalError":
                raise CrmUnavailable(str(e), "unreachable") from e
            raise
        finally:
            conn.close()

    def check(self) -> None:
        self._run(lambda cur: cur.execute(
            "SELECT 1 FROM crm_contacts, crm_lists, crm_memberships LIMIT 0"))

    # contacts

    def contacts(self, q: str = "", limit: int = 1000) -> list[dict]:
        where, params = "", []
        if q.strip():
            needle = f"%{q.strip()}%"
            where = ("WHERE c.name ILIKE %s OR c.email ILIKE %s OR c.phone ILIKE %s "
                     "OR c.whatsapp ILIKE %s OR c.notes ILIKE %s")
            params = [needle] * 5

        def go(cur):
            cur.execute(
                "SELECT c.id, c.name, c.email, c.phone, c.whatsapp, c.notes, "
                "(SELECT count(*) FROM crm_memberships m WHERE m.contact_id = c.id) AS list_count "
                f"FROM crm_contacts c {where} ORDER BY lower(c.name), c.id LIMIT %s",
                (*params, limit))
            return cur.fetchall()
        return self._run(go)

    def contact(self, contact_id: int) -> dict | None:
        def go(cur):
            cur.execute("SELECT id, name, email, phone, whatsapp, notes, created_at, updated_at "
                        "FROM crm_contacts WHERE id = %s", (contact_id,))
            return cur.fetchone()
        return self._run(go)

    def add_contact(self, fields: dict) -> int:
        def go(cur):
            cur.execute("INSERT INTO crm_contacts (name, email, phone, whatsapp, notes) "
                        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                        tuple(fields.get(f) for f in CONTACT_FIELDS))
            return int(cur.fetchone()["id"])
        return self._run(go)

    def update_contact(self, contact_id: int, fields: dict) -> bool:
        def go(cur):
            cur.execute("UPDATE crm_contacts SET name = %s, email = %s, phone = %s, whatsapp = %s, "
                        "notes = %s, updated_at = NOW() WHERE id = %s",
                        (*(fields.get(f) for f in CONTACT_FIELDS), contact_id))
            return cur.rowcount > 0
        return self._run(go)

    def delete_contact(self, contact_id: int) -> bool:
        def go(cur):
            cur.execute("DELETE FROM crm_contacts WHERE id = %s", (contact_id,))
            return cur.rowcount > 0
        return self._run(go)

    def memberships_of(self, contact_id: int) -> list[dict]:
        def go(cur):
            cur.execute("SELECT l.id AS list_id, l.name AS list_name, l.event_date, m.status, m.headcount "
                        "FROM crm_memberships m JOIN crm_lists l ON l.id = m.list_id "
                        "WHERE m.contact_id = %s ORDER BY l.event_date DESC NULLS LAST, l.id DESC",
                        (contact_id,))
            return cur.fetchall()
        return self._run(go)

    # lists

    def lists(self) -> list[dict]:
        def go(cur):
            cur.execute(
                "SELECT l.id, l.name, l.event_date, l.notes, count(m.id) AS members, "
                "coalesce(sum(m.headcount) FILTER (WHERE m.status = 'yes'), 0) AS headcount_yes, "
                "coalesce(sum(m.headcount) FILTER (WHERE m.status = 'maybe'), 0) AS headcount_maybe "
                "FROM crm_lists l LEFT JOIN crm_memberships m ON m.list_id = l.id "
                "GROUP BY l.id ORDER BY l.event_date DESC NULLS LAST, l.id DESC")
            return cur.fetchall()
        return self._run(go)

    def get_list(self, list_id: int) -> dict | None:
        def go(cur):
            cur.execute("SELECT id, name, event_date, notes FROM crm_lists WHERE id = %s", (list_id,))
            return cur.fetchone()
        return self._run(go)

    def add_list(self, fields: dict) -> int:
        def go(cur):
            cur.execute("INSERT INTO crm_lists (name, event_date, notes) VALUES (%s, %s, %s) RETURNING id",
                        tuple(fields.get(f) for f in LIST_FIELDS))
            return int(cur.fetchone()["id"])
        return self._run(go)

    def update_list(self, list_id: int, fields: dict) -> bool:
        def go(cur):
            cur.execute("UPDATE crm_lists SET name = %s, event_date = %s, notes = %s, updated_at = NOW() "
                        "WHERE id = %s", (*(fields.get(f) for f in LIST_FIELDS), list_id))
            return cur.rowcount > 0
        return self._run(go)

    def delete_list(self, list_id: int) -> bool:
        def go(cur):
            cur.execute("DELETE FROM crm_lists WHERE id = %s", (list_id,))
            return cur.rowcount > 0
        return self._run(go)

    # memberships

    def members(self, list_id: int) -> list[dict]:
        def go(cur):
            cur.execute(
                "SELECT m.contact_id, c.name, c.email, c.phone, c.whatsapp, m.status, m.headcount, "
                "m.channel, m.invited_on, m.last_followup_on, m.notes, m.raw_source "
                "FROM crm_memberships m JOIN crm_contacts c ON c.id = m.contact_id "
                "WHERE m.list_id = %s ORDER BY lower(c.name), c.id", (list_id,))
            return cur.fetchall()
        return self._run(go)

    def add_member(self, list_id: int, contact_id: int, fields: dict | None = None) -> bool:
        """False when the contact is already on the list."""
        f = fields or clean_member({})

        def go(cur):
            cur.execute(
                "INSERT INTO crm_memberships (list_id, contact_id, status, headcount, channel, "
                "invited_on, last_followup_on, notes) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (list_id, contact_id) DO NOTHING",
                (list_id, contact_id, *(f.get(k) for k in MEMBER_FIELDS)))
            return cur.rowcount > 0
        return self._run(go)

    def update_member(self, list_id: int, contact_id: int, fields: dict) -> bool:
        """Changes only the fields given, so a form that leaves one out keeps it."""
        cols = [k for k in MEMBER_FIELDS if k in fields]
        if not cols:
            raise CrmError("nothing to change")

        def go(cur):
            cur.execute(
                f"UPDATE crm_memberships SET {', '.join(f'{k} = %s' for k in cols)}, updated_at = NOW() "
                "WHERE list_id = %s AND contact_id = %s",
                (*(fields[k] for k in cols), list_id, contact_id))
            return cur.rowcount > 0
        return self._run(go)

    def remove_member(self, list_id: int, contact_id: int) -> bool:
        def go(cur):
            cur.execute("DELETE FROM crm_memberships WHERE list_id = %s AND contact_id = %s",
                        (list_id, contact_id))
            return cur.rowcount > 0
        return self._run(go)
