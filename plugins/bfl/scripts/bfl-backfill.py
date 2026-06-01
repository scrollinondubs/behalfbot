#!/usr/bin/env python3
"""bfl-backfill.py — bulk-ingest a Body for Life backfill ZIP.

Usage:
    plugins/bfl/scripts/bfl-backfill.py --zip path/to/bfl-backfill.zip --start 2026-03-30

Assumptions encoded:
- ZIP contains notebook pages photographed in chronological order.
- Camera filenames sort chronologically (IMG_NNNN.jpg pattern).
- For each day: workout page first, then meal page (2 pages/day).
- Sundays are REST days — no workout, meal tracked on the Saturday page.
  So a week = 12 pages (Mon-Sat × 2).
- Date interpolation walks Mon → Sat, skipping Sunday entirely.

Pipeline per photo:
1. Unzip + sort by filename
2. Assign `(date, role)` per position
3. Run bfl-vision-extract.py on each (hybrid: Opus handwriting / GPT-4o-mini food)
4. Run bfl-ingest.py with `--date-override` (interpolated date wins over
   any OCR/EXIF date so hallucinations + photo-capture drift are neutralised)
5. Post ONE rollup summary to the health channel at the end, not N confirmations

Flags:
    --zip PATH                  path to the backfill zip (required)
    --start YYYY-MM-DD          anchor date for interpolation
    --dest DIR                  unzip destination (default ${BFL_ARCHIVE_DIR}/raw/backfill)
    --dry-run                   do everything except ingest

Env vars:
    BFL_ARCHIVE_DIR        absolute path; default ~/behalfbot-archive/bfl
    HEALTH_WEBHOOK_URL     optional; rollup summary posted here when set
"""
from __future__ import annotations
import argparse
import json
import os
import pathlib
import subprocess
import sys
import urllib.request
import zipfile
from datetime import date, timedelta

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def archive_root() -> pathlib.Path:
    explicit = os.environ.get("BFL_ARCHIVE_DIR")
    if explicit:
        return pathlib.Path(explicit).expanduser()
    return pathlib.Path.home() / "behalfbot-archive" / "bfl"


def interpolate_date(start: date, position: int) -> tuple[date, str]:
    """Position i → (session_date, role). Sundays skipped.

    Sequence: Mon-workout, Mon-meal, Tue-workout, Tue-meal, ... Sat-workout, Sat-meal,
    then next Monday.
    """
    week = position // 12
    within_week = position % 12
    day_of_week = within_week // 2   # 0=Mon .. 5=Sat
    role = "workout" if within_week % 2 == 0 else "meal"
    session = start + timedelta(days=week * 7 + day_of_week)
    return session, role


