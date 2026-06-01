#!/usr/bin/env python3
"""
USDA FoodData Central (FDC) lookup helper for BFL macro enrichment.

Given a food name + assumed portion (grams), returns macros grounded in the
USDA FDC database. Caches hits in fdc_food_cache so the same query from
future meals is free.

Usage (CLI):
    plugins/bfl/scripts/fdc_lookup.py "turkey omelette" --portion 250
    plugins/bfl/scripts/fdc_lookup.py "grilled chicken breast" --portion 120 --data-type FNDDS

Usage (module):
    from fdc_lookup import lookup
    result = lookup("brown rice", portion_g=150)
    # {fdc_id, description, data_type, kcal_per_100g, protein_per_100g, ..., est_kcal, est_protein_g, ...}

Data-type preference order (matches FNDDS>SR_LEGACY>FOUNDATION>BRANDED):
    FNDDS       — What We Eat In America, best for prepared/mixed dishes
    SR Legacy   — basic ingredients with accurate macros
    Foundation  — experimentally analyzed subset
    Branded     — packaged goods, last resort

Nutrient IDs (USDA):
    1008 — Energy (kcal)          (preferred)
    2047 — Energy (Atwater General Factors) — fallback if 1008 missing
    1003 — Protein (g)
    1005 — Carbohydrate, by difference (g)
    1004 — Total lipid/fat (g)
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import urllib.parse
import urllib.request
from typing import Any

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
FDC_SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"

DATA_TYPE_MAP = {
    "FNDDS": "Survey (FNDDS)",
    "SR_LEGACY": "SR Legacy",
    "FOUNDATION": "Foundation",
    "BRANDED": "Branded",
}
DATA_TYPE_PREFERENCE = ["FNDDS", "SR_LEGACY", "FOUNDATION", "BRANDED"]

NUTRIENT_IDS = {
    "kcal_primary": 1008,
    "kcal_fallback": 2047,
    "protein": 1003,
    "carbs": 1005,
    "fat": 1004,
}


def normalize_query(q: str) -> str:
    q = q.lower().strip()
    q = re.sub(r"[^\w\s]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q


def _chassis_db_helpers():
    """Lazy import of the shared backend helper."""
    sys.path.insert(0, str(PLUGIN_DIR / "scripts"))
    from _chassis_db import connect, get_backend  # noqa
    return connect, get_backend


def open_db():
    """Open a connection to the active backend (SQLite or PG) based on
    USE_PG env flag. See plugins/bfl/scripts/_chassis_db.py."""
    connect, _ = _chassis_db_helpers()
    return connect()


def _is_pg(db) -> bool:
    return db.__class__.__module__.startswith("psycopg")


def cache_get(db, query_norm: str, data_type_pref: str) -> dict | None:
    if _is_pg(db):
        from psycopg.rows import dict_row
        with db.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM fdc_food_cache WHERE query_normalized = %s AND data_type_pref = %s",
                (query_norm, data_type_pref),
            )
            row = cur.fetchone()
        return dict(row) if row else None
    row = db.execute(
        "SELECT * FROM fdc_food_cache WHERE query_normalized = ? AND data_type_pref = ?",
        (query_norm, data_type_pref),
    ).fetchone()
    return dict(row) if row else None


def cache_put(
    db,
    query_norm: str,
    data_type_pref: str,
    food: dict,
    nutrients: dict,
    match_score: float,
) -> None:
    if _is_pg(db):
        sql = """
            INSERT INTO fdc_food_cache
              (query_normalized, data_type_pref, fdc_id, description, data_type,
               kcal_per_100g, protein_per_100g, carbs_per_100g, fat_per_100g,
               match_score, raw_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (query_normalized, data_type_pref) DO UPDATE SET
              fdc_id = EXCLUDED.fdc_id,
              description = EXCLUDED.description,
              data_type = EXCLUDED.data_type,
              kcal_per_100g = EXCLUDED.kcal_per_100g,
              protein_per_100g = EXCLUDED.protein_per_100g,
              carbs_per_100g = EXCLUDED.carbs_per_100g,
              fat_per_100g = EXCLUDED.fat_per_100g,
              match_score = EXCLUDED.match_score,
              raw_json = EXCLUDED.raw_json
        """
    else:
        sql = """
            INSERT OR REPLACE INTO fdc_food_cache
              (query_normalized, data_type_pref, fdc_id, description, data_type,
               kcal_per_100g, protein_per_100g, carbs_per_100g, fat_per_100g,
               match_score, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
    args = (
        query_norm,
        data_type_pref,
        food.get("fdcId"),
        food.get("description"),
        food.get("dataType"),
        nutrients.get("kcal"),
        nutrients.get("protein"),
        nutrients.get("carbs"),
        nutrients.get("fat"),
        match_score,
        json.dumps(food, separators=(",", ":")),
    )
    if _is_pg(db):
        with db.cursor() as cur:
            cur.execute(sql, args)
    else:
        db.execute(sql, args)
    db.commit()


def extract_nutrients(food: dict) -> dict:
    """Pull kcal/protein/carbs/fat per 100g from an FDC food object."""
    out: dict[str, float | None] = {"kcal": None, "protein": None, "carbs": None, "fat": None}
    by_id: dict[int, float] = {}
    for n in food.get("foodNutrients", []):
        nid = n.get("nutrientId") or n.get("nutrient", {}).get("id")
        if nid is None:
            continue
        val = n.get("value")
        if val is None:
            val = n.get("amount")
        if val is None:
            continue
        by_id[int(nid)] = float(val)

    out["kcal"] = by_id.get(NUTRIENT_IDS["kcal_primary"]) or by_id.get(NUTRIENT_IDS["kcal_fallback"])
    out["protein"] = by_id.get(NUTRIENT_IDS["protein"])
    out["carbs"] = by_id.get(NUTRIENT_IDS["carbs"])
    out["fat"] = by_id.get(NUTRIENT_IDS["fat"])
    return out


def fdc_search(
    query: str,
    api_key: str,
    data_type_key: str | None,
    page_size: int = 10,
    require_all_words: bool = True,
) -> list[dict]:
    params = {
        "query": query,
        "pageSize": page_size,
        "api_key": api_key,
    }
    if require_all_words:
        params["requireAllWords"] = "true"
    if data_type_key:
        params["dataType"] = DATA_TYPE_MAP[data_type_key]
    url = f"{FDC_SEARCH_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "behalfbot-bfl-fdc/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RuntimeError("FDC rate-limited (HTTP 429) — upgrade from DEMO_KEY") from e
        raise
    return data.get("foods", []) or []


_DATA_TYPE_RANK = {
    "Survey (FNDDS)": 0,
    "SR Legacy": 1,
    "Foundation": 2,
    "Branded": 3,
}


def pick_best(foods: list[dict]) -> dict | None:
    """Pick the best-matching food with usable macro data (kcal + protein non-null).

    Ranking: sub-DB preference first (FNDDS > SR Legacy > Foundation > Branded),
    then FDC relevance score. Branded results are noisy duplicates; we fall back
    to them only when no canonical match exists.
    """
    scored = []
    for f in foods:
        nuts = extract_nutrients(f)
        if nuts["kcal"] is None or nuts["protein"] is None:
            continue
        rank = _DATA_TYPE_RANK.get(f.get("dataType", ""), 9)
        score = float(f.get("score", 0.0))
        scored.append((rank, -score, f, nuts))
    if not scored:
        return None
    scored.sort(key=lambda x: (x[0], x[1]))
    _, _, food, nuts = scored[0]
    return {"food": food, "nutrients": nuts}


def lookup(
    query: str,
    portion_g: float | None = None,
    data_type_pref: str = "FNDDS",
    api_key: str | None = None,
    db: Any = None,
    use_cache: bool = True,
) -> dict | None:
    """Look up a food in FDC and compute macros for the given portion."""
    q_norm = normalize_query(query)
    if not q_norm:
        return None

    owns_db = False
    if db is None:
        db = open_db()
        owns_db = True

    try:
        if use_cache:
            hit = cache_get(db, q_norm, data_type_pref)
            if hit and hit.get("fdc_id"):
                return _shape_result(hit, portion_g, source="cache")

        if api_key is None:
            api_key = os.environ.get("USDA_FDC_API_KEY") or "DEMO_KEY"

        try:
            foods = fdc_search(q_norm, api_key, data_type_key=None, require_all_words=True)
        except Exception as e:
            if "rate-limited" in str(e):
                raise
            sys.stderr.write(f"FDC lookup soft-failed for {query!r}: {e}\n")
            return None

        if not foods:
            try:
                foods = fdc_search(q_norm, api_key, data_type_key=None, require_all_words=False)
            except Exception:
                foods = []

        best = pick_best(foods)
        if not best:
            return None

        cache_put(db, q_norm, data_type_pref, best["food"], best["nutrients"],
                  float(best["food"].get("score", 0.0)))
        hit = cache_get(db, q_norm, data_type_pref)
        if hit:
            return _shape_result(hit, portion_g, source="api")
        return None
    finally:
        if owns_db:
            db.close()


def _shape_result(cache_row: dict, portion_g: float | None, source: str) -> dict:
    kcal_100 = cache_row.get("kcal_per_100g")
    prot_100 = cache_row.get("protein_per_100g")
    carb_100 = cache_row.get("carbs_per_100g")
    fat_100 = cache_row.get("fat_per_100g")

    def scale(per_100: float | None) -> float | None:
        if per_100 is None or portion_g is None:
            return None
        return round(per_100 * float(portion_g) / 100.0, 2)

    return {
        "fdc_id": cache_row.get("fdc_id"),
        "description": cache_row.get("description"),
        "data_type": cache_row.get("data_type"),
        "match_score": cache_row.get("match_score"),
        "kcal_per_100g": kcal_100,
        "protein_per_100g": prot_100,
        "carbs_per_100g": carb_100,
        "fat_per_100g": fat_100,
        "portion_g": portion_g,
        "est_kcal": scale(kcal_100),
        "est_protein_g": scale(prot_100),
        "est_carbs_g": scale(carb_100),
        "est_fat_g": scale(fat_100),
        "source": source,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", help="food name, e.g. 'turkey omelette'")
    ap.add_argument("--portion", type=float, default=None, help="portion in grams (omit for per-100g macros only)")
    ap.add_argument("--data-type", default="FNDDS", choices=list(DATA_TYPE_MAP.keys()), help="preferred data type")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    result = lookup(args.query, portion_g=args.portion, data_type_pref=args.data_type, use_cache=not args.no_cache)
    if result is None:
        print(json.dumps({"query": args.query, "match": None}, indent=2))
        return 1
    out = {"query": args.query, "match": result}
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
