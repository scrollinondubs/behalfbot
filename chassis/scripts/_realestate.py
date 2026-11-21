"""The `realestate` TripWire: zones from a GeoJSON file, listings from a JSON
file, both written by the install. The chassis knows no zone property by name.
Which properties gate a zone and which ones score it come from the criteria
held in the tripwire record, so one install can screen land on sea view and
another on school catchments with the same code.

Standard library only, like _tripwires.py.

Listings are untrusted: an install builds them from email or a portal. Titles
and `raw` never reach an alert or the store. An alert carries our wording, the
numbers, the zone id and the ad URL (http or https only).

Files are named relative to the customer home, because the container and a host
dashboard mount it at different absolute paths.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

OPS = {
    "<=": lambda a, b: a <= b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    ">": lambda a, b: a > b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_RULES, MAX_WEIGHTS, MAX_REGIONS = 40, 40, 200
MAX_ALERT_LINES = 8
MAX_ALERTED_IDS = 5000
_KEY = re.compile(r"[a-z0-9_]{1,40}")
_PROP = re.compile(r"[A-Za-z0-9_.-]{1,80}")


class RealEstateError(Exception):
    """Bad criteria or a file that cannot be used. Message is fit to show."""


# ---- paths ---------------------------------------------------------------------

def check_relpath(value: str | None, label: str) -> str:
    value = (value or "").strip()
    if not value:
        raise RealEstateError(f"{label} is required")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise RealEstateError(f"{label} must be a path inside the customer home, such as state/x.json")
    return value


def resolve(home: Path, rel: str) -> Path:
    base = Path(home).resolve()
    path = (base / check_relpath(rel, "path")).resolve()
    if base != path and base not in path.parents:
        raise RealEstateError("path leaves the customer home")
    return path


def _read_json(path: Path, what: str):
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise RealEstateError(f"the {what} file is over {MAX_FILE_BYTES // (1024 * 1024)} MB")
        return json.loads(path.read_text(encoding="utf8"))
    except FileNotFoundError:
        raise RealEstateError(f"the {what} file is missing") from None
    except (OSError, UnicodeDecodeError):
        raise RealEstateError(f"the {what} file cannot be read") from None
    except ValueError:
        raise RealEstateError(f"the {what} file is not valid JSON") from None


# ---- criteria ------------------------------------------------------------------

def _num(value, label: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise RealEstateError(f"{label} must be a number") from None
    if not math.isfinite(out):
        raise RealEstateError(f"{label} must be a number")
    return out


def _text(value, label: str, limit: int = 120) -> str:
    out = str(value or "").strip()
    if not out or len(out) > limit:
        raise RealEstateError(f"{label} must be 1 to {limit} characters")
    return out


def validate_criteria(raw: dict) -> dict:
    """Normalise criteria. Shape:

    {"id_property": "zone_id",
     "rules":   [{"key", "label", "property", "op", "value", "enabled", "missing": "fail"|"pass",
                  "group": "exclusion"|"limit"}],
     "weights": [{"key", "label", "property", "weight"}],
     "regions": {"property", "label_property", "include": [...]}}   optional

    A rule says what a passing zone must satisfy. "missing" decides a zone that
    lacks the property. An empty region include list means every region.
    """
    if not isinstance(raw, dict):
        raise RealEstateError("criteria must be a JSON object")
    out: dict = {"id_property": _prop(raw.get("id_property") or "id", "id_property")}
    rules, keys = [], set()
    for r in raw.get("rules") or []:
        if not isinstance(r, dict):
            raise RealEstateError("each rule must be an object")
        key = _key(r.get("key"), keys)
        op = r.get("op")
        if op not in OPS:
            raise RealEstateError(f"rule {key}: op must be one of {' '.join(OPS)}")
        value = r.get("value")
        if isinstance(value, (bool, str)) or value is None:
            if op not in ("==", "!="):
                raise RealEstateError(f"rule {key}: a text or true/false value only works with == or !=")
        else:
            value = _num(value, f"rule {key} value")
        rules.append({
            "key": key, "label": _text(r.get("label") or key, f"rule {key} label"),
            "property": _prop(r.get("property"), f"rule {key} property"), "op": op, "value": value,
            "enabled": bool(r.get("enabled", True)),
            "missing": "pass" if r.get("missing") == "pass" else "fail",
            "group": "limit" if r.get("group") == "limit" else "exclusion",
        })
    if len(rules) > MAX_RULES:
        raise RealEstateError(f"at most {MAX_RULES} rules")
    weights, wkeys = [], set()
    for w in raw.get("weights") or []:
        if not isinstance(w, dict):
            raise RealEstateError("each weight must be an object")
        key = _key(w.get("key"), wkeys)
        weight = _num(w.get("weight", 1), f"weight {key}")
        if not 0 <= weight <= 100:
            raise RealEstateError(f"weight {key} must be between 0 and 100")
        weights.append({"key": key, "label": _text(w.get("label") or key, f"weight {key} label"),
                        "property": _prop(w.get("property"), f"weight {key} property"), "weight": weight})
    if len(weights) > MAX_WEIGHTS:
        raise RealEstateError(f"at most {MAX_WEIGHTS} weights")
    out["rules"], out["weights"] = rules, weights
    regions = raw.get("regions")
    if regions:
        include = regions.get("include") or []
        if not isinstance(include, list) or len(include) > MAX_REGIONS:
            raise RealEstateError("regions.include must be a list")
        out["regions"] = {
            "property": _prop(regions.get("property"), "regions.property"),
            "label_property": _prop(regions.get("label_property") or regions.get("property"),
                                    "regions.label_property"),
            "include": [_text(v, "region", 80) for v in include],
        }
    return out


def _key(value, seen: set) -> str:
    key = str(value or "")
    if not _KEY.fullmatch(key):
        raise RealEstateError(f"key {key!r} must be lowercase letters, digits or _")
    if key in seen:
        raise RealEstateError(f"key {key} is used twice")
    seen.add(key)
    return key


def _prop(value, label: str) -> str:
    out = str(value or "")
    if not _PROP.fullmatch(out):
        raise RealEstateError(f"{label} must be a property name")
    return out


def apply_form(criteria: dict, form: dict[str, str]) -> dict:
    """Criteria edited from the dashboard form. Only enabled flags, values,
    weights and the region set change; properties and ops stay as seeded."""
    out = json.loads(json.dumps(criteria))
    for r in out["rules"]:
        r["enabled"] = form.get(f"rule_{r['key']}_enabled") == "1"
        given = form.get(f"rule_{r['key']}_value")
        if given is not None and isinstance(r["value"], (int, float)) and not isinstance(r["value"], bool):
            r["value"] = _num(given, r["label"])
    for w in out["weights"]:
        given = form.get(f"weight_{w['key']}")
        if given is not None:
            w["weight"] = _num(given, w["label"])
    if "regions" in out:
        out["regions"]["include"] = [v[len("region:"):] for k, v in form.items()
                                     if k.startswith("region_") and v.startswith("region:")]
    return validate_criteria(out)


# ---- zones ---------------------------------------------------------------------

@dataclass
class Zone:
    id: str
    props: dict
    polygons: list  # [[outer ring, hole, ...], ...], rings of (lon, lat)
    bbox: tuple
    feature: dict = field(repr=False, default_factory=dict)


def load_zones(home: Path, rel: str, id_property: str) -> list[Zone]:
    data = _read_json(resolve(home, rel), "zones")
    if not isinstance(data, dict) or not isinstance(data.get("features"), list):
        raise RealEstateError("the zones file is not a GeoJSON FeatureCollection")
    zones, seen = [], set()
    for i, f in enumerate(data["features"]):
        geom = (f or {}).get("geometry") or {}
        props = (f or {}).get("properties") or {}
        if geom.get("type") == "Polygon":
            polys = [geom.get("coordinates")]
        elif geom.get("type") == "MultiPolygon":
            polys = geom.get("coordinates")
        else:
            continue
        try:
            polys = [[[(float(p[0]), float(p[1])) for p in ring] for ring in poly] for poly in polys]
        except (TypeError, ValueError, IndexError):
            continue
        xs = [p[0] for poly in polys for p in poly[0]]
        ys = [p[1] for poly in polys for p in poly[0]]
        if not xs:
            continue
        zid = str(props.get(id_property) or f"zone-{i + 1}")
        if zid in seen:
            zid = f"{zid}-{i + 1}"
        seen.add(zid)
        zones.append(Zone(zid, props, polys, (min(xs), min(ys), max(xs), max(ys)), f))
    if not zones:
        raise RealEstateError("the zones file has no polygons")
    return zones


def _in_ring(x: float, y: float, ring: list) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def contains(zone: Zone, lon: float, lat: float) -> bool:
    x0, y0, x1, y1 = zone.bbox
    if not (x0 <= lon <= x1 and y0 <= lat <= y1):
        return False
    return any(_in_ring(lon, lat, poly[0]) and not any(_in_ring(lon, lat, h) for h in poly[1:])
               for poly in zone.polygons)


def _rule_passes(rule: dict, props: dict) -> bool:
    value = props.get(rule["property"])
    if value is None or value == "":
        return rule["missing"] == "pass"
    target = rule["value"]
    if not isinstance(target, (bool, str)) and target is not None:
        try:
            value = float(value)
        except (TypeError, ValueError):
            return rule["missing"] == "pass"
    try:
        return bool(OPS[rule["op"]](value, target))
    except TypeError:
        return False


def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def score_zones(zones: list[Zone], criteria: dict) -> list[dict]:
    """Every zone with whether it passes, why not, and its weighted score.
    Score is the weighted mean of the weight properties, each clamped to 0..1;
    a missing property counts 0."""
    total = sum(w["weight"] for w in criteria["weights"]) or 0
    regions = criteria.get("regions")
    out = []
    for z in zones:
        reasons = []
        for r in criteria["rules"]:
            if r["enabled"] and not _rule_passes(r, z.props):
                got = z.props.get(r["property"])
                reasons.append(f"{r['label']}: {'missing' if got in (None, '') else _fmt(got)}")
        if regions and regions["include"] and str(z.props.get(regions["property"])) not in regions["include"]:
            reasons.append(f"region {z.props.get(regions['label_property']) or z.props.get(regions['property'])} not included")
        breakdown, acc = [], 0.0
        for w in criteria["weights"]:
            try:
                v = min(1.0, max(0.0, float(z.props.get(w["property"]))))
            except (TypeError, ValueError):
                v = 0.0
            acc += v * w["weight"]
            breakdown.append({"key": w["key"], "label": w["label"], "value": round(v, 3), "weight": w["weight"]})
        out.append({"id": z.id, "passes": not reasons, "reasons": reasons,
                     "score": round(acc / total, 3) if total else 0.0, "breakdown": breakdown})
    return out


def region_options(zones: list[Zone], criteria: dict) -> list[dict]:
    regions = criteria.get("regions")
    if not regions:
        return []
    seen: dict[str, str] = {}
    for z in zones:
        value = z.props.get(regions["property"])
        if value is not None:
            seen.setdefault(str(value), str(z.props.get(regions["label_property"]) or value))
    include = set(regions["include"])
    return [{"value": v, "label": label, "checked": not include or v in include}
            for v, label in sorted(seen.items())]


# ---- listings ------------------------------------------------------------------

_CTRL = re.compile(r"[\x00-\x1f\x7f]+")


def _clean_text(value, limit: int) -> str:
    return _CTRL.sub(" ", str(value or "")).strip()[:limit]


def clean_url(value) -> str | None:
    url = str(value or "").strip()
    if len(url) > 500 or not re.fullmatch(r"https?://[^\s<>\"'`]+", url):
        return None
    return url


def _opt_num(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) and out >= 0 else None


def load_listings(home: Path, rel: str) -> tuple[list[dict], int]:
    """Listings fit to show, and how many records were dropped. Required: id,
    a usable URL and coordinates. Everything is copied field by field, so
    `raw` and anything unknown never leave the file."""
    data = _read_json(resolve(home, rel), "listings")
    items = data.get("listings") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise RealEstateError("the listings file must hold a list under \"listings\"")
    out, dropped, seen = [], 0, set()
    for item in items:
        if not isinstance(item, dict):
            dropped += 1
            continue
        lid = _clean_text(item.get("id"), 120)
        url = clean_url(item.get("url"))
        lat, lon = item.get("lat"), item.get("lon")
        try:
            lat, lon = float(lat), float(lon)
            ok = -90 <= lat <= 90 and -180 <= lon <= 180
        except (TypeError, ValueError):
            ok = False
        if not lid or not url or not ok or lid in seen:
            dropped += 1
            continue
        seen.add(lid)
        out.append({
            "id": lid, "source": _clean_text(item.get("source"), 40) or "unknown", "url": url,
            "title": _clean_text(item.get("title"), 160), "price_eur": _opt_num(item.get("price_eur")),
            "area_m2": _opt_num(item.get("area_m2")), "lat": lat, "lon": lon,
            "first_seen": _clean_text(item.get("first_seen"), 40),
            "approximate": item.get("approximate") is True,
            "zone_hint": _clean_text(item.get("zone_hint"), 80) or None,
        })
    return out, dropped


def match(listings: list[dict], zones: list[Zone], scored: list[dict]) -> list[dict]:
    """Each listing gains `match`: inside (its point is in a passing zone),
    approximate (an approximate pin whose hinted zone passes; the ad may lie
    anywhere in a search area larger than the zone) or outside, plus `zone`."""
    passing = {s["id"] for s in scored if s["passes"]}
    by_id = {z.id: z for z in zones}
    out = []
    for item in listings:
        row = dict(item, match="outside", zone=None)
        if item["approximate"]:
            if item["zone_hint"] in by_id:
                row["zone"] = item["zone_hint"]
                if item["zone_hint"] in passing:
                    row["match"] = "approximate"
        else:
            hits = [z.id for z in zones if contains(z, item["lon"], item["lat"])]
            good = [h for h in hits if h in passing]
            row["zone"] = (good or hits or [None])[0]
            if good:
                row["match"] = "inside"
        out.append(row)
    return out


# ---- the check -----------------------------------------------------------------

@dataclass
class Result:
    outcome: str  # met | not_met | could_not_check
    result: str
    new_ids: list = field(default_factory=list)
    detail: str = ""


def _money(value) -> str:
    return f"EUR {value:,.0f}" if value is not None else "price not given"


def _area(value) -> str:
    return f"{value:,.0f} m2" if value is not None else "size not given"


def alert_lines(rows: list[dict]) -> str:
    lines = []
    for r in rows[:MAX_ALERT_LINES]:
        where = (f"in zone {r['zone']}" if r["match"] == "inside"
                 else f"approximate pin, search area around zone {r['zone']}")
        lines.append(f"- {_money(r['price_eur'])}, {_area(r['area_m2'])}, {where} ({r['source']}): {r['url']}")
    if len(rows) > MAX_ALERT_LINES:
        lines.append(f"- and {len(rows) - MAX_ALERT_LINES} more on the map")
    return "\n".join(lines)


def view_data(rec: dict, home: Path) -> dict:
    params = rec["params"]
    criteria = params["criteria"]
    zones = load_zones(home, params["zones_path"], criteria["id_property"])
    scored = score_zones(zones, criteria)
    listings, dropped = load_listings(home, params["listings_path"])
    return {"zones": zones, "scored": scored, "listings": match(listings, zones, scored), "dropped": dropped}


def observe(rec: dict, home: Path) -> Result:
    try:
        data = view_data(rec, home)
    except RealEstateError as e:
        return Result("could_not_check", f"could not check: {e}")
    alerted = set(rec.get("alerted_ids") or [])
    hits = [r for r in data["listings"] if r["match"] != "outside"]
    fresh = [r for r in hits if r["id"] not in alerted]
    passing = sum(1 for s in data["scored"] if s["passes"])
    summary = f"{passing} zones pass, {len(data['listings'])} listings, {len(hits)} in passing zones"
    if not fresh:
        return Result("not_met", summary)
    return Result("met", f"{summary}, {len(fresh)} new", [r["id"] for r in fresh], alert_lines(fresh))


def geojson_payload(rec: dict, home: Path) -> dict:
    """What the map page draws. Zone properties go out as stored (install data);
    listings only as cleaned by load_listings."""
    data = view_data(rec, home)
    scores = {s["id"]: s for s in data["scored"]}
    features = []
    for z in data["zones"]:
        s = scores[z.id]
        features.append({"type": "Feature", "geometry": z.feature.get("geometry"),
                         "properties": {"_id": z.id, "_passes": s["passes"], "_score": s["score"],
                                        "_reasons": s["reasons"], "_breakdown": s["breakdown"], **z.props}})
    return {"zones": {"type": "FeatureCollection", "features": features},
            "listings": data["listings"], "dropped": data["dropped"],
            "passing": sum(1 for s in data["scored"] if s["passes"])}