def post_discord(webhook: str, content: str) -> None:
    req = urllib.request.Request(
        webhook,
        data=json.dumps({"content": content}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "behalfbot-bfl-backfill (1.0)"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"WARN: discord post failed: {e}", file=sys.stderr)


def main() -> int:
    root = archive_root()
    extractions_dir = root / "extractions"
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", required=True)
    ap.add_argument("--start", required=True, help="Anchor date for interpolation (YYYY-MM-DD)")
    ap.add_argument("--dest", default=str(root / "raw" / "backfill"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0,
                    help="only process first N images after sort (0 = all). Useful for first-day smoke test.")
    args = ap.parse_args()

    zip_path = pathlib.Path(args.zip).expanduser().resolve()
    if not zip_path.exists():
        print(f"FAIL: zip not found: {zip_path}", file=sys.stderr)
        return 1
    dest = pathlib.Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    start = date.fromisoformat(args.start)

    # Step 1: unzip
    print(f"Unzipping {zip_path} → {dest}")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)

    imgs = sorted([p for p in dest.rglob("*")
                   if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png")
                   and not p.name.startswith("._")])
    if not imgs:
        print("No images found in zip.")
        return 1
    print(f"Found {len(imgs)} images.")

    if args.limit and args.limit < len(imgs):
        imgs = imgs[:args.limit]
        print(f"--limit {args.limit}: processing first {len(imgs)} only")

    # Step 2: assign interpolated (date, role)
    plan = []
    for i, p in enumerate(imgs):
        session, role = interpolate_date(start, i)
        plan.append({"position": i, "file": p, "session_date": session.isoformat(), "expected_role": role})

    print("\n=== Interpolation plan ===")
    for row in plan[:6] + plan[-3:]:
        print(f"  [{row['position']:>2}] {row['file'].name:18} → {row['session_date']}  {row['expected_role']}")
    if len(plan) > 9:
        print(f"  ... ({len(plan) - 9} more)")

    if args.dry_run:
        print("\n--dry-run, stopping before extraction.")
        return 0

    # Step 3: vision extract
    print("\n=== Running hybrid extraction ===")
    image_parent = imgs[0].parent
    only_args = []
    for p in imgs:
        only_args += ["--only", p.stem]
    extractor = PLUGIN_DIR / "scripts" / "bfl-vision-extract.py"
    subprocess.run(
        [sys.executable, str(extractor),
         "--input-dir", str(image_parent), "--out-dir", str(extractions_dir), *only_args],
        check=False,
    )

    # Step 4: batch ingest with per-file date override
    print("\n=== Ingesting ===")
    ingester = PLUGIN_DIR / "scripts" / "bfl-ingest.py"
    ingested = 0
    failed = 0
    role_mismatches = []
    total_cost = 0.0
    per_day_summary = {}
    for row in plan:
        p = row["file"]
        ej = extractions_dir / (p.stem + ".json")
        if not ej.exists():
            print(f"  SKIP {p.name}: no extraction JSON")
            failed += 1
            continue
        data = json.loads(ej.read_text())
        total_cost += data.get("cost_usd", 0.0)
        actual_cls = data.get("classification")
        expected_role = row["expected_role"]
        expected_cls = {"workout": "workout_log", "meal": "meal_log"}.get(expected_role)
        if actual_cls != expected_cls:
            role_mismatches.append({
                "position": row["position"],
                "file": p.name,
                "expected": expected_cls,
                "actual": actual_cls,
                "session_date": row["session_date"],
            })
        rc = subprocess.run(
            [sys.executable, str(ingester),
             str(ej), "--photo", str(p), "--quiet",
             "--date-override", row["session_date"]],
        ).returncode
        if rc == 0:
            ingested += 1
            d = row["session_date"]
            per_day_summary.setdefault(d, {"workouts": 0, "meals": 0, "other": 0})
            if actual_cls == "workout_log": per_day_summary[d]["workouts"] += 1
            elif actual_cls == "meal_log":   per_day_summary[d]["meals"] += 1
            else:                            per_day_summary[d]["other"] += 1
        elif rc == 2:
            ingested += 1  # skipped as 'other', not a failure
        else:
            failed += 1
            print(f"  FAIL {p.name}: rc={rc}")

    # Step 5: rollup
    print("\n=== Rollup ===")
    print(f"Total photos: {len(plan)} | ingested: {ingested} | failed: {failed}")
    print(f"Total extraction cost: ${total_cost:.4f}")
    print(f"Role mismatches: {len(role_mismatches)}")
    for rm in role_mismatches:
        print(f"  [{rm['position']}] {rm['file']}: expected {rm['expected']} got {rm['actual']} on {rm['session_date']}")

    webhook = os.environ.get("HEALTH_WEBHOOK_URL")
    if webhook:
        summary = (
            f"**BFL backfill complete** — {ingested}/{len(plan)} photos ingested, "
            f"{failed} failed, {len(role_mismatches)} role mismatches. "
            f"Total extraction cost: **${total_cost:.2f}**. "
            f"Span: {plan[0]['session_date']} → {plan[-1]['session_date']}."
        )
        post_discord(webhook, summary)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
