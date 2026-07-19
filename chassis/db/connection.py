"""Chassis-core Postgres connection handling.

This is the module `plugins/dating/scripts/_chassis_db.py` says is missing:
"Chassis V1 has no shared DB abstraction yet - each plugin that touches a DB
carries its own selector until the chassis grows one." Pacman is chassis-core,
not a plugin, so it cannot carry a third copy of that selector.

Deliberately NOT an ORM and deliberately not a backend selector. The chassis
already decided Postgres is canonical (Sean's "Postgres from start" call,
docker-compose.yml line 9). The dating plugin's selector exists because BFL
predates that call and still has a SQLite mirror to keep alive; core code has
no such history and should not inherit the branch.

What this module owns:
  - DSN resolution, one order, documented below.
  - A connection helper that turns every reachability failure into one
    exception type with an actionable message.
  - Nothing else. Migrations live in chassis/db/migrate.py.

DSN resolution order (first non-empty wins):
  1. CHASSIS_PG_DSN      - set by docker-compose for the chassis container.
  2. BEHALFBOT_PG_DSN    - the name the dating plugin's selector reads first,
                           accepted here so an install that set only that one
                           does not need a second variable.
  3. JAX_PG_DSN          - host-side legacy fallback, per the reference
                           install's docs/jax-db.md. Present so host-run
                           scripts (migrations, one-off drains) work outside
                           the container without re-exporting.

Failure mode is the point of this module. A queue that cannot reach Postgres
must fail loudly: the Pacman queue holds URLs that exist nowhere else once the
source Discord or Telegram message scrolls out of reach, so "degrade to zero
results" is data loss wearing a green checkmark. Every path here raises
ChassisDBUnavailable rather than returning an empty result.
"""

from __future__ import annotations

import os
from typing import Any

# Ordered by preference. Kept as a module constant so the error message below
# and the resolution logic cannot drift apart.
DSN_ENV_VARS: tuple[str, ...] = ("CHASSIS_PG_DSN", "BEHALFBOT_PG_DSN", "JAX_PG_DSN")


class ChassisDBUnavailable(RuntimeError):
    """Postgres is not configured, not installed, or not reachable.

    One exception type for all three because every caller does the same thing
    with them: report loudly and stop. Callers that want to distinguish can
    read `.reason` ('unconfigured' | 'driver_missing' | 'unreachable').
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def get_dsn(env: dict[str, str] | None = None) -> str:
    """Resolve the Postgres DSN, or raise ChassisDBUnavailable."""
    source = env if env is not None else os.environ
    for var in DSN_ENV_VARS:
        value = (source.get(var) or "").strip()
        if value:
            return value
    raise ChassisDBUnavailable(
        "No Postgres DSN configured. Set one of "
        + ", ".join(DSN_ENV_VARS)
        + " (format: postgresql://user:PASSWORD@host:5432/dbname). "
        "In a container install this is set by docker-compose; if it is empty "
        "there, check that .env.baked was generated - see docs/credential-bake.md.",
        reason="unconfigured",
    )


def is_configured(env: dict[str, str] | None = None) -> bool:
    """True when a DSN is set. Does not open a connection or prove reachability."""
    try:
        get_dsn(env)
    except ChassisDBUnavailable:
        return False
    return True


def connect(*, dict_rows: bool = False, autocommit: bool = False, dsn: str | None = None) -> Any:
    """Open a Postgres connection, or raise ChassisDBUnavailable.

    Tuple rows by default to match sqlite3 and the dating plugin's helper, so
    `row[0]` means the same thing everywhere in this codebase. Pass
    dict_rows=True where column names carry the meaning.
    """
    resolved = dsn or get_dsn()
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - psycopg is baked into the image
        raise ChassisDBUnavailable(
            "psycopg is not installed. It is pinned in requirements.txt and baked "
            "into the chassis image; if you are running on the host, "
            "pip install 'psycopg[binary]'.",
            reason="driver_missing",
        ) from exc

    kwargs: dict[str, Any] = {"autocommit": autocommit}
    if dict_rows:
        from psycopg.rows import dict_row

        kwargs["row_factory"] = dict_row

    try:
        return psycopg.connect(resolved, **kwargs)
    except Exception as exc:
        raise ChassisDBUnavailable(
            f"Could not connect to Postgres: {exc}. "
            "Check the postgres service is healthy (docker compose ps postgres) "
            "and that the DSN host resolves from where this is running - the "
            "container reaches it as 'postgres:5432', the host as 'localhost'.",
            reason="unreachable",
        ) from exc
