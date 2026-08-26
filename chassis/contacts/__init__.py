"""chassis.contacts - contacts as a first-class Postgres entity.

Small on purpose, matching chassis/pacman/queue.py's shape. See
chassis/contacts/contacts.py for the operations and chassis/db/migrations/
002_contacts.sql for the schema and the design tradeoffs behind it.
"""

from chassis.contacts.contacts import (
    ContactsError,
    create,
    delete,
    get,
    search,
    update,
)

__all__ = [
    "ContactsError",
    "create",
    "delete",
    "get",
    "search",
    "update",
]
