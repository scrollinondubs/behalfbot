#!/usr/bin/env python3
"""Add, list, cancel and archive tripwires. See chassis/docs/tripwires.md.

  tripwire.py add --name "Tickets" --why "Two seats wanted" \
      --check text_disappears --url https://example.com/t --text "sold out" \
      --every 30m --expires 2026-12-01 --message "Tickets may be on sale"
  tripwire.py list [--all] [--json]
  tripwire.py cancel <id>
  tripwire.py archive <id>
  tripwire.py probe --check text_appears --url https://example.com --text "example"

`add` looks at the page once and prints what it saw, so a bot wall shows up now
instead of three checks from now. `probe` does only that and stores nothing.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _tripwires as tw  # noqa: E402


def check_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--check", required=True, choices=sorted(tw.CHECK_TYPES))
    parser.add_argument("--url", required=True)
    parser.add_argument("--text", help="for text_appears and text_disappears; case-insensitive")
    parser.add_argument("--status", help="for http_status")
    parser.add_argument("--allow-private", action="store_true",
                        help="permit loopback and private addresses, which are refused by default")


def probe(check: dict) -> tw.Observation:
    return tw.evaluate({**check, "seen": False, "baseline_hash": None})


def probe_line(obs: tw.Observation) -> str:
    if obs.outcome == "could_not_check":
        return f"first look: {obs.result}. This check may never work on this page."
    return f"first look: {obs.result}"


def cmd_add(args: argparse.Namespace, store: tw.Store) -> int:
    rec = tw.add(store, name=args.name, why=args.why, check=args.check, url=args.url, text=args.text,
                 status=args.status, every=args.every, expires=args.expires, message=args.message,
                 expiry_message=args.expiry_message, keep_watching=args.keep_watching,
                 allow_private=args.allow_private)
    print(f"added {rec['id']}: {rec['name']} (expires {rec['expires_at']})")
    if not args.no_probe:
        print(probe_line(probe(rec)))
    return 0


def cmd_list(args: argparse.Namespace, store: tw.Store) -> int:
    rows = tw.view(store, show_archived=args.all)["rows"]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for r in rows:
        print(f"{r['id']}  {r['status']:<9}  {r['name']}\n"
              f"  {r['check_label']}: {r['url']}\n"
              f"  last checked {r['last_checked_label']}: {r['last_result'] or 'not yet'}"
              f"; next {r['next_check_label']}; expires {r['expires_label']}"
              f"; failures in a row {r['consecutive_failures']}")
    if not rows:
        print("no tripwires")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Short-term monitors held as data.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add")
    check_args(p)
    p.add_argument("--name", required=True)
    p.add_argument("--why", required=True)
    p.add_argument("--every", default="1h", help="15m, 1h, 2d; at least 15m")
    p.add_argument("--expires", required=True, help="36h, 2d, a date, or an ISO timestamp")
    p.add_argument("--message", required=True, help="what to send when it fires")
    p.add_argument("--expiry-message", help="extra line for the expiry message")
    p.add_argument("--keep-watching", action="store_true", help="stay active after firing")
    p.add_argument("--no-probe", action="store_true", help="skip the first look at the page")

    p = sub.add_parser("list")
    p.add_argument("--all", action="store_true", help="include archived")
    p.add_argument("--json", action="store_true")

    sub.add_parser("cancel").add_argument("id")
    sub.add_parser("archive").add_argument("id")
    check_args(sub.add_parser("probe"))

    args = parser.parse_args()
    store = tw.Store()
    try:
        if args.cmd == "add":
            return cmd_add(args, store)
        if args.cmd == "list":
            return cmd_list(args, store)
        if args.cmd == "cancel":
            print(f"cancelled {tw.cancel(store, args.id)['id']}")
        elif args.cmd == "archive":
            print(f"archived {tw.archive(store, args.id)['id']}")
        elif args.cmd == "probe":
            print(probe_line(probe(tw.build_check(args.check, args.url, args.text, args.status,
                                                  args.allow_private))))
    except (tw.TripwireError, tw.StoreBusy, tw.StoreUnavailable) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
