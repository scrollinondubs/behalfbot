#!/usr/bin/env python3
"""pacman-queue.py - CLI over the Postgres-backed Pacman queue.

This is the interface the drain prompt drives. The old drain read the queue by
issuing raw SQL through the SiYuan MCP server and deleted entries with its
block-delete tool, which is why Pacman only worked on SiYuan installs and did
nothing at all in adapter mode. Both of those are now shell calls to this
script, which behaves identically on every backend.

Subcommands:
    add <url> [--source S] [--source-ref R]   enqueue, prints the token
    count                                     JSON {"count": N} for the gather gate
    pending [--limit N]                       JSON list, read-only, does not claim
    claim [--limit N]                         JSON list, marks rows claimed
    complete <token> --verdict V [--gate N] [--doc-id D]
    release <token>                           un-claim a row

Every subcommand exits non-zero and prints to stderr when Postgres is
unreachable. That is deliberate: the queue is the only durable record of a
submitted URL, so a silent empty result is data loss with a green checkmark.
`count` is the one place this matters most, because the dispatcher treats a
failed gather as a heartbeat failure and alerts on it.

Usage from the drain prompt:
    python3 "$CHASSIS_ROOT/scripts/pacman-queue.py" claim --limit 10
    python3 "$CHASSIS_ROOT/scripts/pacman-queue.py" complete qhtnbz --verdict drop --gate 2
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PACKAGE_PARENT = Path(__file__).resolve().parents[2]
if str(_PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_PARENT))

from chassis.db import ChassisDBUnavailable  # noqa: E402
from chassis.pacman import queue  # noqa: E402


def _cmd_add(args) -> int:
    token = queue.add(args.url, source=args.source, source_ref=args.source_ref)
    print(token)
    return 0


def _cmd_count(args) -> int:
    print(json.dumps({"count": queue.pending_count()}))
    return 0


def _cmd_pending(args) -> int:
    print(json.dumps(queue.pending(limit=args.limit), indent=2))
    return 0


def _cmd_claim(args) -> int:
    print(json.dumps(queue.claim(limit=args.limit), indent=2))
    return 0


def _cmd_complete(args) -> int:
    changed = queue.complete(
        args.token,
        args.verdict,
        gate=args.gate,
        proposal_doc_id=args.doc_id,
    )
    if not changed:
        # Not an error. Exactly-once means the second call is a no-op, and a
        # drain that retries a step should not be told it failed.
        print(json.dumps({"token": args.token, "changed": False, "reason": "already processed"}))
        return 0
    print(json.dumps({"token": args.token, "changed": True, "verdict": args.verdict}))
    return 0


def _cmd_release(args) -> int:
    print(json.dumps({"token": args.token, "released": queue.release(args.token)}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pacman queue operations.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Enqueue a URL, print its approval token.")
    p_add.add_argument("url")
    p_add.add_argument("--source", default="manual")
    p_add.add_argument("--source-ref", default=None)
    p_add.set_defaults(func=_cmd_add)

    p_count = sub.add_parser("count", help='Print {"count": N} of claimable rows.')
    p_count.set_defaults(func=_cmd_count)

    p_pending = sub.add_parser("pending", help="List claimable rows without claiming.")
    p_pending.add_argument("--limit", type=int, default=25)
    p_pending.set_defaults(func=_cmd_pending)

    p_claim = sub.add_parser("claim", help="Claim up to N rows, oldest first.")
    p_claim.add_argument("--limit", type=int, default=10)
    p_claim.set_defaults(func=_cmd_claim)

    p_complete = sub.add_parser("complete", help="Mark a claimed row processed.")
    p_complete.add_argument("token")
    p_complete.add_argument("--verdict", required=True, choices=list(queue.VALID_VERDICTS))
    p_complete.add_argument("--gate", type=int, default=None)
    p_complete.add_argument("--doc-id", default=None, help="Opaque proposal doc id from the second-brain adapter.")
    p_complete.set_defaults(func=_cmd_complete)

    p_release = sub.add_parser("release", help="Un-claim a row so the next drain retries it.")
    p_release.add_argument("token")
    p_release.set_defaults(func=_cmd_release)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ChassisDBUnavailable as exc:
        print(f"ERROR: pacman queue unavailable: {exc}", file=sys.stderr)
        return 2
    except queue.QueueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
