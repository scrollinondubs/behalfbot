# TripWires

Short-term monitors held as data. A package arriving, tickets going on sale, a
listing going live: add a record, and the `tripwires` heartbeat watches it until
it fires or expires. No script, no `HEARTBEATS.md` change, no PR.

## Setup

New installs get the heartbeat from `HEARTBEATS.md.template`. On an existing
install, copy the `tripwires` block from the template into `HEARTBEATS.md`. That
one entry serves every tripwire. The dispatcher ticks every 15 minutes, so that
is the shortest interval.

Messages go through `chassis_alert` in `chassis/scripts/_alert.sh`, the install's
existing alert route. To send to one Discord channel instead, set
`TRIPWIRE_CHANNEL_ID` in front of the gather:

```yaml
gather: TRIPWIRE_CHANNEL_ID=<channel id> state/chassis-root/scripts/gather-tripwires.py
```

## Checks

| Check | Fires when | Notes |
|---|---|---|
| `text_appears` | the text is on the page | Case and whitespace are ignored. Matches visible text and raw HTML, so embedded JSON counts |
| `text_disappears` | the text was there and is gone | Must see the text at least once first; until then it reports "could not check" |
| `page_changed` | the visible text's hash differs from the baseline | The first good fetch sets the baseline. Script and style are ignored, but a page showing a clock or a view count will fire at once |
| `http_status` | the status equals the one you gave | A different status is a normal result, except 401, 403, 407, 429, 451 and 5xx, which read as "could not check" unless they are the status you asked for |
| `flight_price` | a leg's cheapest economy fare drops below its baseline | CLI only (`add-flight`). See "Flight fares" |

Checks are one plain GET with no JavaScript and no login. Loopback and private
addresses are refused, including through a redirect, unless the CLI is given
`--allow-private`. The dashboard form cannot set that.

## Real-estate listings (`realestate`)

A tripwire that watches files instead of a page: a GeoJSON file of zones and a
JSON file of listings, both written by the install. On each check it screens the
zones with the tripwire's criteria and alerts on listings that newly fall inside
a passing zone. The chassis knows no zone property by name; the criteria name
them.

```bash
chassis/scripts/tripwire.py add-realestate --seed config/tripwires/<name>.json
```

The seed holds `name`, `why`, `zones_path`, `listings_path` (both relative to the
customer home, never outside it), `criteria`, `every`, `expires`, `message`, an
optional `expiry_message` and a `seed_key`. Re-running the seed while that
tripwire is active changes nothing, so criteria edited on the dashboard survive
a redeploy.

**Criteria.**

```json
{"id_property": "zone_id",
 "rules": [{"key": "max_drive", "label": "Max drive (min)", "property": "drive_min",
            "op": "<=", "value": 75, "enabled": true, "missing": "fail", "group": "limit"}],
 "weights": [{"key": "view", "label": "View", "property": "view_score", "weight": 3}],
 "regions": {"property": "region", "label_property": "region_name", "include": []}}
```

- A zone passes when every enabled rule holds. `op` is one of `<= >= < > == !=`;
  text and true/false values only take `==` and `!=`. `missing` decides a zone
  without the property (`fail` by default). `group` is `exclusion` or `limit`,
  which only sorts the form.
- The score is the weighted mean of the weight properties, each clamped to 0..1.
  A missing one counts 0.
- An empty `include` means every region.

**Listings file.** `{"listings": [...]}`, each with `id`, `source`, `url`,
`title`, `price_eur`, `area_m2`, `lat`, `lon`, `first_seen` and `raw`. Optional:
`approximate: true` with `zone_hint` for a pin the install could only place at a
zone's centre. A record without an id, an http(s) URL or coordinates is skipped.

A listing matches when its point is inside a passing zone (`inside`), or when it
is approximate and its hinted zone passes (`approximate`; it is never reported as
inside, because a search area is usually wider than the zone). Each matching id
alerts once, batched into one message per check with at most 8 lines. Widening
the criteria alerts on listings that newly match.

**Dashboard.** Each realestate row links to `/tripwires/<id>/map`: a Leaflet map
on OpenStreetMap tiles with every zone (passing ones coloured by score, failing
ones grey with the reasons in the popup), listing pins with price, size and a
link to the ad, and a form for the enabled rules, their values, the regions and
the weights. Properties and operators are fixed by the seed. Leaflet loads from
cdnjs, like the d3 on the disk map.

**Untrusted listings.** Titles and `raw` never reach an alert or the store; an
alert line is our wording, the numbers, the zone id, the source and the URL.
The map builds popups with `textContent` and links only http(s) URLs. A missing
or unreadable file is "could not check", with the usual alert after 3 in a row.

## Flight fares (`flight_price`)

Watches the cheapest economy fare for one one-way leg and alerts when it is
significantly lower than it has been.

```bash
chassis/scripts/tripwire.py probe --check flight_price --from JFK --to LAX --date 2026-12-15
chassis/scripts/tripwire.py add-flight --from JFK --to LAX --date 2026-12-15 \
  [--max-stops nonstop|one] [--currency USD] [--drop-pct 15] [--max-price 300] \
  [--fare-class any|main] [--market US] [--every 6h] [--expires 2026-12-15] \
  [--name ...] [--why ...] [--message ...]
```

Defaults: at most one stop, EUR, 15%, every 6h, and expiry at the end of the
flight date. Name, why and message are filled in from the leg when not given.

