"""The `item_price` TripWire: a product, or the cheapest listing that matches a
search, across major US retailers, alerting when one is within the asked
price or drops well below what it has been.

Two modes. A search (`--query` plus filters) watches the cheapest qualifying
listing on each source: hardware deals such as "a Ryzen AI Max+ 395 mini PC
with 128GB under $3,299". A product (`--product URL_or_ID`) watches one
listing on one source for a price drop.

Two triggers, either or both:
- target: a qualifying listing at or below `target_price`. Every listing id
  under the target alerts once, even when the price logic would stay quiet,
  so a new listing at a deal price is never missed.
- drop: the cheapest qualifying price `drop_pct` below that source's
  reference, exactly as stay_price (_stays.py): the mean of the last 14 days
  of successful checks, or the first price until that is 3 days old.
After an alert a source alerts again on price only below the last price it
alerted at.

Each source is its own price series, like _stays.py: a merged "cheapest"
would read a source dropping out of one check as a price move. All sources
answer in USD; anything else is refused, never converted.

A source that errors, returns nothing, or returns nothing that passes the
filters is "could not check" for that source; the check as a whole is "could
not check" only when every source failed. A source failing 3 checks in a row
while another works sends one alert of its own.

The item source is a function, so it can be swapped: it takes a source name
and the tripwire's params and returns a Search, or raises ItemError. The
default asks Firecrawl's Alexandria shopping providers with FIRECRAWL_API_KEY
from the environment. The key is never printed or stored.

Untrusted: listings are third-party text. Prices, ids and ratings are
validated by type and pattern. Links are rebuilt from validated ids in each
retailer's canonical product URL format, never taken from a response (Micro
Center's alone needs its slug, which is kept only when it is plain lowercase
words and dashes). Titles are filtered on in full but kept only as a short
plain label (_stays.label): treat it as text written by a stranger.

Standard library only, like _tripwires.py.
"""
from __future__ import annotations

import concurrent.futures
import math
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import _flights as flights
import _stays as stays

SOURCES = {"amazon": "Amazon", "walmart": "Walmart", "bestbuy": "Best Buy", "newegg": "Newegg",
           "microcenter": "Micro Center"}
DEFAULT_SOURCES = ("amazon", "walmart", "bestbuy", "newegg")
CURRENCY = "USD"
CONDITIONS = ("new", "any")
DEFAULT_STORE = "029"  # Micro Center's "Shippable Items": web pricing, no store inventory
TOP_N = 3
MAX_TERMS = 10
MAX_ALERTED_IDS = 300
MAX_TITLE = 80
MAX_NEW_LINES = 3

SEARCH = {"amazon": ("amazon-com", "products/search"), "walmart": ("walmart-com", "products/search"),
          "bestbuy": ("bestbuy-com", "products/search"), "newegg": ("newegg-com", "products/search"),
          "microcenter": ("microcenter-com", "products/search_products")}
PRODUCT = {"amazon": ("amazon-com", "products/offer"), "walmart": ("walmart-com", "products/product"),
           "bestbuy": ("bestbuy-com", "products/pricing"), "newegg": ("newegg-com", "products/product"),
           "microcenter": ("microcenter-com", "products/get_product")}

_QUERY = re.compile(r"[^\W_][\w .,'()&/+#\"-]{0,119}")
_TERM = re.compile(r"[^\W_][\w .,'()&/+#-]{0,39}")
_STORE = re.compile(r"[0-9]{3}")
IDS = {"amazon": re.compile(r"[A-Z0-9]{10}"), "walmart": re.compile(r"[0-9]{1,20}"),
       "bestbuy": re.compile(r"[0-9]{6,8}"), "newegg": re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{3,23}[A-Za-z0-9]"),
       "microcenter": re.compile(r"[0-9]{1,8}")}
HOSTS = {"amazon": "amazon.com", "walmart": "walmart.com", "bestbuy": "bestbuy.com", "newegg": "newegg.com",
         "microcenter": "microcenter.com"}
