-- 001_bfl.sql — BFL (Body for Life) schema for the chassis.
--
-- Extracted from the V1 reference (db/migrations-pg/001_baseline.sql lines
-- 218-374) for the chassis BFL plugin. Postgres dialect; the chassis
-- _chassis_db selector also supports SQLite when USE_PG=false (legacy / dev
-- only — the canonical chassis backend is Postgres).
--
-- Run order (installer's bootstrap):
--   1. Chassis baseline schema (whatever the chassis ships)
--   2. This migration (assumes a `schema_migrations` registry table exists)
--
-- Tables (7):
--   bfl_days         — one row per session date; aggregates totals + photo paths
--   bfl_meals        — 1..6 meals per day (UNIQUE day_id+meal_num); both
--                       handwritten + photo-extracted populate this table
--   bfl_workouts     — one row per SET (not per exercise) — flat table for
--                       ad-hoc analytics; (day_id, exercise_order, set_number)
--                       reconstructs the pyramid
--   bfl_runs         — Strava + Oura reconciled run rows
--   bfl_aerobic      — non-run aerobic sessions + Strava-derived run mirrors
--                       so the dashboard renders aerobic on Strava-only days
--   bfl_meal_items   — per-item FDC enrichment; one row per food item per meal
--   fdc_food_cache   — USDA FoodData Central API response cache (per query+pref)
--
-- NOT YET PRESENT — deferred to a follow-up migration:
--   bfl_food_corrections — user-flagged FDC mismatches → correction-learning
--                           loop. Referenced in skills/bfl.md as a future
--                           table; deliberately not implemented in v1.

BEGIN;

-- ─── BFL (6 core tables + meal_items + fdc cache) ─────────────────────

CREATE TABLE bfl_days (
  id                      SERIAL PRIMARY KEY,
  date                    TEXT    NOT NULL UNIQUE,
  week_num                INTEGER,
  day_num                 INTEGER,
  day_type                TEXT,
  notebook_workout_photo  TEXT,
  notebook_meal_photo     TEXT,
  workout_start           TEXT,
  workout_end             TEXT,
  total_protein_portions  REAL,
  total_carb_portions     REAL,
  total_water_cups        REAL,
  ocr_raw_json            TEXT,
  notes                   TEXT,
  created_at              BIGINT NOT NULL DEFAULT extract(epoch from now())::bigint,
  updated_at              BIGINT NOT NULL DEFAULT extract(epoch from now())::bigint
);
CREATE INDEX idx_bfl_days_date     ON bfl_days(date);
CREATE INDEX idx_bfl_days_day_type ON bfl_days(day_type);

CREATE TABLE bfl_meals (
  id                    SERIAL PRIMARY KEY,
  day_id                INTEGER NOT NULL REFERENCES bfl_days(id) ON DELETE CASCADE,
  meal_num              INTEGER NOT NULL,
  time_planned          TEXT,
  time_actual           TEXT,
  description           TEXT,
  food_photo_path       TEXT,
  protein_portions      REAL,
  carb_portions         REAL,
  est_calories          INTEGER,
  est_protein_g         REAL,
  est_carbs_g           REAL,
  est_fat_g             REAL,
  est_portion_g         REAL,
  vision_confidence     REAL,
  vision_items_json     TEXT,
  vision_notes          TEXT,
  photo_matched         INTEGER NOT NULL DEFAULT 0,
  created_at            BIGINT NOT NULL DEFAULT extract(epoch from now())::bigint,
  fdc_kcal              REAL,
  fdc_protein_g         REAL,
  fdc_carbs_g           REAL,
  fdc_fat_g             REAL,
  fdc_enriched_at       BIGINT,
  fdc_match_coverage    REAL,
  manual_kcal           REAL,
  manual_protein_g      REAL,
  manual_carbs_g        REAL,
  manual_fat_g          REAL,
  manual_macros_at      BIGINT,
  manual_macros_notes   TEXT,
  UNIQUE(day_id, meal_num)
);
CREATE INDEX idx_bfl_meals_day ON bfl_meals(day_id);

CREATE TABLE bfl_workouts (
  id              SERIAL PRIMARY KEY,
  day_id          INTEGER NOT NULL REFERENCES bfl_days(id) ON DELETE CASCADE,
  exercise_order  INTEGER NOT NULL,
  exercise_name   TEXT    NOT NULL,
  muscle_group    TEXT,
  is_main         INTEGER NOT NULL DEFAULT 0,
  set_number      INTEGER NOT NULL,
  reps            INTEGER,
  weight_kg       REAL,
  rest_seconds    INTEGER,
  intensity_level INTEGER,
  is_high_point   INTEGER NOT NULL DEFAULT 0,
  up_arrow        INTEGER NOT NULL DEFAULT 0,
  notes           TEXT,
  created_at      BIGINT NOT NULL DEFAULT extract(epoch from now())::bigint
);
CREATE INDEX idx_bfl_workouts_day       ON bfl_workouts(day_id);
CREATE INDEX idx_bfl_workouts_exercise  ON bfl_workouts(exercise_name);

CREATE TABLE bfl_runs (
  id                       SERIAL PRIMARY KEY,
  day_id                   INTEGER REFERENCES bfl_days(id),
  strava_activity_id       TEXT    UNIQUE,
  start_time               BIGINT  NOT NULL,
  end_time                 BIGINT  NOT NULL,
  elapsed_seconds          INTEGER,
  distance_m               REAL,
  avg_pace_seconds_per_km  REAL,
  avg_hr                   REAL,
  max_hr                   INTEGER,
  route_geojson            TEXT,
  hr_samples_json          TEXT,
  hr_1min_buckets_json     TEXT,
  intensity_1min_json      TEXT,
  anecdotal_profile_json   TEXT,
  oura_pull_status         TEXT,
  notes                    TEXT,
  created_at               BIGINT NOT NULL DEFAULT extract(epoch from now())::bigint
);
CREATE INDEX idx_bfl_runs_day    ON bfl_runs(day_id);
CREATE INDEX idx_bfl_runs_strava ON bfl_runs(strava_activity_id);

CREATE TABLE bfl_aerobic (
  id                        SERIAL PRIMARY KEY,
  day_id                    INTEGER NOT NULL REFERENCES bfl_days(id) ON DELETE CASCADE,
  activity_type             TEXT,
  start_time                BIGINT,
  end_time                  BIGINT,
  total_minutes             INTEGER,
  planned_intensities_json  TEXT,
  actual_intensities_json   TEXT,
  hr_1min_buckets_json      TEXT,
  strava_activity_id        TEXT,
  notes                     TEXT,
  created_at                BIGINT NOT NULL DEFAULT extract(epoch from now())::bigint
);
CREATE INDEX idx_bfl_aerobic_day ON bfl_aerobic(day_id);

CREATE TABLE bfl_meal_items (
  id                    SERIAL PRIMARY KEY,
  meal_id               INTEGER NOT NULL REFERENCES bfl_meals(id) ON DELETE CASCADE,
  item_order            INTEGER NOT NULL,
  item_name             TEXT    NOT NULL,
  portion_g             REAL,
  fdc_id                INTEGER,
  fdc_description       TEXT,
  fdc_data_type         TEXT,
  fdc_kcal_per_100g     REAL,
  fdc_protein_per_100g  REAL,
  fdc_carbs_per_100g    REAL,
  fdc_fat_per_100g      REAL,
  est_kcal              REAL,
  est_protein_g         REAL,
  est_carbs_g           REAL,
  est_fat_g             REAL,
  match_score           REAL,
  created_at            BIGINT NOT NULL DEFAULT extract(epoch from now())::bigint
);
CREATE INDEX idx_bfl_meal_items_meal ON bfl_meal_items(meal_id);
CREATE INDEX idx_bfl_meal_items_fdc  ON bfl_meal_items(fdc_id);

-- ─── fdc_food_cache ─────────────────────────────────────────────────────
CREATE TABLE fdc_food_cache (
  id                SERIAL PRIMARY KEY,
  query_normalized  TEXT    NOT NULL,
  data_type_pref    TEXT,
  fdc_id            INTEGER NOT NULL,
  description       TEXT,
  data_type         TEXT,
  kcal_per_100g     REAL,
  protein_per_100g  REAL,
  carbs_per_100g    REAL,
  fat_per_100g      REAL,
  match_score       REAL,
  raw_json          TEXT,
  cached_at         BIGINT NOT NULL DEFAULT extract(epoch from now())::bigint,
  UNIQUE(query_normalized, data_type_pref)
);
CREATE INDEX idx_fdc_cache_query ON fdc_food_cache(query_normalized);

COMMIT;