- **Baseline.** The first successful check records the cheapest eligible fare
  as the baseline. `add-flight` makes that check at once (skip it with
  `--no-probe`), so the fare it prints is the one it keeps. A failed check never
  sets a baseline.
- **Alert.** When the cheapest fare is at least `--drop-pct` below the
  baseline, and not above `--max-price` when one is set. `--max-price` is a gate,
  not a trigger: a fare under it that is not also the drop below the baseline
  stays quiet. After an alert it keeps watching and alerts again only on a fare
  below the last one alerted, until it expires. The baseline never moves.
- **Eligible fares.** Economy on every segment (the source's
  `cabin_matches_request` must be true, since Google substitutes another cabin
  when a flight has no economy fare), at most the stops asked for, and the
  currency asked for. The source is asked for these and they are checked again
  here. Up to 50 rows of Google's first results page are read: the cheapest
  fare is often listed well below the "top" flights.
- **The alert** carries the price and currency, how far below the baseline and
  when the baseline was seen, the last price alerted, the airline(s), stops and
  connection airports, total duration, departure time (local to the origin),
  and a Google Flights search link for the leg.
- **Fare family (`--fare-class main`).** The cheapest economy fare is often
  Basic Economy, which can block seat selection. The fare source shows no fare
  family, so `main` cannot filter. It is recorded, shown on the tab and stated
  in every alert as unverified. Check the fare before booking.
- **No duration filter.** The cheapest one-stop fare can be an overnight
  connection; the alert shows the duration so a person can judge.

**Honesty.** A fare source error, a missing `FIRECRAWL_API_KEY`, an empty result
and a result where no fare passes the filters are all "could not check", never
"no flights" or "price unchanged". They follow the usual failure path: `blind`
on the tab and one alert after 3 in a row. At a 6h interval that alert comes
after about 18 hours.

**Fare source.** A function in `chassis/scripts/_flights.py` that takes the
tripwire's params and returns fares, so it can be swapped. The default calls
Firecrawl's Alexandria provider `flights-google-com` (capability
`flights/search_flights`, a Google Flights scraper) through
`POST https://api.firecrawl.dev/v2/scrape` with an `alexandria` body, using
`FIRECRAWL_API_KEY` from the environment. In the container that comes from
`.env.baked`. The key is never printed, stored or put in a message.

**Untrusted fares.** Fares come from a third-party scrape. Only validated fields
are kept or sent: airport and airline codes, times and numbers by pattern, and
an airline name only when it is plain letters of at most 40 characters (the
code otherwise). The link in the alert is built from the tripwire's own params,
never taken from the response.

## What a tripwire does

- **Could not check.** A network failure, a non-2xx on the text and hash checks,
  a recognised bot wall, or a JavaScript-only shell is "could not check", never
  a negative result. The tab shows the tripwire as `blind`. After 3 in a row it
  sends one alert; the count and the alert reset on the next good check. The bot
  wall and shell detection are heuristics, so a wall they miss on a
  `text_disappears` check can fire falsely. Use `tripwire.py probe` first.
- **Firing.** Once by default, then status `fired`. With keep-watching it stays
  active and fires each time the condition becomes true again, and on each
  change for `page_changed`.
- **Expiry.** Every tripwire has one. The last check runs, then one message says
  it expired unfired and what the last result was, then status `expired`. An
  optional expiry message adds a line, for "if this never happens, do X".
- **Delivery.** A message that cannot be sent is kept and retried each tick, and
  the status only changes once it has gone out. A crash between the send and the
  write can repeat a message; it cannot lose one.
- **Cancel** wins over anything in flight, including an unsent fire. Archive
  applies to anything no longer active and hides it from the default view.

## Fetched pages are untrusted

Nothing from a response body, header or reason phrase is stored, sent or shown.
`last_result` holds only our own wording and the status code, and the page hash
is a sha256. Messages contain the name, your authored text and the URL. The one
exception is `flight_price`, which keeps and sends fare fields after validating
them (see "Untrusted fares" above).

## Spend

None in normal operation: the gather posts for itself and prints `count: 0`. It
prints `count: 1` only when the heartbeat is broken (`store_unreadable`,
`send_failed`), which fires `tripwires-broken-prompt.md`; `alert_signature` and
`dedupe_key` keep that from repeating every tick. An unexpected crash exits
non-zero, which the dispatcher reports directly for a `critical` heartbeat.

A check that fails is not a broken heartbeat and never spends anything.

The exception is `flight_price`: each check is one Firecrawl call, which cost
5 credits when this was written. One leg every 6h is 20 credits a day, so five
legs are about 100 a day. A missing key costs nothing and reads as "could not
check".

## The store

`$CUSTOMER_HOME/state/tripwires/`, outside git:

| File | |
|---|---|
| `tripwires.json` | All records, replaced atomically |
| `.lock` | Serialises writers. Created with `O_EXCL`, because `flock` does not cross a virtiofs bind mount |
| `.created` | Says a store exists, so a missing file is an error and never reads as empty |
| `heartbeat.json` | Last run time, so the tab can tell a dead heartbeat from a quiet watch |

The container and a host dashboard both use it. Readers take no lock. The lock
is held for milliseconds, never across a fetch or a send, and one left by a
crash is broken after 30 seconds.

## Not in v1

Checks that need a logged-in browser or a model reading the page. Many carrier
tracking pages will need one or the other. Design the prompt injection handling
before building that.
