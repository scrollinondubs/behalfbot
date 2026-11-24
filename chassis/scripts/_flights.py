"""The `flight_price` TripWire: the cheapest economy fare for one one-way leg,
optionally over a few days either side of the date, compared with what the
fare has been.

"What it has been" is the mean of the successful checks in the last 14 days.
Until the first check is 3 days old that mean is too thin, so the first fare
seen stands in, and the alert says which one it used.

The fare source is a function, so it can be swapped: it takes the tripwire's
params for one date and returns a list of Fare, or raises FlightError. The
default asks Firecrawl's Alexandria provider `flights-google-com` (Google
Flights) with FIRECRAWL_API_KEY from the environment. The key is never printed
or stored.

Standard library only, like _tripwires.py.

Fares are untrusted: they come from a third party scraping a third party. Only
validated fields reach the store or an alert: IATA codes, ISO times, numbers,
and an airline name limited to plain letters. Anything else is dropped.

The source shows no fare family, so `fare_class: main` (no Basic Economy) is
recorded and repeated in the alert as unverified. It filters nothing.
"""
from __future__ import annotations

import json
import math
import os
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable

MAX_STOPS = {"nonstop": 0, "one": 1}
FARE_CLASSES = ("any", "main")
DEFAULT_DROP_PCT = 15.0
MAX_FLEX_DAYS = 3
AVG_WINDOW = timedelta(days=14)
MIN_HISTORY = timedelta(days=3)
MAX_HISTORY = 200
RESULT_LIMIT = 50  # the cheapest fare is often far down Google's first page
FETCH_TIMEOUT_S = 60
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
ENDPOINT = "https://api.firecrawl.dev/v2/scrape"
PROVIDER, CAPABILITY = "flights-google-com", "flights/search_flights"
USER_AGENT = "BehalfBot-TripWire/1.0"

