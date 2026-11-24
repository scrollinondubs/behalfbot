"""The `stay_price` TripWire: the cheapest qualifying stay for fixed dates in
one area, on Booking.com and Vrbo, compared with what it has been.

Each source is its own price series. Booking.com answers in the currency asked
for; Vrbo answers in USD only. Converting would need a third source (a rate)
and would put exchange-rate noise into the average, and a merged "cheapest"
would read a source dropping out and coming back as a price move. So each
source keeps its own first price, history, lowest and last alerted price, and
each alerts against its own history. When both are in the same currency the
tab lists them together; otherwise side by side.

"What it has been" is the same as for flights (_flights.py): the mean of the
successful checks in the last 14 days, or the first price seen until that is
3 days old. After an alert a source alerts again only below the last price it
alerted at.

A source that fails is "could not check" for that source, and the result line
says so. The check as a whole is "could not check" only when every source
failed. A source failing 3 checks in a row while another works sends one
alert of its own, so a dead source is never silently dropped.

The stay source is a function, so it can be swapped: it takes a source name
and the tripwire's params and returns a Search, or raises StayError. The
default asks Firecrawl's Alexandria providers `booking-com` and `vrbo-com` with
FIRECRAWL_API_KEY from the environment. The key is never printed or stored.

Untrusted: these are third-party listings. Numbers, ids and ratings are
validated by type and pattern; links are rebuilt from validated ids and our own
params, never taken from a response. The one free-text field kept is the
property name (and Vrbo's neighbourhood): newlines, markup and URLs are
stripped, anything outside a plain character set is dropped, and it is cut to
60 characters. Treat it as a label written by a stranger, because it is one.

Standard library only, like _tripwires.py.
"""
from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable

import _flights as flights

SOURCES = {"booking": "Booking.com", "vrbo": "Vrbo"}
VRBO_CURRENCY = "USD"
MAX_NIGHTS = 28  # Vrbo's limit; Booking.com allows 90
MAX_ADULTS = 20
RESULT_LIMIT = 50
TOP_N = 3
BOOKING = ("booking-com", "hotels/search")
VRBO_LOCATIONS = ("vrbo-com", "vacation-rentals/locations")
VRBO_SEARCH = ("vrbo-com", "vacation-rentals/search")
BOOKING_TYPES = {201: "apartment", 204: "hotel", 203: "hostel", 206: "hotel", 208: "B&B", 220: "holiday home",
                 216: "guest house", 213: "villa", 222: "homestay", 228: "chalet", 219: "aparthotel"}

_WHERE = re.compile(r"[^\W_][\w .,'()&/-]{0,99}")
_COUNTRY = re.compile(r"[a-z]{2}")
_PAGE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,119}")
_VRBO_LISTING = re.compile(r"[0-9]{1,12}(ha)?")
_DIGITS = re.compile(r"[1-9][0-9]{0,19}")
_VRBO_TOTAL = re.compile(r"\$([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)(\.[0-9]{1,2})? for ([0-9]{1,3}) nights?")
_VRBO_NIGHTLY = re.compile(r"\$([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)(\.[0-9]{1,2})?")
_URLISH = re.compile(r"(https?://|www\.)\S*", re.I)
_TAG = re.compile(r"<[^>]*>")
_NOT_PLAIN = re.compile(r"[^\w .,'&()/+#-]")
_DISTANCE = re.compile(r"[0-9]{1,3}(\.[0-9]{1,2})? (miles|mile|km|m|ft) from [A-Za-z ]{1,30}")
MAX_LABEL = 60


class StayError(Exception):
    """Bad params, or a source that gave no usable answer. The message is our
    own wording, fit to show."""


@dataclass
class Option:
    source: str            # booking | vrbo
    name: str
    total: float
    currency: str
    nightly: float | None
    rating: float | None   # out of 10
    reviews: int | None
    link: str
    kind: str = ""         # hotel, apartment, or Vrbo's "Apartment · 1 bedroom" head word
    area: str = ""         # Vrbo neighbourhood or Booking.com distance line


@dataclass
class Search:
    options: list = field(default_factory=list)
    place: str = ""        # what the source resolved --where to, our wording plus a validated name
    region_id: str = ""    # Vrbo only: kept so later checks search the same region


