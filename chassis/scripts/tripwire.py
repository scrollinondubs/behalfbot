#!/usr/bin/env python3
"""Add, list, cancel and archive tripwires. See chassis/docs/tripwires.md.

  tripwire.py add --name "Tickets" --why "Two seats wanted" \
      --check text_disappears --url https://example.com/t --text "sold out" \
      --every 30m --expires 2026-12-01 --message "Tickets may be on sale"
  tripwire.py list [--all] [--json]
  tripwire.py cancel <id>
  tripwire.py archive <id>
  tripwire.py probe --check text_appears --url https://example.com --text "example"
  tripwire.py add-realestate --seed config/tripwires/land.json

`add-realestate` reads a seed file (name, why, zones_path, listings_path,
criteria, every, expires, message, seed_key). Re-running it while that seed's
tripwire is active changes nothing, so edits made on the dashboard survive.

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
    parser.add_argument("--check", required=True, choices=sorted(tw.URL_CHECKS))
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


def cmd_add_realestate(args: argparse.Namespace, store: tw.Store) -> int:
    try:
        seed = json.loads(Path(args.seed).read_text(encoding="utf8"))
    except (OSError, ValueError) as e:
        print(f"error: cannot read the seed file: {type(e).__name__}", file=sys.stderr)
        return 1
    if not isinstance(seed, dict):
        print("error: the seed file must hold a JSON object", file=sys.stderr)
        return 1
    rec, created = tw.add_realestate(
        store, name=seed.get("name"), why=seed.get("why"), zones_path=seed.get("zones_path"),
        listings_path=seed.get("listings_path"), criteria=seed.get("criteria") or {},
        every=seed.get("every"), expires=seed.get("expires"), message=seed.get("message"),
        expiry_message=seed.get("expiry_message"), seed_key=seed.get("seed_key"))
    verb = "added" if created else "already active, left as is:"
    print(f"{verb} {rec['id']}: {rec['name']} (expires {rec['expires_at']})")
    print(f"first look: {tw.evaluate(rec).result}")
    return 0


def cmd_list(args: argparse.Namespace, store: tw.Store) -> int:
    rows = tw.view(store, show_archived=args.all)["rows"]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for r in rows:
        print(f"{r['id']}  {r['status']:<9}  {r['name']}\n"
              f"  {r['check_label']}: {r['url'] or r['target']}\n"
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

    p = sub.add_parser("add-realestate", help="add a realestate tripwire from a seed file")
    p.add_argument("--seed", required=True)

    sub.add_parser("cancel").add_argument("id")
    sub.add_parser("archive").add_argument("id")
    check_args(sub.add_parser("probe"))

    args = parser.parse_args()
    store = tw.Store()
    try:
        if args.cmd == "add":
            return cmd_add(args, store)
        if args.cmd == "add-realestate":
            return cmd_add_realestate(args, store)
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
