"""chassis.db - Postgres access for chassis-core code.

Small on purpose. Three things: resolve a DSN, open a connection, apply
migrations. Anything richer belongs in the module that needs it.

See chassis/db/connection.py for the DSN resolution order and the reasoning
about failure modes, and docs/pacman-queue-storage.md section 5 for why this
module had to exist before the Pacman queue could move.
"""

from chassis.db.connection import (
    DSN_ENV_VARS,
    ChassisDBUnavailable,
    connect,
    get_dsn,
    is_configured,
)
from chassis.db.migrate import MIGRATIONS_DIR, apply_migrations, discover_migrations

__all__ = [
    "DSN_ENV_VARS",
    "ChassisDBUnavailable",
    "MIGRATIONS_DIR",
    "apply_migrations",
    "connect",
    "discover_migrations",
    "get_dsn",
    "is_configured",
]
