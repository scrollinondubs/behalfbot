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
  tripwire.py add-flight --from JFK --to LAX --date 2026-12-15 --max-stops one \
      --currency USD --drop-pct 15 [--max-price 300] [--fare-class main] --every 6h
  tripwire.py probe --check flight_price --from JFK --to LAX --date 2026-12-15
  tripwire.py add-stay --where "Downtown Austin" --country us --checkin 2027-03-10 \
      --checkout 2027-03-12 --adults 1 --min-rating 8 --currency USD --drop-pct 15 --every 6h
  tripwire.py probe --check stay_price --where "Downtown Austin" --country us \
      --checkin 2027-03-10 --checkout 2027-03-12
  tripwire.py add-item --query "ryzen ai max+ 395 128gb mini pc" --must 395 --must 128gb \
      --min-price 1000 --target-price 3299 [--sources amazon,walmart,bestbuy,newegg]
  tripwire.py add-item --product https://www.amazon.com/dp/B0DHJCYNS7 --drop-pct 10
  tripwire.py probe --check item_price --query "ryzen ai max+ 395 128gb mini pc" --must 395

`add-flight` watches the cheapest economy fare for one one-way leg (over
--flex-days either side of the date) and alerts when it is --drop-pct below the
average of the last 14 days of checks, or below the first fare until there are
3 days of them. It expires at the end of the last date searched unless
--expires says otherwise, and it checks once straight away. --group labels
which trip option a leg belongs to. Needs FIRECRAWL_API_KEY.

`add-stay` watches the cheapest qualifying stay for fixed dates in one area on
Booking.com and Vrbo, each source against its own 14-day history (Vrbo prices
are USD only), with the same good-buy rule. It expires at the end of the
checkin date unless --expires says otherwise, and checks once straight away.

`add-item` watches the cheapest qualifying listing for a search on major US
retailers (each source its own price series), or one product on one source.
It alerts when a listing is at or below --target-price (each listing id once),
or --drop-pct below that source's 14-day average (15 when there is no
--target-price), and again only below the last price alerted. It expires in
90 days unless --expires says otherwise, and checks once straight away.

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


def check_args(parser: argparse.ArgumentParser, probing: bool = False) -> None:
    checks = sorted(tw.URL_CHECKS) + (["flight_price", "stay_price", "item_price"] if probing else [])
    parser.add_argument("--check", required=True, choices=checks)
    parser.add_argument("--url", required=not probing)
    parser.add_argument("--text", help="for text_appears and text_disappears; case-insensitive")
    parser.add_argument("--status", help="for http_status")
    parser.add_argument("--allow-private", action="store_true",
                        help="permit loopback and private addresses, which are refused by default")


def price_args(parser: argparse.ArgumentParser) -> None:
    """Shared by flights, stays and items, so probe can take all three. The
    defaults are each check's own (EUR for flights and stays, USD for items)."""
    parser.add_argument("--currency", help="EUR by default; items are USD only")
    parser.add_argument("--drop-pct", type=float,
                        help="alert at this percent below the baseline (default 15; for items only "
                             "when there is no --target-price)")
    parser.add_argument("--max-price", type=float,
                        help="never alert above this: the fare, or the total stay; for items, "
                             "listings above it do not count")
    parser.add_argument("--group", help="which trip option this belongs to, e.g. jan-4-option-a")


def flight_args(parser: argparse.ArgumentParser, required: bool) -> None:
    parser.add_argument("--from", dest="origin", required=required, help="airport code, e.g. JFK")
    parser.add_argument("--to", dest="destination", required=required, help="airport code, e.g. LAX")
    parser.add_argument("--date", required=required, help="departure date, YYYY-MM-DD")
    parser.add_argument("--max-stops", choices=sorted(tw.flights.MAX_STOPS), default="one")
    parser.add_argument("--fare-class", choices=tw.flights.FARE_CLASSES, default="any",
                        help="main: wants no Basic Economy. Recorded and stated in alerts, NOT enforced: "
                             "the fare source shows no fare family")
    parser.add_argument("--market", help="2-letter Google sales market, e.g. US; Google's default if unset")
    parser.add_argument("--flex-days", type=int, default=0,
                        help="also search this many days either side of --date (0-3); one paid call per day")
    parser.add_argument("--max-duration-hours", type=float,
                        help="ignore itineraries longer than this, door to door (e.g. 20)")


