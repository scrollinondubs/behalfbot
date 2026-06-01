---
name: bfl
description: Body for Life quantified-self pipeline — ingesting notebook photos (meals / weight-training / aerobic / food), reconciling runs with Strava + Oura HR, writing to bfl_* tables in the chassis Postgres DB. Use when the user uploads BFL photos or asks to reconcile a run.
---

# BFL — Body for Life Quantified-Self Skill

Read this whenever the user uploads Body for Life photos (notebook pages, food photos) or asks to reconcile a run, review weekly gym progress, or analyze HR overlays against hand-drawn anecdotal profiles.

The plugin ships:
- `plugins/bfl/db/migrations/001_bfl.sql` — the 7 tables (`bfl_days`, `bfl_meals`, `bfl_workouts`, `bfl_runs`, `bfl_aerobic`, `bfl_meal_items`, `fdc_food_cache`)
- `plugins/bfl/scripts/bfl-vision-extract.py` — hybrid cloud vision (Claude Opus + GPT-4o-mini)
- `plugins/bfl/scripts/bfl-ingest.py` — DB upsert from extraction JSON
- `plugins/bfl/scripts/bfl-backfill-meal.py` — manual meal insertion (no-photo path)
- `plugins/bfl/scripts/bfl-fdc-enrich.py` + `bfl-fdc-enrich-descriptions.py` — USDA macro enrichment
- `plugins/bfl/scripts/bfl-run-reconcile.py` — Strava + Oura HR overlay
- `plugins/bfl/scripts/strava-ingest.py` — Strava-first activity polling
- `plugins/bfl/scripts/bfl-model-bench.py` — vision-model benchmarking
- `plugins/bfl/scripts/bfl-backfill.py` — bulk-ingest a ZIP of pages

## When this fires

- The user posts photos of their BFL notebook (workout logs, meal logs, aerobic graphs, food photos) to the configured health channel.
- The `bfl-ingest` heartbeat fires (when the gather script has work to escalate).
- The user asks "reconcile yesterday's run" or "show me my HR profile vs the anecdotal".
- A weekly-review gather phase wants BFL compliance stats for the rollup.

## What's in the photos

The user uses an abbreviated handwritten version of the Body for Life planner. They log ACTUALs only (not PLAN). Typical shots:

1. **Workout log page** — weight-training day (upper or lower). Exercises with set number, reps, weight in **kilograms (kg)**, intensity 1-10. Column order on each row: `reps | weight_kg | intensity`. Usually has a start time in the upper-left of the page. BFL structure: 1 main exercise for 5 sets, then switch to a different exercise in the same muscle group for the final sets.
   - **Up-arrow notation**: occasionally a `↑` next to a set means "make this heavier next time." Capture as `up_arrow: true` on that set.
   - **Bodyweight / no-weight machines**: `/` or `0` in the weight column means bodyweight or machine-level. Map to `weight_kg: null` in extraction.
   - **Sections**: chest, back, shoulders, biceps, triceps (upper body); quads, hamstrings, calves, abs (lower body).
2. **Meal log page** — 6 meals with **actual times** (never planned), short descriptions ("turkey omelette", "tacos + quesadillas"), protein portions count, carb portions count. **Water**: tracked as tick marks at the bottom; each tick ≈ 12oz glass → store as `total_water_cups`. Daily totals at top of page.
3. **Aerobic page** — 20-min cardio with minute-by-minute intensity 0-10 + 4 peaks (approx minutes 6, 10, 14, 19 with a flat-out peak at 19) and a 1-min cooldown. Often has a hand-drawn intensity curve.
4. **Food photo** — actual plated meal. Not a notebook page.

## Core pipeline (per image)

### Step 1. Classify