_URL_IDS = {"amazon": re.compile(r"/(?:dp|gp/product)/([A-Za-z0-9]{10})(?:[/?#]|$)"),
            "walmart": re.compile(r"/ip/(?:[^/?#]+/)?([0-9]{1,20})(?:[/?#]|$)"),
            "bestbuy": re.compile(r"(?:[?&]skuId=|/)([0-9]{6,8})(?:\.p)?(?:[/?#&]|$)"),
            "newegg": re.compile(r"/p/([A-Za-z0-9-]{5,25})(?:[/?#]|$)"),
            "microcenter": re.compile(r"/product/([0-9]{1,8})(?:[/?#]|$)")}
_NEWEGG_DASHED = re.compile(r"([0-9]{2})-([0-9]{3})-([0-9]{3})([A-Z]?)")
_MC_SLUG = re.compile(r"/product/[0-9]{1,8}/([a-z0-9][a-z0-9-]{0,199})")
_NOT_NEW = re.compile(r"\b(renewed|refurbished|refurb|open[- ]box|pre-?owned)\b|\(used\)", re.I)


class ItemError(Exception):
    """Bad params, or a source that gave no usable answer. The message is our
    own wording, fit to show."""


@dataclass
class Offer:
    source: str
    id: str                    # validated per source
    title: str                 # the full title as sent, for --must/--exclude only; never kept
    price: float
    currency: str
    link: str
    in_stock: bool | None = None   # None: the source does not say
    condition: str = ""            # new | open_box | refurbished | used | "" when not said
    sponsored: bool = False
    rating: float | None = None    # out of 5
    reviews: int | None = None

    @property
    def label(self) -> str:
        return stays.label(self.title, MAX_TITLE) or "title not given"


@dataclass
class Search:
    offers: list = field(default_factory=list)
    returned: int = 0          # listings the source sent, valid or not


ItemFetch = Callable[[str, dict], Search]


# ---- params --------------------------------------------------------------------

def _terms(values, label: str) -> list[str]:
    out = []
    for v in values or []:
        v = " ".join(str(v).split())
        if not v:
            continue
        if not _TERM.fullmatch(v):
            raise ItemError(f"each {label} must be up to 40 letters, digits, spaces and . , ' ( ) & / + # -")
        out.append(v)
    if len(out) > MAX_TERMS:
        raise ItemError(f"at most {MAX_TERMS} {label} terms")
    return out