def flight_params(args: argparse.Namespace) -> dict:
    try:
        return tw.flights.validate_params(
            origin=args.origin, destination=args.destination, date_str=args.date, max_stops=args.max_stops,
            currency=args.currency, drop_pct=args.drop_pct, max_price=args.max_price,
            fare_class=args.fare_class, market=args.market, flex_days=args.flex_days, group=args.group,
            max_duration_hours=args.max_duration_hours)
    except tw.flights.FlightError as e:
        raise tw.TripwireError(str(e)) from None


def stay_args(parser: argparse.ArgumentParser, required: bool) -> None:
    parser.add_argument("--where", required=required, help='the area, e.g. "Downtown Austin"')
    parser.add_argument("--country", help="2-letter country the area is in, e.g. us; strongly advised")
    parser.add_argument("--checkin", required=required, help="YYYY-MM-DD")
    parser.add_argument("--checkout", required=required, help="YYYY-MM-DD")
    parser.add_argument("--adults", type=int, default=1)
    parser.add_argument("--sources", help="stays: booking, vrbo, or booking,vrbo (default); items: any of "
                                          "amazon,walmart,bestbuy,newegg,microcenter "
                                          "(default amazon,walmart,bestbuy,newegg)")
    parser.add_argument("--min-rating", type=float,
                        help="out of 10 on both sources; unrated and Vrbo 'New' are left out unless --allow-unrated")
    parser.add_argument("--allow-unrated", action="store_true", help="keep unrated stays with --min-rating")


def stay_params(args: argparse.Namespace) -> dict:
    try:
        return tw.stays.validate_params(
            where=args.where, country=args.country, checkin=args.checkin, checkout=args.checkout,
            adults=args.adults, sources=args.sources, min_rating=args.min_rating,
            allow_unrated=args.allow_unrated, currency=args.currency, drop_pct=args.drop_pct,
            max_price=args.max_price, group=args.group)
    except tw.stays.StayError as e:
        raise tw.TripwireError(str(e)) from None


def item_args(parser: argparse.ArgumentParser, with_sources: bool) -> None:
    parser.add_argument("--query", help='search mode: what to search, e.g. "ryzen ai max+ 395 128gb mini pc"')
    parser.add_argument("--product", help="product mode: a product URL, or an id with --source "
                                          "(ASIN, Walmart item id, Best Buy SKU, Newegg item number)")
    parser.add_argument("--source", help="the retailer for a bare --product id")
    parser.add_argument("--must", action="append", default=[],
                        help="text every title must contain, case and spaces ignored; repeatable")
    parser.add_argument("--exclude", action="append", default=[],
                        help="drop titles containing this; repeatable")
    parser.add_argument("--min-price", type=float, help="drop listings below this (accessories, cases)")
    parser.add_argument("--target-price", type=float,
                        help="alert on any qualifying listing at or below this")
    parser.add_argument("--condition", choices=("new", "any"), default="new",
                        help="new (default) drops open-box, refurbished and used where the source says so")
    parser.add_argument("--include-sponsored", action="store_true", help="count sponsored listings too")
    parser.add_argument("--store", help="Micro Center store number; 029 (shippable, web prices) by default")
    if with_sources:
        parser.add_argument("--sources", help="any of amazon,walmart,bestbuy,newegg,microcenter "
                                              "(default amazon,walmart,bestbuy,newegg)")


def item_params(args: argparse.Namespace) -> dict:
    try:
        return tw.items.validate_params(
            query=args.query, product=args.product, source=args.source, must=args.must, exclude=args.exclude,
            min_price=args.min_price, max_price=args.max_price, condition=args.condition, sources=args.sources,
            include_sponsored=args.include_sponsored, store=args.store, target_price=args.target_price,
            drop_pct=args.drop_pct, currency=args.currency, group=args.group)
    except tw.items.ItemError as e:
        raise tw.TripwireError(str(e)) from None


def probe(check: dict) -> tw.Observation:
    return tw.evaluate({**check, "seen": False, "baseline_hash": None, "baseline_price": None})


def probe_line(obs: tw.Observation) -> str:
    if obs.outcome == "could_not_check":
        return f"first look: {obs.result}. This check may never work on this page."
    line = f"first look: {obs.result}"
    if obs.stay is not None:
        return "\n".join([line] + stay_lines(obs.stay))
    if obs.item is not None:
        return "\n".join([line] + item_lines(obs.item))
    return f"{line}\n  {obs.detail}" if obs.price is not None and obs.detail else line


