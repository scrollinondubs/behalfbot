#!/usr/bin/env python3
"""contacts.py - CLI over the Postgres-backed contacts table.

Slice 1 of scrollinondubs/behalfbot#110: a contacts table plus a minimal
read/write/search surface, shipped as a CLI first. An MCP server surface over
the same chassis/contacts module is out of scope for this PR - see the issue
for why (ship the CLI, add MCP once the shape is proven).

Subcommands:
    add <name> [--email E] [--phone P] [--notes N] [--source S]
    get <id>
    search [query] [--include-deleted] [--limit N]
    update <id> [--name N] [--email E] [--phone P] [--notes N]
    delete <id>

Every subcommand exits non-zero and prints to stderr when Postgres is
unreachable, same contract as chassis/scripts/pacman-queue.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PACKAGE_PARENT = Path(__file__).resolve().parents[2]
if str(_PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_PARENT))

from chassis import contacts  # noqa: E402
from chassis.db import ChassisDBUnavailable  # noqa: E402


def _cmd_add(args) -> int:
    contact_id = contacts.create(
        args.name,
        email=args.email,
        phone=args.phone,
        notes=args.notes,
        source=args.source,
    )
    print(json.dumps({"id": contact_id}))
    return 0


def _cmd_get(args) -> int:
    row = contacts.get(args.id)
    if row is None:
        print(json.dumps({"id": args.id, "error": "not found"}))
        return 1
    print(json.dumps(row, indent=2))
    return 0


def _cmd_search(args) -> int:
    rows = contacts.search(args.query, include_deleted=args.include_deleted, limit=args.limit)
    print(json.dumps(rows, indent=2))
    return 0


def _cmd_update(args) -> int:
    changed = contacts.update(
        args.id,
        name=args.name,
        email=args.email,
        phone=args.phone,
        notes=args.notes,
    )
    print(json.dumps({"id": args.id, "changed": changed}))
    return 0 if changed else 1


def _cmd_delete(args) -> int:
    changed = contacts.delete(args.id)
    print(json.dumps({"id": args.id, "deleted": changed}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Contacts operations.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Create a contact, print its id.")
    p_add.add_argument("name")
    p_add.add_argument("--email", default=None)
    p_add.add_argument("--phone", default=None)
    p_add.add_argument("--notes", default=None)
    p_add.add_argument("--source", default="manual")
    p_add.set_defaults(func=_cmd_add)

    p_get = sub.add_parser("get", help="Resolve a contact by id (tombstone included).")
    p_get.add_argument("id", type=int)
    p_get.set_defaults(func=_cmd_get)

    p_search = sub.add_parser("search", help="Find contacts by name/email/phone.")
    p_search.add_argument("query", nargs="?", default="")
    p_search.add_argument("--include-deleted", action="store_true")
    p_search.add_argument("--limit", type=int, default=25)
    p_search.set_defaults(func=_cmd_search)

    p_update = sub.add_parser("update", help="Change fields on a live contact.")
    p_update.add_argument("id", type=int)
    p_update.add_argument("--name", default=None)
    p_update.add_argument("--email", default=None)
    p_update.add_argument("--phone", default=None)
    p_update.add_argument("--notes", default=None)
    p_update.set_defaults(func=_cmd_update)

    p_delete = sub.add_parser("delete", help="Soft delete a contact.")
    p_delete.add_argument("id", type=int)
    p_delete.set_defaults(func=_cmd_delete)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ChassisDBUnavailable as exc:
        print(f"ERROR: contacts store unavailable: {exc}", file=sys.stderr)
        return 2
    except contacts.ContactsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