def _number(value, label: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ItemError(f"{label} must be a number") from None
    if isinstance(value, bool) or not math.isfinite(out):
        raise ItemError(f"{label} must be a number")
    return out


def _money(value, label: str) -> float | None:
    if value in (None, ""):
        return None
    out = _number(value, label)
    if out <= 0:
        raise ItemError(f"{label} must be a number above 0")
    return out


def parse_product(value: str | None, source: str | None) -> tuple[str, str]:
    """(source, id) from a product URL or a bare id. A URL names its own
    source; a bare id needs --source."""
    value = (value or "").strip()
    source = (source or "").strip().lower() or None
    if source and source not in SOURCES:
        raise ItemError(f"source must be one of {', '.join(SOURCES)}")
    if value.lower().startswith(("http://", "https://")):
        parts = urllib.parse.urlsplit(value)
        host = (parts.hostname or "").lower()
        found = next((s for s, h in HOSTS.items() if host == h or host.endswith("." + h)), None)
        if not found:
            raise ItemError(f"product URL must be on {', '.join(HOSTS.values())}")
        if source and source != found:
            raise ItemError(f"that URL is {SOURCES[found]}, not {SOURCES[source]}")
        m = _URL_IDS[found].search(parts.path + ("?" + parts.query if parts.query else ""))
        if not m:
            hint = " (a /product/... link has no SKU: pass --source bestbuy --product SKU)" if found == "bestbuy" else ""
            raise ItemError(f"cannot find the {SOURCES[found]} product id in that URL{hint}")
        source, pid = found, m.group(1)
    else:
        if not source:
            raise ItemError("a bare product id needs --source (amazon, walmart, bestbuy, newegg, microcenter)")
        pid = value
    if source == "amazon":
        pid = pid.upper()
    if not IDS[source].fullmatch(pid):
        raise ItemError(f"that is not a valid {SOURCES[source]} product id")
    return source, pid


def validate_params(*, query: str | None = None, product: str | None = None, source: str | None = None,
                    must=None, exclude=None, min_price=None, max_price=None, condition: str | None = "new",
                    sources: str | list | None = None, include_sponsored: bool = False,
                    store: str | None = None, target_price=None, drop_pct=None, currency: str | None = None,
                    group: str | None = None) -> dict:
    if bool((query or "").strip()) == bool((product or "").strip()):
        raise ItemError("give either --query (search mode) or --product (one product), not both")
    params: dict = {}
    params["currency"] = (currency or CURRENCY).strip().upper()
    if params["currency"] != CURRENCY:
        raise ItemError("these retailers price in USD only, so the currency must be USD")
    params["condition"] = (condition or "new").strip().lower()
    if params["condition"] not in CONDITIONS:
        raise ItemError("condition must be new or any")
    params["store"] = (store or DEFAULT_STORE).strip()
    if not _STORE.fullmatch(params["store"]):
        raise ItemError("store must be a 3-digit Micro Center store number such as 029")
    if product and product.strip():
        params["mode"] = "product"
        params["source"], params["product_id"] = parse_product(product, source)
        params["sources"] = [params["source"]]
        if must or exclude or min_price not in (None, "") or max_price not in (None, "") or sources:
            raise ItemError("--must, --exclude, --min-price, --max-price and --sources are for search mode")
        params["must"], params["exclude"], params["min_price"], params["max_price"] = [], [], None, None
        params["include_sponsored"] = True
    else:
        params["mode"] = "search"
        q = " ".join((query or "").split())
        if not _QUERY.fullmatch(q):
            raise ItemError("query must be up to 120 letters, digits, spaces and . , ' ( ) & / + # \" -")
        params["query"] = q
        names = sources.split(",") if isinstance(sources, str) else list(sources or [])
        names = [s.strip().lower() for s in names if s and s.strip()] or list(DEFAULT_SOURCES)
        bad = [s for s in names if s not in SOURCES]
        if bad:
            raise ItemError(f"sources must be from {', '.join(SOURCES)}")
        params["sources"] = [s for s in SOURCES if s in names]
        params["must"] = _terms(must, "must")
        params["exclude"] = _terms(exclude, "exclude")
        params["min_price"] = _money(min_price, "min price")
        params["max_price"] = _money(max_price, "max price")
        if params["min_price"] and params["max_price"] and params["min_price"] >= params["max_price"]:
            raise ItemError("min price must be below max price")
        params["include_sponsored"] = bool(include_sponsored)
    params["target_price"] = _money(target_price, "target price")
    if drop_pct in (None, "") and params["target_price"] is None:
        drop_pct = flights.DEFAULT_DROP_PCT
    params["drop_pct"] = None if drop_pct in (None, "") else _number(drop_pct, "drop percent")
    if params["drop_pct"] is not None and not 1 <= params["drop_pct"] <= 90:
        raise ItemError("drop percent must be between 1 and 90")
    params["group"] = (group or "").strip() or None
    if params["group"] and not flights._GROUP.fullmatch(params["group"]):
        raise ItemError("group must be up to 40 letters, digits, spaces, dots, dashes or underscores")
    if params["mode"] == "product":
        params["product_url"] = product_link(params["source"], params["product_id"])
        params["search_urls"] = {}
        params["url"] = params["product_url"]
    else:
        params["search_urls"] = {s: search_url(s, params) for s in params["sources"]}
        params["url"] = params["search_urls"][params["sources"][0]]
    return params


# ---- links, built from our own params and validated ids ------------------------

def search_url(source: str, params: dict, text: str | None = None) -> str:
    q = text or params["query"]
    enc = lambda d: urllib.parse.urlencode(d, quote_via=urllib.parse.quote)  # noqa: E731
    return {"amazon": "https://www.amazon.com/s?" + enc({"k": q}),
            "walmart": "https://www.walmart.com/search?" + enc({"q": q}),
            "bestbuy": "https://www.bestbuy.com/site/searchpage.jsp?" + enc({"st": q}),
            "newegg": "https://www.newegg.com/p/pl?" + enc({"d": q}),
            "microcenter": "https://www.microcenter.com/search/search_results.aspx?"
                           + enc({"Ntt": q, "storeid": params.get("store") or DEFAULT_STORE})}[source]


def product_link(source: str, pid: str, slug: str = "") -> str:
    """Each retailer's canonical product URL. Best Buy's /site/<sku>.p
    redirects to the current page (checked 2026-11-27). Micro Center 404s
    without the slug, so without a valid one it is a search for the id."""
    if source == "amazon":
        return f"https://www.amazon.com/dp/{pid}"
    if source == "walmart":
        return f"https://www.walmart.com/ip/{pid}"
    if source == "bestbuy":
        return f"https://www.bestbuy.com/site/{pid}.p?skuId={pid}"
    if source == "newegg":
        m = _NEWEGG_DASHED.fullmatch(pid)
        return f"https://www.newegg.com/p/N82E168{''.join(m.groups())}" if m else f"https://www.newegg.com/p/{pid}"
    if slug:
        return f"https://www.microcenter.com/product/{pid}/{slug}"
    return "https://www.microcenter.com/search/search_results.aspx?" + urllib.parse.urlencode({"Ntt": pid})


def what(params: dict) -> str:
    if params["mode"] == "product":
        return f"{SOURCES[params['source']]} product {params['product_id']}"
    return f'"{params["query"]}"'


def summary(params: dict) -> str:
    """One line for the tab and `list`."""
    if params["mode"] == "product":
        out = what(params)
    else:
        out = f"{what(params)} on {', '.join(SOURCES[s] for s in params['sources'])}"
        bits = [f"must have {', '.join(params['must'])}"] if params["must"] else []
        if params["exclude"]:
            bits.append(f"not {', '.join(params['exclude'])}")
        if params["min_price"] is not None or params["max_price"] is not None:
            lo, hi = params["min_price"], params["max_price"]
            bits.append(f"{flights.money(lo, CURRENCY) if lo else 'any'} to {flights.money(hi, CURRENCY) if hi else 'any'}")
        if params["condition"] == "new":
            bits.append("new only")
        if not params["include_sponsored"]:
            bits.append("no sponsored")
        if "microcenter" in params["sources"] and params["store"] != DEFAULT_STORE:
            bits.append(f"Micro Center store {params['store']}")
        out += f" ({'; '.join(bits)})" if bits else ""
    triggers = []
    if params.get("target_price") is not None:
        triggers.append(f"at or below {flights.money(params['target_price'], CURRENCY)}")
    if params.get("drop_pct") is not None:
        triggers.append(f"{params['drop_pct']:g}% below the 14-day average")
    return f"{out}, alert {' or '.join(triggers)}"


# ---- the default item source ---------------------------------------------------

def options_for(source: str, params: dict) -> dict:
    if params["mode"] == "product":
        pid = params["product_id"]
        return {"amazon": {"asin": pid}, "walmart": {"us_item_id": pid}, "bestbuy": {"sku": pid},
                "newegg": {"item_number": pid},
                "microcenter": {"product_id": pid, "store_id": params["store"]}}[source]
    q = params["query"]
    if source == "walmart":
        out = {"query": q, "limit": 40}
        if params.get("min_price") is not None:
            out["min_price"] = params["min_price"]
        if params.get("max_price") is not None:
            out["max_price"] = params["max_price"]
        return out
    if source == "newegg":
        return {"query": q, "condition": "new"} if params["condition"] == "new" else {"query": q}
    if source == "microcenter":
        return {"query": q, "store_id": params["store"], "per_page": 48}
    return {"query": q}


def alexandria_fetch(source: str, params: dict) -> Search:
    provider, capability = (PRODUCT if params["mode"] == "product" else SEARCH)[source]
    try:
        data = stays._call(provider, capability, options_for(source, params))
    except stays.StayError as e:
        raise ItemError(str(e)) from None
    if params["mode"] == "product":
        return parse_product_page(source, data, params)
    return parse_search(source, data)


def _amount(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def _price(obj, *keys: str) -> tuple[float | None, str | None]:
    """A price from a number or an object; the currency it states, else None."""
    if not isinstance(obj, dict):
        return _amount(obj), None
    cur = obj.get("currency")
    for k in keys:
        amount = _amount(obj.get(k))
        if amount is not None:
            return amount, cur if isinstance(cur, str) else None
    return None, None


def _rating(score, count) -> tuple[float | None, int | None]:
    reviews = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None
    rating = float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) \
        and math.isfinite(score) and 0 < score <= 5 else None
    if reviews == 0:
        rating = None
    return rating, reviews


def _condition(value) -> str:
    text = str(value or "").lower().replace("condition", "").replace("-", "_").replace(" ", "_")
    for word, out in (("open_box", "open_box"), ("openbox", "open_box"), ("refurb", "refurbished"),
                      ("renewed", "refurbished"), ("used", "used"), ("damaged", "used")):
        if word in text:
            return out
    return "new" if text.strip("_") == "new" else ""


def _stock(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower().replace("_", "").replace(" ", "")
        if v in ("instock", "limitedstock", "onlineonly"):
            return True
        if v in ("outofstock", "soldout", "discontinued", "notcarried"):
            return False
    return None


def _offer(source: str, item: dict, *, pid, title, price, currency, **extra) -> Offer | None:
    pid = pid.upper() if source == "amazon" and isinstance(pid, str) else pid
    if not isinstance(pid, str) or not IDS[source].fullmatch(pid) or price is None:
        return None
    slug = ""
    if source == "microcenter":
        m = _MC_SLUG.search(str(item.get("url") or ""))
        slug = m.group(1) if m else ""
    return Offer(source=source, id=pid, title=title if isinstance(title, str) else "", price=price,
                 currency=currency if isinstance(currency, str) and currency else CURRENCY, link=product_link(source, pid, slug), **extra)


def parse_search(source: str, data: dict) -> Search:
    """Listings from one search page. A page that is not a search raises; a
    listing that does not validate is skipped."""
    items = data.get("items" if source == "walmart" else "results")
    if not isinstance(items, list):
        raise ItemError(f"{SOURCES[source]} sent an unexpected response")
    unavailable = {s for s in data.get("unavailable_skus") or [] if isinstance(s, str)}
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if source == "amazon":
            price, cur = _price(item.get("price"), "amount")
            rating, reviews = _rating(item.get("rating"), None)
            o = _offer(source, item, pid=item.get("asin"), title=item.get("title"), price=price, currency=cur,
                       sponsored=item.get("sponsored") is True, rating=rating, reviews=reviews)
        elif source == "walmart":
            price, cur = _price(item.get("price"), "current")
            rating, reviews = _rating(item.get("rating"), item.get("review_count"))
            o = _offer(source, item, pid=item.get("us_item_id"), title=item.get("name"), price=price, currency=cur,
                       sponsored=item.get("sponsored") is True, in_stock=_stock(item.get("availability")),
                       rating=rating, reviews=reviews)
        elif source == "bestbuy":
            price, cur = _price(item.get("price"), "current")
            rating, reviews = _rating(item.get("rating"), item.get("review_count"))
            sku = item.get("sku")
            o = _offer(source, item, pid=sku, title=item.get("title"), price=price, currency=cur,
                       in_stock=False if sku in unavailable else None, rating=rating, reviews=reviews)
        elif source == "newegg":
            price, cur = _price(item.get("price"), "current")
            r = item.get("rating") if isinstance(item.get("rating"), dict) else {}
            rating, reviews = _rating(r.get("average"), r.get("count"))
            cond = item.get("condition") if isinstance(item.get("condition"), dict) else {}
            o = _offer(source, item, pid=item.get("item_number"), title=item.get("title"), price=price,
                       currency=cur, sponsored=item.get("is_sponsored") is True,
                       in_stock=_stock(item.get("in_stock")), condition=_condition(cond.get("label")),
                       rating=rating, reviews=reviews)
        else:
            price, _ = _price(item.get("price"))
            cur = item.get("currency")
            o = _offer(source, item, pid=item.get("product_id"), title=item.get("name"), price=price,
                       currency=cur if isinstance(cur, str) else None, in_stock=_stock(item.get("in_stock")))
        if o is not None:
            out.append(o)
    return Search(out, returned=len(items))


def parse_product_page(source: str, data: dict, params: dict) -> Search:
    """The one watched listing. A page about another product raises."""
    pid = params["product_id"]
    if source == "amazon":
        if data.get("redirected") is True or (data.get("served_asin") not in (None, pid)):
            raise ItemError("Amazon showed a different variant than the one watched")
        price, cur = _price(data.get("price"), "amount")
        stock = _stock(data.get("in_stock"))
        if data.get("offer_state") == "unavailable":
            stock = False
        o = _offer(source, data, pid=pid, title=data.get("title"), price=price, currency=cur, in_stock=stock,
                   condition=_condition(data.get("condition")))
    elif source == "walmart":
        price, cur = _price(data.get("price"), "current")
        rating, reviews = _rating(data.get("rating"), data.get("review_count"))
        got = data.get("us_item_id")
        if got not in (None, pid):
            raise ItemError("Walmart showed a different item than the one watched")
        o = _offer(source, data, pid=pid, title=data.get("name"), price=price, currency=cur,
                   in_stock=_stock(data.get("availability")), rating=rating, reviews=reviews)
    elif source == "bestbuy":
        price, cur = _price(data.get("price"), "current")
        o = _offer(source, data, pid=pid, title=f"Best Buy SKU {pid}", price=price, currency=cur,
                   condition=_condition(data.get("condition")))
    elif source == "newegg":
        price, cur = _price(data.get("price"), "current")
        r = data.get("rating") if isinstance(data.get("rating"), dict) else {}
        rating, reviews = _rating(r.get("average"), r.get("count"))
        cond = data.get("condition") if isinstance(data.get("condition"), dict) else {}
        stock = _stock(data.get("in_stock"))
        if data.get("is_active") is False:
            stock = False
        o = _offer(source, data, pid=pid, title=data.get("title"), price=price, currency=cur, in_stock=stock,
                   condition=_condition(cond.get("label")), rating=rating, reviews=reviews)
    else:
        price, _ = _price(data.get("price"))
        rating, reviews = _rating(data.get("rating"), data.get("review_count"))
        cur = data.get("currency")
        o = _offer(source, data, pid=pid, title=data.get("name"), price=price,
                   currency=cur if isinstance(cur, str) else None, in_stock=_stock(data.get("in_stock")),
                   condition=_condition(data.get("condition")), rating=rating, reviews=reviews)
    return Search([o] if o else [], returned=1)


# ---- filters -------------------------------------------------------------------

def _squash(text: str) -> str:
    return "".join(text.lower().split())


def reasons(o: Offer, params: dict) -> list[str]:
    """Why a listing does not qualify; empty when it does."""
    out = []
    if o.currency != params["currency"]:
        out.append("currency")
    if o.in_stock is False:
        out.append("out of stock")
    if params["condition"] == "new" and (o.condition not in ("", "new") or _NOT_NEW.search(o.title)):
        out.append("not new")
    if params["mode"] == "search":
        if o.sponsored and not params["include_sponsored"]:
            out.append("sponsored")
        title = _squash(o.title)
        if any(_squash(t) not in title for t in params["must"]):
            out.append("missing a must term")
        if any(_squash(t) in title for t in params["exclude"]):
            out.append("excluded term")
        if params["min_price"] is not None and o.price < params["min_price"]:
            out.append("below min price")
        if params["max_price"] is not None and o.price > params["max_price"]:
            out.append("above max price")
    return out


def eligible(offers: list[Offer], params: dict) -> list[Offer]:
    return [o for o in offers if not reasons(o, params)]


# ---- the check -----------------------------------------------------------------

def new_state() -> dict:
    return {**stays.new_state(CURRENCY), "alerted_ids": []}


def rating_text(o: Offer) -> str:
    if o.rating is None:
        return "unrated"
    count = "review count not given" if o.reviews is None else f"{o.reviews} review" + ("s" if o.reviews != 1 else "")
    return f"rated {o.rating:g}/5 ({count})"


def offer_line(o: Offer) -> str:
    bits = [flights.money(round(o.price, 2), o.currency)]
    if o.condition:
        bits.append(o.condition.replace("_", " "))
    if o.in_stock:
        bits.append("in stock")
    bits.append(rating_text(o))
    return f'"{o.label}" on {SOURCES[o.source]}: {", ".join(bits)}'


def top_dict(o: Offer) -> dict:
    return {"source": o.source, "source_label": SOURCES[o.source], "id": o.id, "name": o.label,
            "price": round(o.price, 2), "currency": o.currency, "rating_label": rating_text(o), "link": o.link,
            "price_label": flights.money(round(o.price, 2), o.currency)}


@dataclass
class SourceResult:
    source: str
    error: str = ""
    best: Offer | None = None
    ok: list = field(default_factory=list)
    top: list = field(default_factory=list)
    ref: float | None = None
    how: str = ""
    hit: bool = False           # target or drop condition holds for the cheapest
    met: bool = False           # an alert is due, judged against the record as read
    first: bool = False
    line: str = ""


@dataclass
class Result:
    outcome: str  # met | not_met | baseline | could_not_check
    result: str
    sources: list = field(default_factory=list)


def _hit(price: float, params: dict, ref: float | None) -> bool:
    if params.get("target_price") is not None and price <= params["target_price"]:
        return True
    return params.get("drop_pct") is not None and ref is not None and price <= ref * (1 - params["drop_pct"] / 100)


def under_target(ok: list[Offer], params: dict) -> list[Offer]:
    target = params.get("target_price")
    return [] if target is None else [o for o in ok if o.price <= target]


def due(r: SourceResult, state: dict, params: dict) -> tuple[bool, list[Offer]]:
    """Whether this source alerts against `state`: the cheapest holds a
    trigger and is below the last alert, or listings under the target that
    have never alerted. Run on the record as read and again on a fresh read."""
    last = state.get("last_alerted_price")
    lower = r.hit and (last is None or r.best.price < last)
    done = set(state.get("alerted_ids") or [])
    fresh = [o for o in under_target(r.ok, params) if o.id not in done]
    return lower, fresh


def _one(source: str, rec: dict, fetch: ItemFetch, now: datetime) -> SourceResult:
    params = rec["params"]
    name = SOURCES[source]
    try:
        got = fetch(source, params)
    except ItemError as e:
        return SourceResult(source, error=str(e), line=f"{name} could not check: {e}")
    if not isinstance(got, Search) or not got.offers:
        # Never "nothing for sale": an empty page is far more often a scraper problem.
        n = got.returned if isinstance(got, Search) else 0
        why = "it returned no listings" if not n else f"none of the {n} listings it returned could be read"
        if params["mode"] == "product":
            why = "it returned no price for the product"
        return SourceResult(source, error=why, line=f"{name} could not check: {why}")
    ok = eligible(got.offers, params)
    if not ok:
        if params["mode"] == "product":
            why = "the product is " + " and ".join(reasons(got.offers[0], params))
        else:
            why = f"none of the {got.returned} listings it returned passes the filters"
        return SourceResult(source, error=why, line=f"{name} could not check: {why}")
    ok.sort(key=lambda o: o.price)
    best = ok[0]
    state = (rec.get("item_state") or {}).get(source) or new_state()
    ref, how = stays.reference(state, now)
    res = SourceResult(source, best=best, ok=ok, top=[top_dict(o) for o in ok[:TOP_N]], ref=ref, how=how)
    res.hit = _hit(best.price, params, ref)
    price = flights.money(round(best.price, 2), CURRENCY)
    noun = "price" if params["mode"] == "product" else "cheapest"
    if ref is None:
        res.line, res.first = f"{name} {noun} {price}, the first price seen", True
    else:
        pct = (ref - best.price) / ref * 100
        short = "14-day average" if how.startswith("the 14-day") else "first price"
        res.line = (f"{name} {noun} {price}, {abs(pct):.0f}% {'below' if pct >= 0 else 'above'} "
                    f"{short} {flights.money(round(ref), CURRENCY)}")
    lower, fresh = due(res, state, params)
    res.met = lower or bool(fresh)
    return res


def observe(rec: dict, fetch: ItemFetch | None = None, now: datetime | None = None) -> Result:
    """Every source at once: a slow source must not push a tick past the run
    budget. Decided against the record as read; apply repeats the test on a
    fresh read, so an overlapping run cannot double up."""
    now = now or datetime.now(timezone.utc)
    fetch = fetch or alexandria_fetch
    names = rec["params"]["sources"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(names)) as pool:
        results = list(pool.map(lambda s: _one(s, rec, fetch, now), names))
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


def _detail(r: SourceResult, state: dict, params: dict, lower: bool, fresh: list[Offer]) -> str:
    name = SOURCES[r.source]
    best, target = r.best, params.get("target_price")
    if lower:
        why = []
        if target is not None and best.price <= target:
            why.append(f"{flights.money(round(target - best.price, 2), CURRENCY)} under your target "
                       f"{flights.money(target, CURRENCY)}")
        if r.ref is not None and best.price < r.ref:
            why.append(f"{(r.ref - best.price) / r.ref * 100:.0f}% below {r.how}")
        lines = [f"{name}: {flights.money(round(best.price, 2), CURRENCY)}" + (f", {', '.join(why)}" if why else "")]
        if state.get("last_alerted_price") is not None:
            lines.append(f"Previous {name} alert: {flights.money(round(state['last_alerted_price'], 2), CURRENCY)}")
        shown = [best] + [o for o in fresh if o is not best][:MAX_NEW_LINES]
    else:
        n = len(fresh)
        lines = [f"{name}: {n} new listing{'s' if n != 1 else ''} under your target {flights.money(target, CURRENCY)}"]
        shown = fresh[:MAX_NEW_LINES + 1]
    for o in shown:
        lines.append(("New: " if o is not best and o in fresh else "") + offer_line(o))
        lines.append(o.link)
    more = len(fresh) - len([o for o in shown if o in fresh])
    if more > 0:
        lines.append(f"and {more} more under the target")
    if params["mode"] == "search":
        lines.append(f"Search {name}: {params['search_urls'][r.source]}")
    return "\n".join(lines)


def apply(rec: dict, got: Result, now: datetime, fired_text: Callable[[dict, str], str],
          failure_after: int) -> None:
    """Record each source on a fresh read. One message per check covers every
    source with an alert due."""
    params = rec["params"]
    store = rec.setdefault("item_state", {})
    worked = any(not r.error for r in got.sources)
    lines = []
    for r in got.sources:
        state = store.setdefault(r.source, new_state())
        state.setdefault("alerted_ids", [])
        if r.error:
            state["consecutive_failures"] += 1
            state["last_error"] = r.error
            if worked and state["consecutive_failures"] >= failure_after and not state["failure_alerted"]:
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
        lower, fresh = due(r, state, params)
        if lower or fresh:
            lines.append(_detail(r, state, params, lower, fresh))
            if lower:
                state["last_alerted_price"] = r.best.price
            ids = [o.id for o in under_target(r.ok, params)]
            state["alerted_ids"] = (state["alerted_ids"] + [i for i in ids if i not in state["alerted_ids"]]
                                    )[-MAX_ALERTED_IDS:]
        flights.record_price(state, r.best.price, now)
    if lines:
        head = [f"Option: {params['group']}"] if params.get("group") else []
        rec["fire_count"] += 1
        rec["outbox"].append({"kind": "refire", "text": fired_text(rec, "\n".join(head + lines))})


# ---- what the tab shows --------------------------------------------------------

def prices_label(rec: dict, now: datetime) -> list[str]:
    lines = []
    for source in rec["params"]["sources"]:
        state = (rec.get("item_state") or {}).get(source) or new_state()
        ref, how = stays.reference(state, now)
        if ref is None:
            compared = "no price yet"
        elif how.startswith("the 14-day"):
            compared = f"14-day average {flights.money(round(ref), CURRENCY)}"
        else:
            compared = "average after 3 days of checks"
        money = lambda v: flights.money(None if v is None else round(v, 2), CURRENCY)  # noqa: E731
        top = state.get("top") or []
        last = f"last cheapest {top[0]['price_label']}" if top else "last cheapest -"
        lines.append(f"{SOURCES[source]}: {last}, {compared}, first {money(state.get('baseline_price'))}, "
                     f"lowest {money(state.get('lowest_price'))}, last alert {money(state.get('last_alerted_price'))}")
    return lines


def top_rows(rec: dict) -> list[dict]:
    """The cheapest qualifying listings from each source's last good check,
    merged: every source is in USD."""
    per = [((rec.get("item_state") or {}).get(s) or {}).get("top") or [] for s in rec["params"]["sources"]]
    return sorted((o for group in per for o in group), key=lambda o: o["price"])[:TOP_N]


def links(params: dict) -> list[dict]:
    if params["mode"] == "product":
        return [{"label": f"Open on {SOURCES[params['source']]}", "url": params["product_url"]}]
    return [{"label": SOURCES[s], "url": u} for s, u in params["search_urls"].items()]


def degraded(rec: dict) -> list[str]:
    if rec.get("last_outcome") == "could_not_check":
        return []
    return [SOURCES[s] for s, st in (rec.get("item_state") or {}).items()
            if s in SOURCES and st.get("consecutive_failures")]
