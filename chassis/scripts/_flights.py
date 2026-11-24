"""The `flight_price` TripWire: the cheapest economy fare for one one-way leg,
compared with the first fare it saw.

The fare source is a function, so it can be swapped: it takes the tripwire's
params and returns a list of Fare, or raises FlightError. The default asks
Firecrawl's Alexandria provider `flights-google-com` (Google Flights) with
FIRECRAWL_API_KEY from the environment. The key is never printed or stored.

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
from datetime import date
from typing import Callable

MAX_STOPS = {"nonstop": 0, "one": 1}
FARE_CLASSES = ("any", "main")
DEFAULT_DROP_PCT = 15.0
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


FlightFetch = Callable[[dict], "list[Fare]"]


# ---- params --------------------------------------------------------------------

def _airport(value: str | None, label: str) -> str:
    out = (value or "").strip().upper()
    if not _IATA.fullmatch(out):
        raise FlightError(f"{label} must be a 3-letter airport code such as LIS")
    return out


def validate_params(*, origin: str | None, destination: str | None, date_str: str | None,
                    max_stops: str | None = "one", currency: str | None = "EUR",
                    drop_pct: float | str | None = DEFAULT_DROP_PCT, max_price: float | str | None = None,
                    fare_class: str | None = "any", market: str | None = None,
                    today: date | None = None) -> dict:
    params: dict = {"origin": _airport(origin, "from"), "destination": _airport(destination, "to")}
    if params["origin"] == params["destination"]:
        raise FlightError("from and to must differ")
    try:
        day = date.fromisoformat((date_str or "").strip())
    except ValueError:
        raise FlightError("date must look like 2026-12-15") from None
    if day < (today or date.today()):
        raise FlightError("the flight date is already in the past")
    params["date"] = day.isoformat()
    if (max_stops or "one") not in MAX_STOPS:
        raise FlightError("max stops must be nonstop or one")
    params["max_stops"] = max_stops or "one"
    params["currency"] = (currency or "EUR").strip().upper()
    if not _CURRENCY.fullmatch(params["currency"]):
        raise FlightError("currency must be a 3-letter code such as EUR")
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
        raise FlightError("market must be a 2-letter country code such as PT")
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


def google_flights_url(params: dict) -> str:
    """Built from our own validated params, never from a response."""
    q = f"Flights to {params['destination']} from {params['origin']} on {params['date']} oneway economy"
    return ("https://www.google.com/travel/flights?"
            + urllib.parse.urlencode({"q": q, "curr": params["currency"], "hl": "en"}, quote_via=urllib.parse.quote))


def leg(params: dict) -> str:
    return f"{params['origin']}-{params['destination']} {params['date']}"


def stops_label(n: int) -> str:
    return "nonstop" if n == 0 else f"{n} stop" + ("s" if n > 1 else "")


def summary(params: dict) -> str:
    """One line for the tab and `list`."""
    out = (f"{leg(params)}, economy, {'nonstop' if params['max_stops'] == 'nonstop' else 'max 1 stop'}, "
           f"alert at {params['drop_pct']:g}% below baseline")
    if params.get("max_price") is not None:
        out += f" and at most {money(params['max_price'], params['currency'])}"
    if params.get("fare_class") == "main":
        out += ", wants main cabin (unverified)"
    return out


def money(amount: float | None, currency: str) -> str:
    if amount is None:
        return "-"
    return f"{currency} {amount:,.0f}" if float(amount).is_integer() else f"{currency} {amount:,.2f}"


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
    limit = MAX_STOPS[params["max_stops"]]
    return [f for f in fares if f.economy and f.stops <= limit and f.currency == params["currency"]]


def fare_line(f: Fare) -> str:
    parts = [" + ".join(f.airlines) or "airline not given"]
    parts.append(stops_label(f.stops) + (f" via {', '.join(f.via)}" if f.via else ""))
    if f.duration_min:
        parts.append(f"{f.duration_min // 60}h {f.duration_min % 60:02d}m")
    if f.departs:
        parts.append(f"departs {f.departs}")
    return ", ".join(parts)


def qualifies(price: float, params: dict, baseline: float, last_alerted: float | None) -> bool:
    """Alert when the fare is drop_pct below the baseline, never above
    max_price, and only below the last price already alerted."""
    if price > baseline * (1 - params["drop_pct"] / 100):
        return False
    if params.get("max_price") is not None and price > params["max_price"]:
        return False
    return last_alerted is None or price < last_alerted


def observe(rec: dict, fetch: FlightFetch | None = None) -> Result:
    """Decided against the record as read. apply_observation repeats the
    last-alerted test on a fresh read, so an overlapping run cannot double up."""
    params = rec["params"]
    try:
        fares = (fetch or alexandria_fetch)(params)
    except FlightError as e:
        return Result("could_not_check", f"could not check: {e}")
    if not fares:
        # Never "no flights": an empty page is far more often a scraper problem.
        return Result("could_not_check", "could not check: the fare source returned no fares")
    ok = eligible(fares, params)
    if not ok:
        return Result("could_not_check", f"could not check: no economy fare in {params['currency']} with "
                                         f"{'no stops' if params['max_stops'] == 'nonstop' else 'at most 1 stop'} "
                                         f"among {len(fares)} returned")
    best = min(ok, key=lambda f: f.price)
    cur = params["currency"]
    baseline = rec.get("baseline_price")
    if baseline is None:
        return Result("baseline", f"cheapest {money(best.price, cur)}, recorded as the baseline",
                      best.price, fare_line(best))
    pct = (baseline - best.price) / baseline * 100
    change = f"{abs(pct):.0f}% {'below' if pct >= 0 else 'above'} baseline {money(baseline, cur)}"
    result = f"cheapest {money(best.price, cur)}, {change}"
    if not qualifies(best.price, params, baseline, rec.get("last_alerted_price")):
        return Result("not_met", result, best.price, fare_line(best))
    lines = [f"{money(best.price, cur)}, {change}" + (f" (first seen {rec['baseline_at'][:10]})"
                                                      if rec.get("baseline_at") else "")]
    if rec.get("last_alerted_price") is not None:
        lines.append(f"Previous alert: {money(rec['last_alerted_price'], cur)}")
    lines.append(fare_line(best))
    if params.get("fare_class") == "main":
        lines.append("Fare family not verified: this may be Basic Economy, which can block seat selection. "
                     "Check before booking.")
    return Result("met", result, best.price, "\n".join(lines))