StayFetch = Callable[[str, dict], Search]


# ---- params --------------------------------------------------------------------

def validate_params(*, where: str | None, country: str | None, checkin: str | None, checkout: str | None,
                    adults: int | str | None = 1, sources: str | list | None = "booking,vrbo",
                    min_rating: float | str | None = None, allow_unrated: bool = False,
                    currency: str | None = "EUR", drop_pct: float | str | None = flights.DEFAULT_DROP_PCT,
                    max_price: float | str | None = None, group: str | None = None,
                    today: date | None = None) -> dict:
    where_ = " ".join((where or "").split())
    if not _WHERE.fullmatch(where_):
        raise StayError("where must be a place name of up to 100 letters, digits, spaces and . , ' ( ) & / -")
    params: dict = {"where": where_}
    params["country"] = (country or "").strip().lower() or None
    if params["country"] and not _COUNTRY.fullmatch(params["country"]):
        raise StayError("country must be a 2-letter code such as us")
    try:
        cin = date.fromisoformat((checkin or "").strip())
        cout = date.fromisoformat((checkout or "").strip())
    except ValueError:
        raise StayError("checkin and checkout must look like 2027-01-04") from None
    if cin < (today or date.today()):
        raise StayError("the checkin date is already in the past")
    nights = (cout - cin).days
    if not 1 <= nights <= MAX_NIGHTS:
        raise StayError(f"checkout must be 1 to {MAX_NIGHTS} nights after checkin")
    params.update(checkin=cin.isoformat(), checkout=cout.isoformat(), nights=nights)
    try:
        params["adults"] = int(1 if adults in (None, "") else adults)
    except (TypeError, ValueError):
        raise StayError("adults must be a whole number") from None
    if not 1 <= params["adults"] <= MAX_ADULTS:
        raise StayError(f"adults must be between 1 and {MAX_ADULTS}")
    names = sources.split(",") if isinstance(sources, str) else list(sources or [])
    names = [s.strip().lower() for s in names if s and s.strip()] or list(SOURCES)
    if any(s not in SOURCES for s in names):
        raise StayError("sources must be booking, vrbo, or booking,vrbo")
    params["sources"] = [s for s in SOURCES if s in names]
    params["min_rating"] = None if min_rating in (None, "") else _number(min_rating, "min rating")
    if params["min_rating"] is not None and not 0 < params["min_rating"] <= 10:
        raise StayError("min rating is out of 10 and must be above 0")
    params["allow_unrated"] = bool(allow_unrated)
    params["currency"] = (currency or "EUR").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", params["currency"]):
        raise StayError("currency must be a 3-letter code such as USD")
    params["drop_pct"] = _number(flights.DEFAULT_DROP_PCT if drop_pct in (None, "") else drop_pct, "drop percent")
    if not 1 <= params["drop_pct"] <= 90:
        raise StayError("drop percent must be between 1 and 90")
    params["max_price"] = None if max_price in (None, "") else _number(max_price, "max price")
    if params["max_price"] is not None and params["max_price"] <= 0:
        raise StayError("max price must be above 0")
    if (params["max_price"] is not None and "vrbo" in params["sources"]
            and params["currency"] != VRBO_CURRENCY):
        raise StayError(f"Vrbo prices are in USD only, so a {params['currency']} max price cannot gate them: "
                        "use --currency USD, or --sources booking")
    params["group"] = (group or "").strip() or None
    if params["group"] and not flights._GROUP.fullmatch(params["group"]):
        raise StayError("group must be up to 40 letters, digits, spaces, dots, dashes or underscores")
    params["booking_url"] = booking_search_url(params)
    params["vrbo_url"] = vrbo_search_url(params)
    params["url"] = params["booking_url"] if "booking" in params["sources"] else params["vrbo_url"]
    return params