def stay_lines(got) -> list[str]:
    """The cheapest qualifying options each source returned, so a first look
    shows what the price is made of."""
    out = []
    for r in got.sources:
        if r.place:
            out.append(f"  {tw.stays.SOURCES[r.source]} searched: {r.place}")
        for o in r.top:
            taxes = " before taxes" if o["source"] == "booking" else ", all fees included"
            out.append(f"  {o['source_label']}: {o['name']}, {o['price_label']} total{taxes}, {o['rating_label']}\n"
                       f"    {o['link']}")
    return out


def item_lines(got) -> list[str]:
    """The cheapest qualifying listings each source returned."""
    out = []
    for r in got.sources:
        for o in r.top:
            out.append(f"  {o['source_label']}: {o['name']}, {o['price_label']}, {o['rating_label']}\n"
                       f"    {o['link']}")
    return out


def cmd_add_flight(args: argparse.Namespace, store: tw.Store) -> int:
    rec = tw.add_flight(store, origin=args.origin, destination=args.destination, date_str=args.date,
                        max_stops=args.max_stops, currency=args.currency, drop_pct=args.drop_pct,
                        max_price=args.max_price, fare_class=args.fare_class, market=args.market,
                        flex_days=args.flex_days, group=args.group,
                        max_duration_hours=args.max_duration_hours, name=args.name, why=args.why, every=args.every, expires=args.expires,
                        message=args.message, expiry_message=args.expiry_message)
    print(f"added {rec['id']}: {rec['name']} (every {args.every.strip()}, expires {rec['expires_at']})")
    print(f"  {tw.flights.summary(rec['params'])}\n  {rec['params']['url']}")
    if args.fare_class == "main":
        print("  note: fare family cannot be checked, so a Basic Economy fare can still trigger it")
    if not args.no_probe:
        obs = tw.check_now(store, rec["id"])
        if obs.outcome == "could_not_check":
            print(f"first look: {obs.result}. No baseline yet; the heartbeat retries every interval.")
        else:
            print(probe_line(obs))
    return 0


def cmd_add_stay(args: argparse.Namespace, store: tw.Store) -> int:
    rec = tw.add_stay(store, where=args.where, country=args.country, checkin=args.checkin, checkout=args.checkout,
                      adults=args.adults, sources=args.sources, min_rating=args.min_rating,
                      allow_unrated=args.allow_unrated, currency=args.currency, drop_pct=args.drop_pct,
                      max_price=args.max_price, group=args.group, name=args.name, why=args.why, every=args.every,
                      expires=args.expires, message=args.message, expiry_message=args.expiry_message)
    print(f"added {rec['id']}: {rec['name']} (every {args.every.strip()}, expires {rec['expires_at']})")
    print(f"  {tw.stays.summary(rec['params'])}")
    if "vrbo" in rec["params"]["sources"] and rec["params"]["currency"] != tw.stays.VRBO_CURRENCY:
        print(f"  note: Vrbo prices are USD, so Vrbo is tracked in USD and Booking.com in {rec['params']['currency']}")
    if not args.no_probe:
        obs = tw.check_now(store, rec["id"])
        if obs.outcome == "could_not_check":
            print(f"first look: {obs.result}. No baseline yet; the heartbeat retries every interval.")
        else:
            print(probe_line(obs))
    return 0


