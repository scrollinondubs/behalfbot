#!/usr/bin/env python3
"""
Backfill FDC macros on bfl_meals rows that have a description but no photo.

The user wants editable macros for every meal, not just the photo-matched
ones. Vision can't produce numbers without a photo, so we parse the
handwritten meal description into food tokens and feed those to FDC directly.

Ingest loop:
    for meal in bfl_meals where fdc_enriched_at IS NULL and description IS NOT NULL:
        items = tokenize(description)
        portion_g = PORTION_DEFAULT_G / len(items)    # rough equal-split at 100g/item baseline
        for item in items: fdc_lookup(item, portion_g)
        write bfl_meal_items + bfl_meals.fdc_* columns

Tokenization rules (pragmatic, BFL-specific):
  - strip suffixes: " from X", " @ X", " at X"         ("carnitas bowl @ Place" -> "carnitas bowl")
  - strip parentheticals:  "(15g)", "(optional)"       ("protein bar (15g)" -> "protein bar")
  - strip size modifiers at start: big, small, large, huge, plate of, bowl of
  - split on: ',', '+', ' and ', ' & ', '/'
  - lowercase + strip each token
  - drop empty, drop pure numbers, drop stopwords

Portion baseline: PORTION_TOTAL_G (default 350g, roughly one plated meal) split
equally across items. The dashboard edit form pre-fills with these numbers so
the installer can correct them in place.

Usage:
    plugins/bfl/scripts/bfl-fdc-enrich-descriptions.py              # all unenriched meals
    plugins/bfl/scripts/bfl-fdc-enrich-descriptions.py --date 2026-04-20
    plugins/bfl/scripts/bfl-fdc-enrich-descriptions.py --since-days 7
    plugins/bfl/scripts/bfl-fdc-enrich-descriptions.py --force      # redo even if already enriched
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
import time
from datetime import date as _date, timedelta

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR / "scripts"))
import _chassis_db  # type: ignore
import fdc_lookup  # type: ignore


def default_portion_g(n_items: int) -> float:
    """Rough per-item gram default when no photo is available.
    1 item  -> 150g (single-food meal, e.g. protein shake)
    2-3     -> 100g (main + side)
    4+      -> 80g  (plate breakdown, e.g. rice+chicken+veg+potato+peas)
    The installer corrects these via the dashboard edit form."""
    if n_items <= 1:
        return 150.0
    if n_items <= 3:
        return 100.0
    return 80.0

STOPWORDS = {
    "big", "small", "large", "huge", "medium", "little",
    "plate", "bowl", "plateful", "bowlful",
    "of", "with", "and", "the", "a", "an",
}
SIZE_PREFIX_RE = re.compile(
    r"^(?:big|small|large|huge|medium|little|plate of|bowl of|plateful of|bowlful of|a |an |the )\s+",
    re.IGNORECASE,
)
PAREN_RE = re.compile(r"\([^)]*\)")
SUFFIX_RE = re.compile(r"\s+(?:from|@|at)\s+.+$", re.IGNORECASE)
SPLITTER_RE = re.compile(r"\s*[,+/]\s*|\s+and\s+|\s+&\s+", re.IGNORECASE)


def tokenize_description(desc: str) -> list[str]:
    if not desc:
        return []
    desc = SUFFIX_RE.sub("", desc)
    desc = PAREN_RE.sub("", desc)
    parts = [p.strip() for p in SPLITTER_RE.split(desc) if p.strip()]
    cleaned: list[str] = []
    for p in parts:
        p = p.lower()
        for _ in range(3):
            new = SIZE_PREFIX_RE.sub("", p)
            if new == p:
                break
            p = new
        if not p or p.strip().isdigit():
            continue
        words = [w for w in p.split() if w not in STOPWORDS]
        if not words:
            continue
        cleaned.append(" ".join(words))
    return cleaned


def open_db():
    """Backend via USE_PG (see plugins/bfl/scripts/_chassis_db.py)."""
    return _chassis_db.connect()


def eligible_meals(cur, *, since_days: int | None, date: str | None, force: bool) -> list:
    # Always skip photo-matched meals — they get more accurate FDC data via
    # bfl-fdc-enrich.py (vision-extracted items). Description-based
    # enrichment is the fallback for when no photo exists.
    where = [
        "m.description IS NOT NULL",
        "length(trim(m.description)) > 0",
        "m.food_photo_path IS NULL",
    ]
    params: list = []
    if not force:
        where.append("m.fdc_enriched_at IS NULL")
    if date:
        where.append("d.date = ?")
        params.append(date)
    elif since_days is not None:
        cutoff = (_date.today() - timedelta(days=int(since_days))).isoformat()
        where.append("d.date >= ?")
        params.append(cutoff)
    q = f"""
        SELECT m.id as meal_id, m.day_id, m.meal_num, m.description, m.food_photo_path, d.date
          FROM bfl_meals m
          JOIN bfl_days d ON m.day_id = d.id
         WHERE {" AND ".join(where)}
         ORDER BY d.date DESC, m.meal_num
    """
    return cur.execute(q, params).fetchall()


def enrich_meal(db, cur, meal) -> dict:
    desc = meal["description"] if isinstance(meal, dict) else meal[3]
    meal_id_val = meal["meal_id"] if isinstance(meal, dict) else meal[0]
    items = tokenize_description(desc)
    if not items:
        return {"meal_id": meal_id_val, "status": "no_tokens", "items": []}

    per_item_g = default_portion_g(len(items))
    per_item_results = []
    matched = 0
    totals = {"kcal": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0}

    for i, name in enumerate(items):
        try:
            m = fdc_lookup.lookup(name, portion_g=per_item_g, data_type_pref="FNDDS", db=db)
        except Exception as e:
            if "rate-limited" in str(e):
                raise
            m = None
        per_item_results.append({"order": i, "name": name, "portion_g": per_item_g, "match": m})
        if m:
            matched += 1
            totals["kcal"] += m.get("est_kcal") or 0.0
            totals["protein"] += m.get("est_protein_g") or 0.0
            totals["carbs"] += m.get("est_carbs_g") or 0.0
            totals["fat"] += m.get("est_fat_g") or 0.0
        time.sleep(0.05)

    coverage = matched / len(items) if items else 0.0
    meal_id = meal_id_val

    cur.execute("DELETE FROM bfl_meal_items WHERE meal_id = ?", (meal_id,))
    for r in per_item_results:
        m = r["match"] or {}
        cur.execute(
            """
            INSERT INTO bfl_meal_items
              (meal_id, item_order, item_name, portion_g,
               fdc_id, fdc_description, fdc_data_type,
               fdc_kcal_per_100g, fdc_protein_per_100g, fdc_carbs_per_100g, fdc_fat_per_100g,
               est_kcal, est_protein_g, est_carbs_g, est_fat_g, match_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                meal_id, r["order"], r["name"], r["portion_g"],
                m.get("fdc_id"), m.get("description"), m.get("data_type"),
                m.get("kcal_per_100g"), m.get("protein_per_100g"),
                m.get("carbs_per_100g"), m.get("fat_per_100g"),
                m.get("est_kcal"), m.get("est_protein_g"),
                m.get("est_carbs_g"), m.get("est_fat_g"),
                m.get("match_score"),
            ),
        )
    now_unix = int(time.time())
    cur.execute(
        """
        UPDATE bfl_meals
           SET fdc_kcal = ?, fdc_protein_g = ?, fdc_carbs_g = ?, fdc_fat_g = ?,
               fdc_enriched_at = ?, fdc_match_coverage = ?
         WHERE id = ?
        """,
        (
            round(totals["kcal"], 2), round(totals["protein"], 2),
            round(totals["carbs"], 2), round(totals["fat"], 2),
            now_unix, round(coverage, 3), meal_id,
        ),
    )
    db.commit()
    return {
        "meal_id": meal_id,
        "status": "ok",
        "items": [r["name"] for r in per_item_results],
        "coverage": coverage,
        "totals": {k: round(v, 1) for k, v in totals.items()},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD, enrich only this date")
    ap.add_argument("--since-days", type=int, default=None, help="only meals from last N days")
    ap.add_argument("--force", action="store_true", help="re-enrich meals that already have fdc data")
    ap.add_argument("--limit", type=int, default=None, help="stop after N meals (rate-limit safety)")
    args = ap.parse_args()

    db = open_db()
    cur = _chassis_db.cursor(db, dict_rows=True)
    try:
        meals = eligible_meals(cur, since_days=args.since_days, date=args.date, force=args.force)
        if args.limit:
            meals = meals[: args.limit]
        if not meals:
            print("No eligible meals to enrich.")
            return 0
        print(f"Processing {len(meals)} meal(s).")
        errors = 0
        for meal in meals:
            if isinstance(meal, dict):
                m_date, m_num, m_desc = meal["date"], meal["meal_num"], meal["description"]
            else:
                m_date, m_num, m_desc = meal[5], meal[2], meal[3]
            try:
                r = enrich_meal(db, cur, meal)
            except Exception as e:
                errors += 1
                print(f"  {m_date} m{m_num}: ERROR {e}", file=sys.stderr)
                if "rate-limited" in str(e):
                    break
                continue
            if r["status"] == "no_tokens":
                print(f"  {m_date} m{m_num}: no tokens extracted from {m_desc!r}")
                continue
            t = r["totals"]
            print(f"  {m_date} m{m_num}: {t['kcal']} kcal / "
                  f"{t['protein']}g P / {t['carbs']}g C / {t['fat']}g F "
                  f"(cov={r['coverage']:.0%}, items={len(r['items'])})")
        print(f"\nDone. errors={errors}")
        return 0 if errors == 0 else 2
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
