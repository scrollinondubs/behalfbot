"""Shared chassis DB backend selector.

All BFL ingest + enrichment scripts route through this module so the chassis
can switch between Postgres (canonical) and SQLite (legacy / dev) uniformly.

Env vars:
    USE_PG          'true' | 'false' | 'parallel'   (default: 'true')
    BEHALFBOT_PG_DSN | CHASSIS_PG_DSN                   (Postgres DSN)
    BEHALFBOT_SQLITE_PATH | CHASSIS_SQLITE_PATH         (SQLite mirror; dev only)

Canonical usage:

    from _chassis_db import connect, cursor

    conn = connect()
    cur  = cursor(conn)
    cur.execute("SELECT * FROM bfl_meals WHERE day_id = ?", (day_id,))

The cursor wrapper rewrites SQLite-native `?` placeholders to `%s` for psycopg
so a single-source SQL string works on both backends. For dialect-specific
SQL (ON CONFLICT clauses, RETURNING, etc.) callers branch on `is_pg(conn)`.

Why ship this in the plugin (not chassis core)? Chassis V1 has no shared DB
abstraction yet — each plugin that touches a DB carries its own selector
until the chassis grows one. The BFL plugin is the largest DB consumer in
the V1 reference and is the natural place for this to live until the
chassis core grows a `chassis-db` module that owns it.
"""
from __future__ import annotations

import os
import pathlib

# Default SQLite path (legacy / dev only). Override with BEHALFBOT_SQLITE_PATH
# or CHASSIS_SQLITE_PATH (V1-compat alias). The chassis canonical DB is Postgres
# and BFL writes through that path in any production install.
_DEFAULT_SQLITE_PATH = pathlib.Path.home() / "behalfbot" / "data" / "chassis.db"


def _sqlite_path() -> pathlib.Path:
    explicit = os.environ.get("BEHALFBOT_SQLITE_PATH") or os.environ.get("CHASSIS_SQLITE_PATH")
    if explicit:
        return pathlib.Path(explicit).expanduser()
    return _DEFAULT_SQLITE_PATH


def get_backend() -> str:
    """Returns 'sqlite', 'pg', or 'parallel'. Honours USE_PG env var."""
    val = os.environ.get("USE_PG", "true").strip().lower()
    if val in ("1", "true", "yes"):
        return "pg"
    if val == "parallel":
        return "parallel"
    return "sqlite"


def ph() -> str:
    """Parameter placeholder for the active backend."""
    return "%s" if get_backend() == "pg" else "?"


def get_pg_dsn() -> str:
    dsn = os.environ.get("BEHALFBOT_PG_DSN") or os.environ.get("CHASSIS_PG_DSN")
    if dsn:
        return dsn
    raise RuntimeError(
        "BEHALFBOT_PG_DSN (or CHASSIS_PG_DSN) not set. Set it in the chassis env "
        "or export it. Format: postgresql://user:PASSWORD@host:5432/dbname"
    )


def connect_sqlite():
    """SQLite connection with row factory + foreign keys ON.

    Note: V1 reference also loaded sqlite-vec for the embedding paths used by
    the briefing/memory layer. BFL doesn't need that, so the plugin's SQLite
    helper deliberately omits the sqlite_vec.load() call. Plugins that need
    embeddings should layer it on top.
    """
    import sqlite3
    db = sqlite3.connect(_sqlite_path())
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def connect_pg(*, dict_rows: bool = False):
    """Postgres connection via psycopg.

    Defaults to tuple rows (matches sqlite3's default) so existing ingest
    code that accesses `fetchone()[0]` / `row[0]` keeps working. Pass
    `dict_rows=True` for scripts that want dict-like access by column name.
    """
    import psycopg
    if dict_rows:
        from psycopg.rows import dict_row
        return psycopg.connect(get_pg_dsn(), row_factory=dict_row)
    return psycopg.connect(get_pg_dsn())


def connect(*, backend: str | None = None):
    """Default connection for the active backend. Pass backend explicitly
    in parallel mode to open a secondary connection to the other dialect."""
    b = backend or get_backend()
    if b == "pg":
        return connect_pg()
    return connect_sqlite()


def is_pg(conn) -> bool:
    """Detect whether a connection is Postgres vs SQLite by module name."""
    return conn.__class__.__module__.startswith("psycopg")


def exec_sql(conn, sql: str, params: tuple | list = ()):
    """Execute SQL written in SQLite-native `?` placeholder style against
    either backend. When PG, rewrites `?` to `%s` before dispatch.
    """
    if is_pg(conn):
        pg_sql = sql.replace("?", "%s")
        cur = conn.cursor()
        cur.execute(pg_sql, params)
        return cur
    return conn.execute(sql, params)


def exec_many(conn, sql: str, seq):
    if is_pg(conn):
        pg_sql = sql.replace("?", "%s")
        cur = conn.cursor()
        cur.executemany(pg_sql, seq)
        return cur
    return conn.executemany(sql, seq)


class _QmarkCursor:
    """Cursor wrapper that accepts SQLite-native `?` placeholders and
    rewrites them to `%s` before dispatching to psycopg. Matches the
    subset of cursor API the ingest scripts actually use (execute,
    executemany, fetchone, fetchall, lastrowid, __iter__, close).

    `lastrowid`: psycopg doesn't expose lastrowid. Scripts that need it
    should instead use `RETURNING id` in the INSERT.
    """

    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql, params=()):
        self._cur.execute(sql.replace("?", "%s"), params or ())
        return self

    def executemany(self, sql, seq):
        self._cur.executemany(sql.replace("?", "%s"), seq)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def lastrowid(self):
        return None

    @property
    def rowcount(self):
        return self._cur.rowcount

    def __iter__(self):
        return iter(self._cur)

    def close(self):
        self._cur.close()


def cursor(conn, *, dict_rows: bool = False):
    """Return a cursor that accepts `?` placeholders on either backend.
    For SQLite this is a real sqlite3.Cursor; for PG it's a _QmarkCursor
    wrapping psycopg's cursor."""
    if is_pg(conn):
        if dict_rows:
            from psycopg.rows import dict_row
            return _QmarkCursor(conn.cursor(row_factory=dict_row))
        return _QmarkCursor(conn.cursor())
    return conn.cursor()
