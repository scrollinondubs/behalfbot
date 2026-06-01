#!/usr/bin/env python3
"""
Enrich existing food_photo extractions with USDA FDC macro data.

For each extraction JSON under `${BFL_ARCHIVE_DIR}/extractions/`:
  - If classification == food_photo, look up each item in FDC
  - Equal-split the reported total portion_grams across items (v1 heuristic)
  - Compute per-item macros + aggregate meal totals
  - Write sidecar file *_fdc.json with the enrichment
  - If the extraction's source image is linked to a bfl_meals row via
    food_photo_path, also:
      * insert a bfl_meal_items row per item
      * update bfl_meals.fdc_kcal / fdc_protein_g / fdc_carbs_g / fdc_fat_g /
        fdc_enriched_at / fdc_match_coverage

This script is additive. Running it again on the same extractions is safe —
DB writes use INSERT OR REPLACE / UPDATE on (meal_id, item_order).

Usage:
    plugins/bfl/scripts/bfl-fdc-enrich.py                 # process all food_photo extractions missing a _fdc.json sidecar
    plugins/bfl/scripts/bfl-fdc-enrich.py --force         # re-process even if sidecar exists
    plugins/bfl/scripts/bfl-fdc-enrich.py --only IMG_8518  # process specific basenames (repeatable)
    plugins/bfl/scripts/bfl-fdc-enrich.py --since-days 3  # process extractions modified within last N days

Env vars:
    BFL_ARCHIVE_DIR     absolute path; default ~/behalfbot-archive/bfl
    USDA_FDC_API_KEY    DEMO_KEY works at low volume; register for higher rate limit
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
from datetime import datetime, timezone

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR / "scripts"))
import _chassis_db  # type: ignore
import fdc_lookup  # type: ignore


def archive_root() -> pathlib.Path:
    explicit = os.environ.get("BFL_ARCHIVE_DIR")
    if explicit:
        return pathlib.Path(explicit).expanduser()
    return pathlib.Path.home() / "behalfbot-archive" / "bfl"


def open_db():
    """Backend via USE_PG (see plugins/bfl/scripts/_chassis_db.py)."""
    return _chassis_db.connect()


def find_meal_by_photo(cur, photo_path: str):
    return cur.execute(
        "SELECT id, day_id, meal_num, est_calories, est_protein_g, est_carbs_g, est_fat_g FROM bfl_meals WHERE food_photo_path = ?",
        (photo_path,),
    ).fetchone()


def enrich_extraction(ext_path: pathlib.Path, db, cur, *, force: bool) -> dict | None:
    sidecar = ext_path.with_name(ext_path.stem + "_fdc.json")
    if sidecar.exists() and not force:
        return None

    data = json.loads(ext_path.read_text())
    if data.get("classification") != "food_photo":
        return None
    extraction = data.get("extraction") or {}
    items = extraction.get("items") or []
    total_g = extraction.get("estimated_portion_grams")
    if not items:
        return None

    # Vision-primary, FDC-fallback (<v1-reference-install> #513, chassis #90).
    #
    # FDC's Foundation Foods database is raw-ingredient-only — it systematically
    # under-reports kcal by 5-15% on every cooked meal because oil + cooking
    # losses + density changes aren't captured in raw values. If the linked
    # bfl_meals row already has vision-derived macros (est_calories non-null
    # and > 0), skip the FDC lookup entirely. FDC only runs as a fallback
    # when vision didn't produce macros.
    #
    # `--force` overrides this guard for explicit re-enrichment runs.
    source_path = data.get("source")
    if source_path and not force:
        meal_row = find_meal_by_photo(cur, source_path)
        if meal_row:
            est_kcal = meal_row.get("est_calories") if isinstance(meal_row, dict) else meal_row[3]
            if est_kcal is not None and float(est_kcal) > 0:
                # Vision already has macros — skip FDC. Drop a tiny sidecar so
                # this extraction isn't reprocessed every dispatcher tick.
                skip_payload = {
                    "source_extraction": str(ext_path),
                    "source_image": source_path,
                    "skipped_at": datetime.now(timezone.utc).isoformat(),
                    "skip_reason": "vision_primary_fdc_fallback (#90): bfl_meals row already has est_calories",
                    "est_kcal": float(est_kcal),
                }
                sidecar.write_text(json.dumps(skip_payload, indent=2))
                return None

    # v1 heuristic: equal-split total portion across items. Per-item gram
    # estimation is a follow-up.
    per_item_g = (float(total_g) / len(items)) if total_g else None

    item_results = []
    matched = 0
    for i, name in enumerate(items):
        try:
            match = fdc_lookup.lookup(
                name,
                portion_g=per_item_g,
                data_type_pref="FNDDS",
                db=db,
            )
        except Exception as e:
            sys.stderr.write(f"  {ext_path.stem}[{i}] {name!r}: lookup error: {e}\n")
            match = None
            if "rate-limited" in str(e):
                raise
        item_results.append({
            "item_order": i,
            "item_name": name,
            "assumed_portion_g": per_item_g,
            "match": match,
        })
        if match is not None:
            matched += 1
        time.sleep(0.05)

    coverage = matched / len(items) if items else 0.0

    agg = {"fdc_kcal": 0.0, "fdc_protein_g": 0.0, "fdc_carbs_g": 0.0, "fdc_fat_g": 0.0}
    for r in item_results:
        m = r["match"]
        if not m:
            continue
        agg["fdc_kcal"] += m.get("est_kcal") or 0.0
        agg["fdc_protein_g"] += m.get("est_protein_g") or 0.0
        agg["fdc_carbs_g"] += m.get("est_carbs_g") or 0.0
        agg["fdc_fat_g"] += m.get("est_fat_g") or 0.0
    for k in agg:
        agg[k] = round(agg[k], 2)

    source_path = data.get("source")
    meal_row = find_meal_by_photo(cur, source_path) if source_path else None

    now_unix = int(time.time())
    db_writes = {"updated_meal_id": None, "inserted_items": 0}
    if meal_row:
        meal_id = meal_row["id"] if isinstance(meal_row, dict) else meal_row[0]
        cur.execute("DELETE FROM bfl_meal_items WHERE meal_id = ?", (meal_id,))
        for r in item_results:
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
                    meal_id, r["item_order"], r["item_name"], r["assumed_portion_g"],
                    m.get("fdc_id"), m.get("description"), m.get("data_type"),
                    m.get("kcal_per_100g"), m.get("protein_per_100g"),
                    m.get("carbs_per_100g"), m.get("fat_per_100g"),
                    m.get("est_kcal"), m.get("est_protein_g"),
                    m.get("est_carbs_g"), m.get("est_fat_g"),
                    m.get("match_score"),
                ),
            )
        cur.execute(
            """
            UPDATE bfl_meals
               SET fdc_kcal = ?, fdc_protein_g = ?, fdc_carbs_g = ?, fdc_fat_g = ?,
                   fdc_enriched_at = ?, fdc_match_coverage = ?
             WHERE id = ?
            """,
            (
                agg["fdc_kcal"], agg["fdc_protein_g"], agg["fdc_carbs_g"], agg["fdc_fat_g"],
                now_unix, round(coverage, 3), meal_id,
            ),
        )
        db.commit()
        db_writes = {"updated_meal_id": meal_id, "inserted_items": len(item_results)}

    sidecar_payload = {
        "source_extraction": str(ext_path),
        "source_image": source_path,
        "enriched_at": datetime.now(timezone.utc).isoformat(),
        "v1_assumption_notes": "equal-split of total portion_g across N items — per-item gram estimation is a follow-up",
        "item_count": len(items),
        "items": item_results,
        "match_coverage": round(coverage, 3),
        "vision_estimate": {
            "kcal": extraction.get("estimated_calories"),
            "protein_g": extraction.get("estimated_protein_g"),
            "carbs_g": extraction.get("estimated_carbs_g"),
            "fat_g": extraction.get("estimated_fat_g"),
            "portion_g": extraction.get("estimated_portion_grams"),
        },
        "fdc_estimate": agg,
        "db_writes": db_writes,
    }
    sidecar.write_text(json.dumps(sidecar_payload, indent=2))
    return sidecar_payload


def main() -> int:
    default_extractions_dir = archive_root() / "extractions"
    ap = argparse.ArgumentParser()
    ap.add_argument("--extractions-dir", default=str(default_extractions_dir))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only", action="append", default=[])
    ap.add_argument("--since-days", type=int, default=None)
    args = ap.parse_args()

    ext_dir = pathlib.Path(args.extractions_dir)
    if not ext_dir.is_dir():
        print(f"Extractions dir not found: {ext_dir}", file=sys.stderr)
        return 1

    candidates: list[pathlib.Path] = []
    now = time.time()
    cutoff = (now - args.since_days * 86400) if args.since_days else None
    for p in sorted(ext_dir.glob("*.json")):
        if p.name == "_costs.jsonl":
            continue
        if p.stem.endswith("_fdc"):
            continue
        if args.only and p.stem not in args.only:
            continue
        if cutoff and p.stat().st_mtime < cutoff:
            continue
        candidates.append(p)

    if not candidates:
        print("No extractions to enrich.")
        return 0

    db = open_db()
    cur = _chassis_db.cursor(db, dict_rows=True)
    processed = 0
    skipped = 0
    errors = 0
    meal_summaries = []
    try:
        for p in candidates:
            try:
                result = enrich_extraction(p, db, cur, force=args.force)
            except Exception as e:
                sys.stderr.write(f"{p.stem}: ERROR {e}\n")
                errors += 1
                if "rate-limited" in str(e):
                    print("Aborting due to rate limit.")
                    break
                continue
            if result is None:
                skipped += 1
                continue
            processed += 1
            v = result["vision_estimate"]
            f = result["fdc_estimate"]
            print(f"  {p.stem}: cov={result['match_coverage']:.0%}  "
                  f"vision kcal={v.get('kcal')}→fdc kcal={f['fdc_kcal']}  "
                  f"vision P={v.get('protein_g')}→fdc P={f['fdc_protein_g']}  "
                  f"items={result['item_count']}  "
                  f"db={'yes' if result['db_writes']['updated_meal_id'] else 'no'}")
            meal_summaries.append({"stem": p.stem, "vision": v, "fdc": f, "coverage": result["match_coverage"]})
    finally:
        db.close()

    print(f"\nDone. processed={processed} skipped={skipped} errors={errors}")
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