Cloud-only via `bfl-vision-extract.py`. Routes:
- Classifier: `gpt-4o-mini` (cheap, reliable single-word classifier)
- workout_log / meal_log / aerobic_log: Claude Opus via `claude -p` (best handwriting accuracy; bills against the user's Claude Code subscription, NOT PAYG API)
- food_photo: `gpt-4o-mini` (good enough, ~20x cheaper than Opus)
- other: skip

If `gpt-4o-mini` returns `other`, the script escalates to Opus once (rotated pages and hand-drawn graphs sometimes confuse the small model).

The earlier local-Ollama path (qwen2.5vl / llava) is removed — it returned unusable output on dense handwriting.

### Step 2. Structured JSON extract

Use a per-type prompt that says "Return ONLY valid JSON, no prose." Models return code-fenced JSON ~90% of the time; the extraction helper (`parse_json_ish`) strips fences + locates the first `{` to last `}`. Temperature 0.1 (Opus rejects `temperature` so it's omitted there).

Schemas the extract MUST target (do NOT invent additional fields — the SQL migration doesn't hold them):

**workout_log:** `{date, workout_start, day_type, muscle_groups_worked, exercises:[{name, muscle_group, is_main, sets:[{set, reps, weight_kg, intensity, up_arrow}]}], notes}`

**meal_log:** `{date, meals:[{meal_num, time_actual, description, protein_portions, carb_portions}], total_protein_portions, total_carb_portions, water_tick_marks, notes}` — each tick = ~12oz glass, persist as `total_water_cups` in `bfl_days`.

**aerobic_log:** `{date, workout_start, activity_type, total_minutes, actual_intensities_json, notes}` — `actual_intensities_json` is an array of integers, one per minute of the workout.

**food_photo:** `{items, estimated_portion_grams, estimated_calories, estimated_protein_g, estimated_carbs_g, estimated_fat_g, confidence, notes}`

### Step 3. Known quirks + corrections

- **Year hallucinations** — the model sometimes writes a wrong year. Always sanity-check the extraction date against EXIF `DateTimeOriginal` (the ingest script does this automatically; if the model's year is more than ±1 from EXIF year, EXIF wins).
- **Meal-time jumbling** — for meal logs, the model occasionally assigns times out of order. Don't auto-correct — flag and ask the user.
- **Exercise name misspellings** — proper nouns often jumbled ("Dumb Lunges" for "Dumbbell Lunges"). Post-process against the known BFL vocabulary below.

### Step 4. Ingest into the BFL DB

The chassis canonical backend is **Postgres**. Ingest scripts route through `plugins/bfl/scripts/_chassis_db.py` `connect()`, which honours `USE_PG=true` (default) and falls back to SQLite only when `USE_PG=false` is set explicitly. The DSN comes from `BEHALFBOT_PG_DSN` (or `CHASSIS_PG_DSN` as a V1-compat alias).

For a workout day:
1. `INSERT INTO bfl_days (date, day_type, notebook_workout_photo, workout_start, ocr_raw_json, ...) ON CONFLICT(date) DO UPDATE`
2. For each exercise, `INSERT INTO bfl_workouts (day_id, exercise_order, exercise_name, muscle_group, is_main, set_number, reps, weight_kg, intensity_level, up_arrow)` — one row per set. Set `weight_kg=NULL` when the column showed `/` or `0` (bodyweight machine). Set `up_arrow=1` when the user drew `↑` next to that specific set.

For a meal day:
1. `INSERT INTO bfl_days (date, notebook_meal_photo, total_protein_portions, total_carb_portions, total_water_cups, ocr_raw_json, ...) ON CONFLICT(date) DO UPDATE`
2. For each meal, `INSERT INTO bfl_meals (day_id, meal_num, time_actual, description, protein_portions, carb_portions)` — one row per meal, UNIQUE(day_id, meal_num)

For an aerobic day:
1. `INSERT INTO bfl_days (date, day_type='aerobic', ...)` — UPSERT
2. `INSERT INTO bfl_aerobic (day_id, activity_type, total_minutes, actual_intensities_json, notes)` — one row per session.

For a food photo:
1. The day's `bfl_days` row gets created if missing.
2. Idempotency: if a `bfl_meals` row already exists with `food_photo_path = <photo>`, UPDATE it; otherwise INSERT a new meal row with the next free `meal_num`.
3. Vision-derived fields (`est_calories`, `est_protein_g`, etc., `vision_items_json`, `vision_confidence`) populate from the extraction.
4. `photo_matched=1` so the dashboard can distinguish photo-derived vs handwritten meals.

For a no-photo plain-English Discord backfill (manual case): use `plugins/bfl/scripts/bfl-backfill-meal.py` instead of inline SQL — it handles the day-row upsert, next-meal-num selection, and backend-aware INSERT. The `Backfill:` Discord trigger (declared in `openclaw.plugin.json` `contracts.triggers`) routes here automatically.

### Step 4b. FDC macro enrichment

After the vision-extract step writes an extraction JSON, enrich with USDA FoodData Central to replace vision's one-shot macro guesses with database-grounded numbers:

```
plugins/bfl/scripts/bfl-fdc-enrich.py --since-days 1
```

The script:
1. Reads each `food_photo` extraction JSON, equal-splits the total portion grams across the N items (v1 heuristic — per-item gram estimation is a follow-up).
2. Calls `fdc_lookup.lookup(item_name, portion_g)` for each item. Results cached in `fdc_food_cache` (same item name queried later hits cache, not API).
3. Sub-DB ranking prefers FNDDS > SR Legacy > Foundation > Branded (canonical > crowdsourced).
4. Writes `*_fdc.json` sidecar next to each extraction with per-item matches + aggregate totals + vision-vs-FDC delta.
5. If the source photo is linked to a `bfl_meals` row via `food_photo_path`, also writes `bfl_meal_items` rows (one per item) and updates `bfl_meals.fdc_kcal / fdc_protein_g / fdc_carbs_g / fdc_fat_g / fdc_enriched_at / fdc_match_coverage`.

For description-only meals (handwritten meal log without a food photo), use `bfl-fdc-enrich-descriptions.py` — it tokenises the free-text description, looks up each token in FDC, and writes the same aggregate columns.

Known v1 matching issues:
- Single-word queries like `"brown rice"` may hit a raw/dry Branded entry, not cooked FNDDS → kcal inflation
- Common names like `"corn"`, `"lime"` match into compound dishes (`"Corn beverage"`, `"Lime souffle"`)
- Equal-split portions overweight low-density items (collagen powder vs banana in a smoothie)

Do NOT blindly trust enriched totals yet — flag edge cases to the user. The correction-learning loop (a future `bfl_food_corrections` table) is out of scope for v1; the table is referenced here as a forward-looking placeholder, NOT created by `001_bfl.sql`.

### Step 5. Run reconciliation (runs only)

Runs don't come from notebook photos — they come from **Strava + Oura**. Two scripts cover this:

- `strava-ingest.py` — heartbeat-driven Strava-first ingest. Polls Strava activities since last-seen, upserts into `bfl_runs`, posts a summary to the health channel.
- `bfl-run-reconcile.py` — deeper enrichment. Pulls Oura `/heartrate` for the activity window, re-samples HR samples into 1-min buckets, computes 0-10 intensity per minute (Karvonen-style with `hr_max=190, hr_rest=55` defaults — tune per-user via `chassis.config.yaml modules.bfl.hr_max / hr_rest`).

**Critical Oura config**: the user must explicitly start **Workout Heart Rate** mode on the ring before the run. Without WHR, `/heartrate` returns samples at 5-minute intervals, which blurs the 4-peak BFL profile beyond usefulness. If `oura_pull_status='coarse'`, flag it in the Discord summary so the user knows to enable WHR next time.

## Known BFL exercise vocabulary (use for name correction)

### Upper body
- Bench Press (flat / incline / decline)
- Dumbbell Press
- Dumbbell Fly
- Seated Row
- Lat Pulldown
- Pull-up / Chin-up
- Bent-Over Row
- Shoulder Press (seated / standing)
- Lateral Raise
- Front Raise
- Rear Delt Fly
- Tricep Pushdown
- Tricep Extension (overhead / skull crusher)
- Dumbbell Curl
- Barbell Curl
- Hammer Curl
- Preacher Curl

### Lower body
- Squat (back / front / hack)
- Leg Press (45° / seated / horizontal)
- Leg Extension
- Lying Leg Curl (aka Leg Curl)
- Seated Leg Curl
- Dumbbell Lunge
- Walking Lunge
- Romanian Deadlift
- Standing Calf Raise
- Seated Calf Raise

### Core / incidental
- Plank
- Crunch
- Hanging Leg Raise
- Russian Twist

If the model returns a name not in this list, store it raw and tag `name_unknown=1`. Don't silently rewrite — let the user review periodically and map it.

## Weekly rollup integration

The chassis weekly rollup heartbeat (when configured) should pull from `bfl_days` + children:

- **BFL compliance**: `X of 6 weight-training days hit this week + Y of 6 aerobic days`
- **Nutrition adherence**: average daily `total_protein_portions` + `total_carb_portions` vs target of 6/6
- **Run HR trend**: did the 4-peak profile match the anecdotal? Overlay delta over 4 weeks
- **Strength progression**: for each core lift, max weight lifted per week

Don't overload the rollup — one bullet per topic area.

## Failure modes + recovery

| Symptom | Cause | Fix |
|---|---|---|
| Classification returns `other` | Image is blurry or not a notebook/food | Ask the user to reshoot; log path to `${BFL_ARCHIVE_DIR}/unmatched/` |
| JSON parse fails on extract | Model wrapped response in prose | Re-run with explicit "ONLY valid JSON, no prose" prompt or use `parse_json_ish` helper |
| Date extracted wrong year | Model hallucinated | Override with EXIF `DateTimeOriginal` — this is the default path in the ingest script |
| `oura_pull_status='coarse'` | WHR mode was not active on the ring | Flag in daily report; next run start WHR first |
| Strava rate limit hit during backfill | 200/15min exceeded | Auto-sleep for the remaining window or run again later |
| Food photo doesn't match any meal | Ad-hoc eating / not logged OR low confidence | Flag in morning briefing, store at `${BFL_ARCHIVE_DIR}/unmatched/` |
| Workout page has no start/end time | The user forgot to log it | Fallback to `NULL`; don't block the rest of the ingest |

## Reference paths

- **Raw photos**: `${BFL_ARCHIVE_DIR}/raw/` (local-only, never committed)
- **Extraction JSONs**: `${BFL_ARCHIVE_DIR}/extractions/` (rebuildable)
- **Unmatched food photos**: `${BFL_ARCHIVE_DIR}/unmatched/`
- **Ground truth (benchmarking)**: `${BFL_ARCHIVE_DIR}/ground-truth/`
- **Benchmark runs**: `${BFL_ARCHIVE_DIR}/benchmark/`
- **Migration**: `plugins/bfl/db/migrations/001_bfl.sql`
- **Schema tables**: `bfl_days`, `bfl_meals`, `bfl_workouts`, `bfl_runs`, `bfl_aerobic`, `bfl_meal_items`, `fdc_food_cache`

`${BFL_ARCHIVE_DIR}` defaults to `~/behalfbot-archive/bfl` and is configurable per `chassis.config.yaml modules.bfl.archive_dir`.

## Backfill trigger

A Discord trigger registered in `openclaw.plugin.json` `contracts.triggers`:

- **Keyword**: `^Backfill[\s:]+` (case-insensitive)
- **Channel**: the configured health channel
- **Handler**: `plugins/bfl/triggers/backfill.sh`
- **Action**: parses a plain-English meal description from the message body and invokes `bfl-backfill-meal.py` with structured `--description / --time-actual / --protein-portions / --carb-portions` flags.

Examples the user might post:
- `Backfill: one protein shake 9am` → meal #N, time 09:00, description "protein shake", protein_portions=1.0
- `Backfill: two scrambled eggs and a slice of toast at 7:30am` → meal #N, time 07:30, description "two scrambled eggs and a slice of toast", protein_portions=2.0, carb_portions=1.0
- `Backfill one banana 3pm` → meal #N, time 15:00, description "one banana", carb_portions=1.0
- `Backfill: mid-afternoon snack — handful of almonds` (no time) → meal #N, time NULL, description "handful of almonds"

Parsing rules (the trigger handler does this in-message before calling the script):
- Time at the end (`9am`, `9:30 AM`, `15:00`, `at 7:30am`, `noon`) → `--time-actual` (normalize to `H:MM AM/PM`).
- Quantity word (`one`, `two`, `three`) or numeral at the start of an item → portion count for that item.
- Item type classification: protein items (shake, eggs, chicken, fish, turkey, beef, yoghurt, cottage cheese, whey, casein, jerky) → `--protein-portions`. Carb items (rice, oats, toast, banana, apple, sweet potato, tortilla, pasta) → `--carb-portions`. Mixed meals: split sensibly.
- Free-text description goes verbatim into `--description`.

If parsing is ambiguous (no time, no quantity, food item the parser can't classify), the handler should reply with one clarifying question rather than guessing. Don't invent macro values — leave them NULL the same way photo-ingested meals do.

## Change log

- **0.1.0** — extracted from V1 reference into chassis plugin. Postgres canonical via `_chassis_db`; SQLite legacy fallback. Backfill Discord trigger registered. Installer-personal coupling (secret-store item IDs, hard-coded TZ, Apple-specific paths) generalized.