def _number(value, label: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise StayError(f"{label} must be a number") from None
    if not math.isfinite(out):
        raise StayError(f"{label} must be a number")
    return out


def source_currency(params: dict, source: str) -> str:
    return VRBO_CURRENCY if source == "vrbo" else params["currency"]


def booking_search_url(params: dict) -> str:
    q = {"ss": params["where"], "checkin": params["checkin"], "checkout": params["checkout"],
         "group_adults": params["adults"], "no_rooms": 1, "group_children": 0,
         "selected_currency": params["currency"], "order": "price"}
    if params.get("min_rating") and not params.get("allow_unrated"):
        q["nflt"] = f"review_score={int(params['min_rating'] * 10)}"
    return "https://www.booking.com/searchresults.html?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)


def vrbo_search_url(params: dict, region_id: str | None = None) -> str:
    q = {"destination": params["where"], "startDate": params["checkin"], "endDate": params["checkout"],
         "adults": params["adults"], "sort": "PRICE_LOW_TO_HIGH"}
    if region_id:
        q["regionId"] = region_id
    return "https://www.vrbo.com/search?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)


def stay(params: dict) -> str:
    n = params["nights"]
    who = f"{params['adults']} adult" + ("s" if params["adults"] != 1 else "")
    return f"{params['where']} {params['checkin']} to {params['checkout']} ({n} night{'s' if n != 1 else ''}, {who})"


def summary(params: dict) -> str:
    """One line for the tab and `list`."""
    out = f"{stay(params)}, {' and '.join(SOURCES[s] for s in params['sources'])}"
    if params.get("min_rating"):
        out += f", rated {params['min_rating']:g}+" + (" or unrated" if params.get("allow_unrated") else "")
    out += f", alert at {params['drop_pct']:g}% below the 14-day average"
    if params.get("max_price") is not None:
        out += f" and at most {flights.money(params['max_price'], params['currency'])} total"
    return out


# ---- untrusted text ------------------------------------------------------------

def label(value, limit: int = MAX_LABEL) -> str:
    """A listing's free text made safe to show: no newlines, markup or URLs,
    a plain character set, and short."""
    if not isinstance(value, str):
        return ""
    text = _URLISH.sub(" ", _TAG.sub(" ", value))
    text = " ".join(_NOT_PLAIN.sub(" ", text).split())
    if len(text) > limit:
        text = text[:limit - 3].rstrip() + "..."
    return text


# ---- history -------------------------------------------------------------------

def new_state(currency: str) -> dict:
    return {"currency": currency, "baseline_price": None, "baseline_at": None, "lowest_price": None,
            "last_alerted_price": None, "history": [], "consecutive_failures": 0, "failure_alerted": False,
            "top": [], "last_error": None}


def reference(state: dict, now: datetime) -> tuple[float | None, str]:
    """What a source's price is compared with, and how to say so."""
    cur = state["currency"]
    first, first_at = state.get("baseline_price"), state.get("baseline_at")
    if first is None:
        return None, ""
    window = [h["price"] for h in state.get("history") or [] if now - flights._at(h["at"]) <= flights.AVG_WINDOW]
    if now - flights._at(first_at) < flights.MIN_HISTORY or not window:
        return first, (f"the first price seen, {flights.money(round(first), cur)} on {first_at[:10]} "
                       f"(under 3 days of history, so no average yet)")
    avg = sum(window) / len(window)
    checks = f"{len(window)} check" + ("s" if len(window) != 1 else "")
    return avg, f"the 14-day average {flights.money(round(avg), cur)} ({checks})"


def prices_label(rec: dict, now: datetime) -> list[str]:
    """The tab's lines of numbers, one per source."""
    lines = []
    for source in rec["params"]["sources"]:
        state = (rec.get("stay_state") or {}).get(source) or new_state(source_currency(rec["params"], source))
        cur = state["currency"]
        ref, how = reference(state, now)
        if ref is None:
            compared = "no price yet"
        elif how.startswith("the 14-day"):
            compared = f"14-day average {flights.money(round(ref), cur)}"
        else:
            compared = "average after 3 days of checks"
        money = lambda v: flights.money(None if v is None else round(v), cur)  # noqa: E731
        lines.append(f"{SOURCES[source]}: {compared}, first {money(state.get('baseline_price'))}, "
                     f"lowest {money(state.get('lowest_price'))}, last alert {money(state.get('last_alerted_price'))}")
    return lines


def top_rows(rec: dict) -> list[dict]:
    """The cheapest qualifying options from the last good check of each source,
    merged when every source is in the same currency."""
    params = rec["params"]
    per = [((rec.get("stay_state") or {}).get(s) or {}).get("top") or [] for s in params["sources"]]
    if len({source_currency(params, s) for s in params["sources"]}) == 1:
        return sorted((o for group in per for o in group), key=lambda o: o["total"])[:TOP_N]
    return [o for group in per for o in group]


# ---- the default stay source ---------------------------------------------------

def booking_options(params: dict) -> dict:
    options = {"query": params["where"], "checkin": params["checkin"], "checkout": params["checkout"],
               "adults": params["adults"], "currency": params["currency"], "sort": "price", "rows": RESULT_LIMIT}
    if params.get("country"):
        options["country_code"] = params["country"]
    if params.get("min_rating") and not params.get("allow_unrated"):
        # Booking.com drops unrated properties with this filter, so it is only
        # sent when unrated ones are not wanted. Checked again locally.
        options["filters"] = f"review_score={int(params['min_rating'] * 10)}"
    return options


def vrbo_options(params: dict, region_id: str) -> dict:
    return {"region_id": region_id, "checkin": params["checkin"], "checkout": params["checkout"],
            "adults": params["adults"], "sort": "PRICE_LOW_TO_HIGH", "limit": RESULT_LIMIT}


def _call(provider: str, capability: str, options: dict):
    """One Alexandria call through Firecrawl's scrape endpoint, the same
    request as _flights.alexandria_fetch. Returns the entry's data."""
    key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    if not key:
        raise StayError("FIRECRAWL_API_KEY is not set")
    body = json.dumps({"alexandria": {"provider": provider, "capability": capability,
                                      "options": options}}).encode("utf8")
    req = urllib.request.Request(flights.ENDPOINT, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": flights.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=flights.FETCH_TIMEOUT_S) as resp:
            raw = resp.read(flights.MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as e:
        e.close()
        raise StayError(f"source HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise StayError(flights._category(e.reason).replace("fare source", "source")) from None
    except Exception as e:  # noqa: BLE001 - anything that goes wrong is "could not check"
        raise StayError(flights._category(e).replace("fare source", "source")) from None
    try:
        payload = json.loads(raw.decode("utf8", "ignore"))
    except ValueError:
        raise StayError("source sent something that is not JSON") from None
    return entry_data(payload)


def entry_data(payload) -> dict:
    if not isinstance(payload, dict) or payload.get("success") is False:
        raise StayError("source reported a failure")
    entry = (payload.get("data") or {}).get("alexandria") if isinstance(payload.get("data"), dict) else None
    if isinstance(entry, list):
        entry = entry[0] if entry else None
    if not isinstance(entry, dict) or entry.get("error"):
        raise StayError("source reported a failure")
    data = entry.get("data")
    if not isinstance(data, dict):
        raise StayError("source sent an unexpected response")
    return data


def alexandria_fetch(source: str, params: dict) -> Search:
    if source == "booking":
        return parse_booking(_call(*BOOKING, booking_options(params)), params)
    region_id = params.get("vrbo_region_id")
    place = params.get("vrbo_place") or ""
    if not region_id:
        region_id, place = pick_region(_call(*VRBO_LOCATIONS, {"query": params["where"]}), params)
    got = parse_vrbo(_call(*VRBO_SEARCH, vrbo_options(params, region_id)), params)
    got.region_id, got.place = region_id, place
    return got


def pick_region(data: dict, params: dict) -> tuple[str, str]:
    """The first destination in the asked country. Never the first one
    anywhere: a same-name town abroad would be watched without a word."""
    dests = data.get("destinations")
    if not isinstance(dests, list) or not dests:
        raise StayError("Vrbo found no destination for that place")
    for d in dests:
        if not isinstance(d, dict):
            continue
        cc = d.get("country_code")
        if params.get("country") and not (isinstance(cc, str) and cc.lower() == params["country"]):
            continue
        rid = d.get("region_id")
        if isinstance(rid, str) and _DIGITS.fullmatch(rid):
            return rid, label(d.get("name"), 100)
    raise StayError("Vrbo found no destination for that place in that country")


def _amount(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def parse_booking(data: dict, params: dict) -> Search:
    """A response that is not a search of the asked place raises; a property
    that does not validate is skipped."""
    results = data.get("results")
    if not isinstance(results, list):
        raise StayError("Booking.com sent an unexpected response")
    if data.get("name_match") is False:
        raise StayError("Booking.com could not find that place, only somewhere near it")
    dest = data.get("destination") if isinstance(data.get("destination"), dict) else {}
    cc = dest.get("country_code")
    if params.get("country") and isinstance(cc, str) and cc.lower() != params["country"]:
        raise StayError("Booking.com matched that place in another country")
    if data.get("currency") not in (None, params["currency"]):
        raise StayError(f"Booking.com did not answer in {params['currency']}")
    resolved = label(data.get("resolved_name"), 80)
    where = label(data.get("resolved_location"), 80)
    country = params.get("country") or (cc.lower() if isinstance(cc, str) and _COUNTRY.fullmatch(cc.lower()) else "")
    out = []
    for item in results:
        if not isinstance(item, dict) or item.get("is_sold_out") is True or item.get("is_closed") is True:
            continue
        price = item.get("price") if isinstance(item.get("price"), dict) else {}
        total = _amount(price.get("amount"))
        if total is None or price.get("currency") != params["currency"]:
            continue
        page = item.get("page_name")
        if not isinstance(page, str) or not _PAGE_NAME.fullmatch(page) or not country:
            continue
        score = item.get("review_score")
        rating = float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) \
            and math.isfinite(score) and 0 < score <= 10 else None
        count = item.get("review_count")
        reviews = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None
        if reviews == 0:
            rating = None
        type_id = item.get("accommodation_type_id")
        dist = item.get("distance_from_center")
        q = urllib.parse.urlencode({"checkin": params["checkin"], "checkout": params["checkout"],
                                    "group_adults": params["adults"], "no_rooms": 1,
                                    "selected_currency": params["currency"]})
        out.append(Option(source="booking", name=label(item.get("name")) or "name not given", total=total,
                          currency=params["currency"], nightly=total / params["nights"], rating=rating,
                          reviews=reviews, link=f"https://www.booking.com/hotel/{country}/{page}.html?{q}",
                          kind=BOOKING_TYPES.get(type_id, "") if isinstance(type_id, int) else "",
                          area=dist if isinstance(dist, str) and _DISTANCE.fullmatch(dist) else ""))
    place = ", ".join(p for p in (resolved, where) if p)
    return Search(out, place=place)


def _dollars(m: re.Match) -> float:
    return float(m.group(1).replace(",", "") + (m.group(2) or ""))


def parse_vrbo(data: dict, params: dict) -> Search:
    props = data.get("properties")
    if not isinstance(props, list):
        raise StayError("Vrbo sent an unexpected response")
    out = []
    for item in props:
        if not isinstance(item, dict):
            continue
        price = item.get("price") if isinstance(item.get("price"), dict) else {}
        if price.get("currency") not in (None, VRBO_CURRENCY):
            continue
        m = _VRBO_TOTAL.fullmatch(str(price.get("total") or "").strip())
        if not m or int(m.group(3)) != params["nights"]:
            continue
        total = _dollars(m)
        if total <= 0:
            continue
        n = _VRBO_NIGHTLY.fullmatch(str(price.get("nightly") or "").strip())
        nightly = _dollars(n) if n else total / params["nights"]
        listing, prop = item.get("listing_id"), item.get("property_id")
        q = {"chkin": params["checkin"], "chkout": params["checkout"], "adults": params["adults"]}
        if isinstance(prop, str) and _DIGITS.fullmatch(prop):
            q["expediaPropertyId"] = prop
        if isinstance(listing, str) and _VRBO_LISTING.fullmatch(listing):
            path = listing
        elif isinstance(prop, str) and _DIGITS.fullmatch(prop):
            path = f"pdp/lo/{prop}"
        else:
            continue
        rating = reviews = None
        r = item.get("rating") if isinstance(item.get("rating"), dict) else {}
        try:
            score = float(str(r.get("score")))
        except ValueError:
            score = None  # "New", or nothing
        if score is not None and math.isfinite(score) and 0 < score <= 10:
            rating = score
        count = r.get("review_count")
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            reviews = count
        if reviews == 0:
            rating = None
        kind = label(str(item.get("summary") or "").split("·")[0], 30)
        out.append(Option(source="vrbo", name=label(item.get("name")) or "name not given", total=total,
                          currency=VRBO_CURRENCY, nightly=nightly, rating=rating, reviews=reviews,
                          link=f"https://www.vrbo.com/{path}?" + urllib.parse.urlencode(q),
                          kind=kind, area=label(item.get("neighborhood"), 40)))
    return Search(out)


# ---- the check -----------------------------------------------------------------

@dataclass
class SourceResult:
    source: str
    error: str = ""             # our wording; set when this source could not check
    best: Option | None = None
    top: list = field(default_factory=list)
    met: bool = False
    first: bool = False         # no price recorded before this one
    line: str = ""              # its part of the result line
    detail: str = ""            # its lines for an alert
    region_id: str = ""
    place: str = ""


@dataclass
class Result:
    outcome: str  # met | not_met | baseline | could_not_check
    result: str
    sources: list = field(default_factory=list)


def eligible(options: list[Option], params: dict) -> list[Option]:
    out = []
    for o in options:
        if o.currency != source_currency(params, o.source):
            continue
        if params.get("min_rating") is not None:
            if o.rating is None:
                if not params.get("allow_unrated"):
                    continue
            elif o.rating < params["min_rating"]:
                continue
        out.append(o)
    return out


def rating_text(o: Option) -> str:
    if o.rating is None:
        return "unrated"
    count = "review count not given" if o.reviews is None else f"{o.reviews} review" + ("s" if o.reviews != 1 else "")
    return f"rated {o.rating:g}/10 ({count})"


def option_line(o: Option, nights: int) -> str:
    taxes = " before taxes and charges" if o.source == "booking" else ", all fees included"
    head = f'"{o.name}" on {SOURCES[o.source]}: {flights.money(round(o.total, 2), o.currency)} for {nights} night' \
           + ("s" if nights != 1 else "") + f"{taxes} ({flights.money(round(o.nightly or 0), o.currency)}/night)"
    extra = ", ".join(p for p in (o.kind, o.area) if p)
    return f"{head}, {rating_text(o)}" + (f", {extra}" if extra else "")


def top_dict(o: Option) -> dict:
    return {"source": o.source, "source_label": SOURCES[o.source], "name": o.name, "total": round(o.total, 2),
            "currency": o.currency, "nightly": round(o.nightly, 2) if o.nightly else None, "rating": o.rating,
            "reviews": o.reviews, "rating_label": rating_text(o), "link": o.link,
            "price_label": flights.money(round(o.total), o.currency)}


def _one(source: str, rec: dict, fetch: StayFetch, now: datetime) -> SourceResult:
    params = rec["params"]
    name = SOURCES[source]
    try:
        got = fetch(source, params)
    except StayError as e:
        return SourceResult(source, error=str(e), line=f"{name} could not check: {e}")
    if not isinstance(got, Search) or not got.options:
        # Never "nothing available": an empty page is far more often a scraper problem.
        why = "it returned no stays"
        return SourceResult(source, error=why, line=f"{name} could not check: {why}")
    ok = eligible(got.options, params)
    if not ok:
        why = (f"none of the {len(got.options)} stays it returned is "
               + (f"rated {params['min_rating']:g}+" if params.get("min_rating") else "usable")
               + f" in {source_currency(params, source)}")
        return SourceResult(source, error=why, line=f"{name} could not check: {why}")
    ok.sort(key=lambda o: o.total)
    best = ok[0]
    state = (rec.get("stay_state") or {}).get(source) or new_state(source_currency(params, source))
    cur = state["currency"]
    res = SourceResult(source, best=best, top=[top_dict(o) for o in ok[:TOP_N]],
                       region_id=got.region_id, place=got.place)
    ref, how = reference(state, now)
    price = flights.money(round(best.total), cur)
    if ref is None:
        res.line = f"{name} cheapest {price}, the first price seen"
        res.first = True
        return res
    pct = (ref - best.total) / ref * 100
    short = "14-day average" if how.startswith("the 14-day") else "first price"
    res.line = (f"{name} cheapest {price}, {abs(pct):.0f}% {'below' if pct >= 0 else 'above'} "
                f"{short} {flights.money(round(ref), cur)}")
    gate = {"drop_pct": params["drop_pct"],
            "max_price": params.get("max_price") if cur == params["currency"] else None}
    if flights.qualifies(best.total, gate, ref, state.get("last_alerted_price")):
        res.met = True
        lines = [f"{name}: {price}, {pct:.0f}% below {how}"]
        if state.get("last_alerted_price") is not None:
            lines.append(f"Previous {name} alert: {flights.money(round(state['last_alerted_price']), cur)}")
        lines.append(option_line(best, params["nights"]))
        lines.append(best.link)
        res.detail = "\n".join(lines)
    return res


def observe(rec: dict, fetch: StayFetch | None = None, now: datetime | None = None) -> Result:
    """Decided against the record as read. apply repeats the last-alerted test
    on a fresh read, so an overlapping run cannot double up."""
    now = now or datetime.now(timezone.utc)
    results = [_one(s, rec, fetch or alexandria_fetch, now) for s in rec["params"]["sources"]]
    line = "; ".join(r.line for r in results)
    if all(r.error for r in results):
        return Result("could_not_check", "could not check: " + line, results)
    if any(r.met for r in results):
        outcome = "met"
    elif any(r.first for r in results):
        outcome = "baseline"
    else:
        outcome = "not_met"
    return Result(outcome, line, results)


def apply(rec: dict, got: Result, now: datetime, fired_text: Callable[[dict, str], str],
          failure_after: int) -> None:
    """Record each source on a fresh read of the record. Alerts are queued
    here, not by the generic path: one message per check, covering every source
    whose price is still below the last one it alerted at."""
    params = rec["params"]
    store = rec.setdefault("stay_state", {})
    worked = any(not r.error for r in got.sources)
    lines = []
    for r in got.sources:
        state = store.setdefault(r.source, new_state(source_currency(params, r.source)))
        if r.error:
            state["consecutive_failures"] += 1
            state["last_error"] = r.error
            if (worked and state["consecutive_failures"] >= failure_after
                    and not state["failure_alerted"]):
                state["failure_alerted"] = True
                rec["outbox"].append({"kind": "failure", "text": (
                    f"TripWire cannot see {SOURCES[r.source]}: {rec['name']}. {state['consecutive_failures']} "
                    f"checks in a row failed there (last: {r.error}), while "
                    f"{' and '.join(SOURCES[x.source] for x in got.sources if not x.error)} worked. It keeps "
                    f"watching what it can, but treat {SOURCES[r.source]} as unwatched until this clears. "
                    f"This is the only alert for this run of failures.\n{params['url']}")})
            continue
        state["consecutive_failures"], state["failure_alerted"], state["last_error"] = 0, False, None
        state["top"] = r.top
        if r.source == "vrbo" and r.region_id and not params.get("vrbo_region_id"):
            params["vrbo_region_id"], params["vrbo_place"] = r.region_id, r.place
            params["vrbo_url"] = vrbo_search_url(params, r.region_id)
        flights.record_price(state, r.best.total, now)
        last = state.get("last_alerted_price")
        if r.met and (last is None or r.best.total < last):
            state["last_alerted_price"] = r.best.total
            lines.append(r.detail)
    if lines:
        head = [f"Option: {params['group']}"] if params.get("group") else []
        rec["fire_count"] += 1
        rec["outbox"].append({"kind": "refire", "text": fired_text(rec, "\n".join(head + lines))})


def degraded(rec: dict) -> list[str]:
    """Sources whose last check failed while the tripwire as a whole worked."""
    if rec.get("last_outcome") == "could_not_check":
        return []
    return [SOURCES[s] for s, st in (rec.get("stay_state") or {}).items()
            if s in SOURCES and st.get("consecutive_failures")]