_IATA = re.compile(r"[A-Z]{3}")
_AIRLINE_CODE = re.compile(r"[A-Z0-9]{2}")
_AIRLINE_NAME = re.compile(r"[A-Za-z][A-Za-z .&'-]{0,39}")
_CURRENCY = re.compile(r"[A-Z]{3}")
_MARKET = re.compile(r"[A-Z]{2}")
_GROUP = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}")
_LOCAL_TIME = re.compile(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(:\d{2})?")


class FlightError(Exception):
    """Bad params, or a fare source that gave no usable answer. The message is
    our own wording, fit to show."""


@dataclass
class Fare:
    price: float
    currency: str
    stops: int
    economy: bool          # every segment economy, and the source says the cabin matched
    airlines: list = field(default_factory=list)
    via: list = field(default_factory=list)
    duration_min: int | None = None
    departs: str = ""      # "2026-12-15 10:15", local to the origin
    day: str = ""          # the date searched; set by observe


FlightFetch = Callable[[dict], "list[Fare]"]


# ---- params --------------------------------------------------------------------

def _airport(value: str | None, label: str) -> str:
    out = (value or "").strip().upper()
    if not _IATA.fullmatch(out):
        raise FlightError(f"{label} must be a 3-letter airport code such as JFK")
    return out


def validate_params(*, origin: str | None, destination: str | None, date_str: str | None,
                    max_stops: str | None = "one", currency: str | None = "EUR",
                    drop_pct: float | str | None = DEFAULT_DROP_PCT, max_price: float | str | None = None,
                    fare_class: str | None = "any", market: str | None = None, flex_days: int | str | None = 0,
                    group: str | None = None, max_duration_hours: float | str | None = None,
                    today: date | None = None) -> dict:
    params: dict = {"origin": _airport(origin, "from"), "destination": _airport(destination, "to")}
    if params["origin"] == params["destination"]:
        raise FlightError("from and to must differ")
    try:
        day = date.fromisoformat((date_str or "").strip())
    except ValueError:
        raise FlightError("date must look like 2026-12-15") from None
    try:
        params["flex_days"] = int(flex_days or 0)
    except (TypeError, ValueError):
        raise FlightError("flex days must be a whole number") from None
    if not 0 <= params["flex_days"] <= MAX_FLEX_DAYS:
        raise FlightError(f"flex days must be between 0 and {MAX_FLEX_DAYS}")
    if day + timedelta(days=params["flex_days"]) < (today or date.today()):
        raise FlightError("the flight date is already in the past")
    params["date"] = day.isoformat()
    if (max_stops or "one") not in MAX_STOPS:
        raise FlightError("max stops must be nonstop or one")
    params["max_stops"] = max_stops or "one"
    params["currency"] = (currency or "EUR").strip().upper()
    if not _CURRENCY.fullmatch(params["currency"]):
        raise FlightError("currency must be a 3-letter code such as USD")
    params["drop_pct"] = _number(DEFAULT_DROP_PCT if drop_pct in (None, "") else drop_pct, "drop percent")
    if not 1 <= params["drop_pct"] <= 90:
        raise FlightError("drop percent must be between 1 and 90")
    params["max_price"] = None if max_price in (None, "") else _number(max_price, "max price")
    if params["max_price"] is not None and params["max_price"] <= 0:
        raise FlightError("max price must be above 0")
    if (fare_class or "any") not in FARE_CLASSES:
        raise FlightError("fare class must be any or main")
    params["fare_class"] = fare_class or "any"
    params["market"] = (market or "").strip().upper() or None
    if params["market"] and not _MARKET.fullmatch(params["market"]):
        raise FlightError("market must be a 2-letter country code such as US")
    params["group"] = (group or "").strip() or None
    if params["group"] and not _GROUP.fullmatch(params["group"]):
        raise FlightError("group must be up to 40 letters, digits, spaces, dots, dashes or underscores")
    params["max_duration_hours"] = (None if max_duration_hours in (None, "")
                                    else _number(max_duration_hours, "max duration hours"))
    if params["max_duration_hours"] is not None and not 1 <= params["max_duration_hours"] <= 72:
        raise FlightError("max duration hours must be between 1 and 72")
    params["url"] = google_flights_url(params)
    return params


def _number(value, label: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise FlightError(f"{label} must be a number") from None
    if not math.isfinite(out):
        raise FlightError(f"{label} must be a number")
    return out


def search_dates(params: dict, today: date | None = None) -> list[str]:
    """The date, and flex_days either side of it that are not yet past."""
    center, flex = date.fromisoformat(params["date"]), params.get("flex_days") or 0
    floor = today or date.today()
    days = [center + timedelta(days=d) for d in range(-flex, flex + 1)]
    return [d.isoformat() for d in days if d >= floor]


def google_flights_url(params: dict, day: str | None = None) -> str:
    """Built from our own validated params, never from a response."""
    q = f"Flights to {params['destination']} from {params['origin']} on {day or params['date']} oneway economy"
    return ("https://www.google.com/travel/flights?"
            + urllib.parse.urlencode({"q": q, "curr": params["currency"], "hl": "en"}, quote_via=urllib.parse.quote))


def leg(params: dict) -> str:
    flex = params.get("flex_days") or 0
    return f"{params['origin']}-{params['destination']} {params['date']}" + (f" ±{flex}d" if flex else "")


def stops_label(n: int) -> str:
    return "nonstop" if n == 0 else f"{n} stop" + ("s" if n > 1 else "")


def summary(params: dict) -> str:
    """One line for the tab and `list`."""
    out = (f"{leg(params)}, economy, {'nonstop' if params['max_stops'] == 'nonstop' else 'max 1 stop'}, "
           f"alert at {params['drop_pct']:g}% below the 14-day average")
    if params.get("max_price") is not None:
        out += f" and at most {money(params['max_price'], params['currency'])}"
    if params.get("max_duration_hours") is not None:
        out += f", trips up to {params['max_duration_hours']:g}h"
    if params.get("fare_class") == "main":
        out += ", wants main cabin (unverified)"
    return out


def money(amount: float | None, currency: str) -> str:
    if amount is None:
        return "-"
    return f"{currency} {amount:,.0f}" if float(amount).is_integer() else f"{currency} {amount:,.2f}"


# ---- history -------------------------------------------------------------------

def _at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def record_price(rec: dict, price: float, now: datetime) -> None:
    """Keep a successful check: first fare, lowest, and the bounded history
    the average is taken over."""
    stamp = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if rec.get("baseline_price") is None:
        rec["baseline_price"], rec["baseline_at"] = price, stamp
    if rec.get("lowest_price") is None or price < rec["lowest_price"]:
        rec["lowest_price"] = price
    history = [h for h in rec.get("history") or [] if now - _at(h["at"]) <= AVG_WINDOW]
    history.append({"at": stamp, "price": price})
    rec["history"] = history[-MAX_HISTORY:]


def reference(rec: dict, now: datetime) -> tuple[float | None, str]:
    """What a fare is compared with, and how to say so. Past checks only."""
    cur = rec["params"]["currency"]
    first, first_at = rec.get("baseline_price"), rec.get("baseline_at")
    if first is None:
        return None, ""
    window = [h["price"] for h in rec.get("history") or [] if now - _at(h["at"]) <= AVG_WINDOW]
    if now - _at(first_at) < MIN_HISTORY or not window:
        return first, (f"the first fare seen, {money(first, cur)} on {first_at[:10]} "
                       f"(under 3 days of history, so no average yet)")
    avg = sum(window) / len(window)
    checks = f"{len(window)} check" + ("s" if len(window) != 1 else "")
    return avg, f"the 14-day average {money(round(avg), cur)} ({checks})"


def fares_label(rec: dict, now: datetime) -> str:
    """The tab's line of numbers."""
    cur = rec["params"]["currency"]
    ref, how = reference(rec, now)
    if ref is None:
        compared = "no fare yet"
    elif how.startswith("the 14-day"):
        compared = f"14-day average {money(round(ref), cur)}"
    else:
        compared = "average after 3 days of checks"
    return (f"{compared}, first {money(rec.get('baseline_price'), cur)}, "
            f"lowest {money(rec.get('lowest_price'), cur)}, last alert {money(rec.get('last_alerted_price'), cur)}")


# ---- the default fare source ---------------------------------------------------

def alexandria_options(params: dict) -> dict:
    options = {"origin": params["origin"], "destination": params["destination"],
               "departure_date": params["date"], "currency": params["currency"],
               "cabin_class": "economy", "max_stops": MAX_STOPS[params["max_stops"]], "limit": RESULT_LIMIT}
    if params.get("market"):
        options["market"] = params["market"]
    return options


def alexandria_fetch(params: dict) -> list[Fare]:
    """POST to Firecrawl's scrape endpoint with an `alexandria` body. Every
    failure is a FlightError naming a category of ours, never remote text."""
    key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    if not key:
        raise FlightError("FIRECRAWL_API_KEY is not set")
    body = json.dumps({"alexandria": {"provider": PROVIDER, "capability": CAPABILITY,
                                      "options": alexandria_options(params)}}).encode("utf8")
    req = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as e:
        e.close()
        raise FlightError(f"fare source HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise FlightError(_category(e.reason)) from None
    except Exception as e:  # noqa: BLE001 - anything that goes wrong is "could not check"
        raise FlightError(_category(e)) from None
    try:
        payload = json.loads(raw.decode("utf8", "ignore"))
    except ValueError:
        raise FlightError("fare source sent something that is not JSON") from None
    return parse_alexandria(payload)


def _category(exc: object) -> str:
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "fare source timeout"
    if isinstance(exc, socket.gaierror):
        return "fare source DNS failure"
    if isinstance(exc, ssl.SSLError):
        return "fare source TLS failure"
    return "fare source connection failed"


def parse_alexandria(payload) -> list[Fare]:
    """The fares in a scrape response. A response that is not a successful
    search raises; a fare that does not validate is skipped."""
    if not isinstance(payload, dict) or payload.get("success") is False:
        raise FlightError("fare source reported a failure")
    entry = (payload.get("data") or {}).get("alexandria") if isinstance(payload.get("data"), dict) else None
    if isinstance(entry, list):
        entry = entry[0] if entry else None
    if not isinstance(entry, dict) or entry.get("error"):
        raise FlightError("fare source reported a failure")
    data = entry.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("flights"), list):
        raise FlightError("fare source sent an unexpected response")
    return [f for f in (_fare(item) for item in data["flights"]) if f is not None]


def _fare(item) -> Fare | None:
    if not isinstance(item, dict):
        return None
    price = item.get("price") if isinstance(item.get("price"), dict) else {}
    amount, currency = price.get("amount"), price.get("currency")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount <= 0:
        return None
    if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency):
        return None
    stops = item.get("stops")
    if isinstance(stops, bool) or not isinstance(stops, int) or stops < 0:
        return None
    segments = item.get("segments")
    if not isinstance(segments, list) or not segments or not all(isinstance(s, dict) for s in segments):
        return None
    economy = item.get("cabin_matches_request") is True and all(s.get("cabin_class") == "economy" for s in segments)
    airlines: list[str] = []
    for s in segments:
        name = s.get("airline_name")
        code = s.get("airline")
        label = name if isinstance(name, str) and _AIRLINE_NAME.fullmatch(name) else (
            code if isinstance(code, str) and _AIRLINE_CODE.fullmatch(code) else None)
        if label and label not in airlines:
            airlines.append(label)
    via = [s.get("destination") for s in segments[:-1]]
    via = [v for v in via if isinstance(v, str) and _IATA.fullmatch(v)]
    duration = item.get("duration_minutes")
    if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
        duration = None
    m = _LOCAL_TIME.fullmatch(str(segments[0].get("departure_local") or ""))
    return Fare(price=float(amount), currency=currency, stops=stops, economy=economy, airlines=airlines,
                via=via, duration_min=duration, departs=f"{m.group(1)} {m.group(2)}" if m else "")


# ---- the check -----------------------------------------------------------------

@dataclass
class Result:
    outcome: str  # met | not_met | baseline | could_not_check
    result: str
    price: float | None = None
    detail: str = ""


def eligible(fares: list[Fare], params: dict) -> list[Fare]:
    """A duration cap also drops fares whose duration is not given: a fare that
    cannot be shown to fit must not set the price."""
    limit = MAX_STOPS[params["max_stops"]]
    cap = params.get("max_duration_hours")
    return [f for f in fares if f.economy and f.stops <= limit and f.currency == params["currency"]
            and (cap is None or (f.duration_min is not None and f.duration_min <= cap * 60))]


def fare_line(f: Fare) -> str:
    parts = [" + ".join(f.airlines) or "airline not given"]
    parts.append(stops_label(f.stops) + (f" via {', '.join(f.via)}" if f.via else ""))
    if f.duration_min:
        parts.append(f"{f.duration_min // 60}h {f.duration_min % 60:02d}m")
    parts.append(f"departs {f.departs}" if f.departs else f"on {f.day}")
    return ", ".join(parts)


def qualifies(price: float, params: dict, ref: float, last_alerted: float | None) -> bool:
    """Alert when the fare is drop_pct below the reference, never above
    max_price, and only below the last price already alerted."""
    if price > ref * (1 - params["drop_pct"] / 100):
        return False
    if params.get("max_price") is not None and price > params["max_price"]:
        return False
    return last_alerted is None or price < last_alerted


def _cheapest(params: dict, fetch: FlightFetch, today: date) -> tuple[Fare | None, str]:
    """The cheapest eligible fare over every search date, or why there is none.
    Every date must answer: a day that failed could have held the cheapest
    fare, so a partial look is not a look."""
    days = search_dates(params, today)
    if not days:
        return None, "could not check: every search date is in the past"
    best = None
    for day in days:
        try:
            fares = fetch({**params, "date": day})
        except FlightError as e:
            return None, f"could not check: {e}" + (f" for {day}" if len(days) > 1 else "")
        if not fares:
            # Never "no flights": an empty page is far more often a scraper problem.
            return None, f"could not check: the fare source returned no fares for {day}"
        ok = eligible(fares, params)
        if not ok:
            return None, (f"could not check: no economy fare in {params['currency']} with "
                          f"{'no stops' if params['max_stops'] == 'nonstop' else 'at most 1 stop'}"
                          + (f" within {params['max_duration_hours']:g}h" if params.get("max_duration_hours") else "")
                          + f" among {len(fares)} returned for {day}")
        for f in ok:
            f.day = day
        cheapest = min(ok, key=lambda f: f.price)
        if best is None or cheapest.price < best.price:
            best = cheapest
    return best, ""


def observe(rec: dict, fetch: FlightFetch | None = None, now: datetime | None = None) -> Result:
    """Decided against the record as read. apply_observation repeats the
    last-alerted test on a fresh read, so an overlapping run cannot double up."""
    params = rec["params"]
    now = now or datetime.now(timezone.utc)
    best, why = _cheapest(params, fetch or alexandria_fetch, now.astimezone().date())
    if best is None:
        return Result("could_not_check", why)
    cur = params["currency"]
    on_day = f" on {best.day}" if params.get("flex_days") else ""
    ref, how = reference(rec, now)
    if ref is None:
        return Result("baseline", f"cheapest {money(best.price, cur)}{on_day}, the first fare seen",
                      best.price, fare_line(best))
    pct = (ref - best.price) / ref * 100
    short = "14-day average" if how.startswith("the 14-day") else "first fare"
    result = (f"cheapest {money(best.price, cur)}{on_day}, {abs(pct):.0f}% "
              f"{'below' if pct >= 0 else 'above'} {short} {money(round(ref), cur)}")
    if not qualifies(best.price, params, ref, rec.get("last_alerted_price")):
        return Result("not_met", result, best.price, fare_line(best))
    lines = []
    if params.get("group"):
        lines.append(f"Option: {params['group']}")
    lines.append(f"{money(best.price, cur)}{on_day}, {pct:.0f}% below {how}")
    if rec.get("last_alerted_price") is not None:
        lines.append(f"Previous alert: {money(rec['last_alerted_price'], cur)}")
    lines.append(fare_line(best))
    if params.get("fare_class") == "main":
        lines.append("Fare family not verified: this may be Basic Economy, which can block seat selection. "
                     "Check before booking.")
    if best.day != params["date"]:
        lines.append(f"Google Flights for {best.day}: {google_flights_url(params, best.day)}")
    return Result("met", result, best.price, "\n".join(lines))