def cmd_add_item(args: argparse.Namespace, store: tw.Store) -> int:
    rec = tw.add_item(store, query=args.query, product=args.product, source=args.source, must=args.must,
                      exclude=args.exclude, min_price=args.min_price, max_price=args.max_price,
                      condition=args.condition, sources=args.sources, include_sponsored=args.include_sponsored,
                      store_id=args.store, target_price=args.target_price, drop_pct=args.drop_pct,
                      currency=args.currency, group=args.group, name=args.name, why=args.why, every=args.every,
                      expires=args.expires, message=args.message, expiry_message=args.expiry_message)
    print(f"added {rec['id']}: {rec['name']} (every {args.every.strip()}, expires {rec['expires_at']})")
    print(f"  {tw.items.summary(rec['params'])}\n  {rec['params']['url']}")
    if not args.no_probe:
        obs = tw.check_now(store, rec["id"])
        if obs.outcome == "could_not_check":
            print(f"first look: {obs.result}. No baseline yet; the heartbeat retries every interval.")
        else:
            print(probe_line(obs))
    return 0


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
        if r["is_flight"]:
            print(f"  {r['target']}" + (f" [{r['group']}]" if r["group"] else "") + f"\n  {r['fares_label']}")
        if r["is_stay"]:
            print(f"  {r['target']}" + (f" [{r['group']}]" if r["group"] else ""))
            for line in r["stay_prices"]:
                print(f"  {line}")
        if r["is_item"]:
            print(f"  {r['target']}" + (f" [{r['group']}]" if r["group"] else ""))
            for line in r["item_prices"]:
                print(f"  {line}")
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

    p = sub.add_parser("add-flight", help="watch the cheapest economy fare on one leg")
    flight_args(p, required=True)
    price_args(p)
    p.add_argument("--every", default="6h", help="at least 15m; 6h by default")
    p.add_argument("--expires", help="default: the end of the last date searched")
    p.add_argument("--name", help="default: Fare <FROM>-<TO> <date>")
    p.add_argument("--why", help="default: Fare watch for the leg")
    p.add_argument("--message", help="first line of each alert; default: Fare drop on the leg")
    p.add_argument("--expiry-message", help="extra line for the expiry message")
    p.add_argument("--no-probe", action="store_true", help="skip the first check; the heartbeat makes it")

    p = sub.add_parser("add-stay", help="watch the cheapest qualifying stay for fixed dates in one area")
    stay_args(p, required=True)
    price_args(p)
    p.add_argument("--every", default="6h", help="at least 15m; 6h by default")
    p.add_argument("--expires", help="default: the end of the checkin date")
    p.add_argument("--name", help="default: Stay <where> <checkin>")
    p.add_argument("--why", help="default: Stay price watch for the dates")
    p.add_argument("--message", help="first line of each alert; default: Stay price drop in <where>")
    p.add_argument("--expiry-message", help="extra line for the expiry message")
    p.add_argument("--no-probe", action="store_true", help="skip the first check; the heartbeat makes it")

    p = sub.add_parser("add-item", help="watch a product, or the cheapest listing for a search, on US retailers")
    item_args(p, with_sources=True)
    price_args(p)
    p.add_argument("--every", default="6h", help="at least 15m; 6h by default")
    p.add_argument("--expires", default="90d", help="90d by default")
    p.add_argument("--name", help="default: Item <query or product>")
    p.add_argument("--why", help="default: Price watch for <query or product>")
    p.add_argument("--message", help="first line of each alert; default: Deal on <query or product>")
    p.add_argument("--expiry-message", help="extra line for the expiry message")
    p.add_argument("--no-probe", action="store_true", help="skip the first check; the heartbeat makes it")

    sub.add_parser("cancel").add_argument("id")
    sub.add_parser("archive").add_argument("id")
    p = sub.add_parser("probe")
    check_args(p, probing=True)
    flight_args(p, required=False)
    stay_args(p, required=False)
    item_args(p, with_sources=False)
    price_args(p)

    args = parser.parse_args()
    store = tw.Store()
    try:
        if args.cmd == "add":
            return cmd_add(args, store)
        if args.cmd == "add-realestate":
            return cmd_add_realestate(args, store)
        if args.cmd == "add-flight":
            return cmd_add_flight(args, store)
        if args.cmd == "add-stay":
            return cmd_add_stay(args, store)
        if args.cmd == "add-item":
            return cmd_add_item(args, store)
        if args.cmd == "list":
            return cmd_list(args, store)
        if args.cmd == "cancel":
            print(f"cancelled {tw.cancel(store, args.id)['id']}")
        elif args.cmd == "archive":
            print(f"archived {tw.archive(store, args.id)['id']}")
        elif args.cmd == "probe" and args.check == "item_price":
            print(probe_line(probe({"check": "item_price", "params": item_params(args), "item_state": {}})))
        elif args.cmd == "probe" and args.check == "stay_price":
            print(probe_line(probe({"check": "stay_price", "params": stay_params(args), "stay_state": {}})))
        elif args.cmd == "probe" and args.check == "flight_price":
            print(probe_line(probe({"check": "flight_price", "params": flight_params(args)})))
        elif args.cmd == "probe":
            print(probe_line(probe(tw.build_check(args.check, args.url, args.text, args.status,
                                                  args.allow_private))))
    except (tw.TripwireError, tw.StoreBusy, tw.StoreUnavailable) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
