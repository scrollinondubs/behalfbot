# Plugin: BFL (Body for Life)

Quantified-self pipeline for the Body for Life program. Ingests handwritten notebook photos (workout logs, meal logs, hand-drawn aerobic graphs) + plated food photos via cloud vision (Claude Opus + GPT-4o-mini routing), reconciles runs with Strava + Oura HR, enriches macros via USDA FoodData Central. Postgres-by-default via the chassis `_chassis_db` selector.

This plugin is **dormant by default**. Quantified-self automation requires deliberate opt-in.

## When to enable

- The installer wants automated ingestion of a handwritten BFL notebook (the canonical use-case the V1 reference was built around).
- The installer wants Strava activities to land in a structured DB with HR overlays from Oura.
- The installer wants USDA-grounded macros for plated meal photos.

If the installer doesn't track BFL, leave this plugin disabled. The chassis surfaces the `modules.bfl.enabled` knob so installers can toggle without uninstalling.

## Activation

```yaml
# chassis.config.yaml
modules:
  bfl:
    enabled: true
    archive_dir: ~/behalfbot-archive/bfl
    notebook_ingest: true
    strava_oura_reconcile: false  # opt-in: only enable if installer GPS-tracks aerobic workouts
    fdc_enrich: false             # opt-in: vision-primary is the V1 default (see Lesson refs below)
    health_channel_id: <DISCORD_CHANNEL_ID>
    health_channel_name: <installer>-health
    ops_channel_name: <installer>-ops
    daily_macro_targets:
      protein_portions: 6
      carb_portions: 6
      water_cups: 10
    hr_max: 190
    hr_rest: 55
    database: postgres
```

The bootstrap maps these to env vars the scripts read:

```bash
export BFL_ARCHIVE_DIR="${HOME}/behalfbot-archive/bfl"
export HEALTH_CHANNEL_ID="..."
export HEALTH_WEBHOOK_URL="..."     # Discord webhook for confirmations
export OPS_WEBHOOK_URL="..."        # Discord webhook for failure escalations
export USDA_FDC_API_KEY="..."
# Strava + Oura (for the strava_oura_reconcile path):
export STRAVA_CLIENT_ID="..."
export STRAVA_CLIENT_SECRET="..."
export STRAVA_ACCESS_TOKEN="..."
export STRAVA_REFRESH_TOKEN="..."
export STRAVA_TOKEN_EXPIRES_AT="..."
export OURA_TOKEN="..."
# Database (canonical):
export USE_PG=true
export BEHALFBOT_PG_DSN="postgresql://user:PASS@host:5432/db"
```

## Archive directory layout

`${BFL_ARCHIVE_DIR}` (default `~/behalfbot-archive/bfl`) holds local-only files. Never committed, never copied into the chassis git tree.

```
${BFL_ARCHIVE_DIR}/
├── raw/                   # Source images downloaded from the health channel
│   ├── IMG_8451.jpg
│   ├── IMG_8451.caption.txt   # optional; written by the gather script
│   │                          # from the Discord message body when present
│   └── ...
├── extractions/           # Vision-extract output JSONs (rebuildable)
│   ├── IMG_8451.json      # {source, classification, extraction, cost_usd, ...}
│   ├── IMG_8451_fdc.json  # FDC enrichment sidecar (food_photo only)
│   ├── _costs.jsonl       # running per-extraction cost log
│   └── ...
├── unmatched/             # Food photos that didn't match any meal row
├── ground-truth/          # Hand-verified extraction JSONs for benchmarking
└── benchmark/             # Per-model bench output (bfl-model-bench.py)
    ├── claude-opus-4-7/
    ├── claude-sonnet-4-6/
    └── gpt-4o-mini/
```

## Schema

`plugins/bfl/db/migrations/001_bfl.sql` adds 7 tables:

| Table | Purpose |
|---|---|
| `bfl_days` | One row per session date; aggregates totals + photo paths + day_type ('upper' / 'lower' / 'aerobic') |
| `bfl_meals` | 1..6 meals per day (UNIQUE day_id+meal_num); both handwritten + photo-extracted populate this table |
| `bfl_workouts` | One row per **set** (not per exercise); flat table for ad-hoc analytics. (day_id, exercise_order, set_number) reconstructs the pyramid |
| `bfl_runs` | Strava + Oura reconciled run rows (HR samples, 1-min buckets, intensity profile) |
| `bfl_aerobic` | Non-run aerobic sessions + Strava-derived run mirrors so the dashboard renders aerobic on Strava-only days |
| `bfl_meal_items` | Per-item FDC enrichment; one row per food item per meal |
| `fdc_food_cache` | USDA FoodData Central API response cache (UNIQUE per query+pref) |

**Future / not-yet-implemented:** `bfl_food_corrections` is referenced in `skills/bfl.md` as the target table for an FDC correction-learning loop. **It is NOT created by `001_bfl.sql`** — flag this when the loop ships.

Run order (installer's bootstrap):
1. Chassis baseline schema (whatever the chassis ships)
2. `psql "$BEHALFBOT_PG_DSN" -f plugins/bfl/db/migrations/001_bfl.sql`

## Heartbeat registration

The plugin ships `plugins/bfl/scheduled-tasks/gather-bfl-photos.sh` (the gather script) and `bfl-ingest-prompt.md` (the Claude fallback prompt). The installer registers the heartbeat in `${CHASSIS_HOME}/HEARTBEATS.md` (NOT inside `chassis/`, per anti-pattern #17):

```yaml
## bfl-ingest

```yaml
schedule: every 15m
gather: ${CHASSIS_HOME}/plugins/bfl/scheduled-tasks/gather-bfl-photos.sh
condition: threshold count > 0
prompt: ${CHASSIS_HOME}/plugins/bfl/scheduled-tasks/bfl-ingest-prompt.md
model: opus
budget: 1
criticality: normal
```
```

Per the chassis lesson #11, the gather script + manifest are dead until this block is added to `HEARTBEATS.md`. The plugin manifest's `contracts.heartbeats: ["bfl-ingest"]` is a declaration, not a registration.

The gather script's contract:
- Polls Discord for new photos in `HEALTH_CHANNEL_ID` since the last seen message id (state in `${CHASSIS_HOME}/scheduled-tasks/bfl-health-state.json`).
- Downloads attachments to `${BFL_ARCHIVE_DIR}/raw/`, writes message body to `<basename>.caption.txt` if present.
- Runs `bfl-vision-extract.py` on new images (cheap no-op when nothing's new).
- Runs `bfl-ingest.py` on each new extraction JSON. `food_photo` extractions also trigger `bfl-fdc-enrich.py`.
- Emits `{"count": N, "failed": [...]}` on stdout. N is the count of items that **failed** local processing — the heartbeat fires Claude only when the cheap path couldn't resolve them.

## Discord trigger registration

The plugin manifest declares one trigger in `contracts.triggers`:

| Field | Value |
|---|---|
| `name` | `backfill` |
| `keyword_regex` | `^Backfill[\s:]+` |
| `channel_filter` | `${HEALTH_CHANNEL_ID}` |
| `parser` | `bfl-natural-language-meal` (parser library lands with chassis trigger framework #506) |
| `handler` | `${CHASSIS_HOME}/plugins/bfl/triggers/backfill.sh` |
| `react_emoji` | `fork_and_knife` |

The handler is a **stub** in v0.1.0 — it echoes received env vars and emits `{"ok": false, "reason": "trigger_framework_pending"}` until the chassis trigger dispatch framework (<v1-reference-install>#506) lands. Once #506 ships, the dispatcher will populate `TRIGGER_PARSED_ARGS_JSON` with `{description, time_actual, protein_portions, carb_portions}` and the handler will invoke `bfl-backfill-meal.py` with those structured flags.

Use cases the trigger covers (no-photo manual meal logging):

```
Backfill: one protein shake 9am
Backfill: two scrambled eggs and a slice of toast at 7:30am
Backfill one banana 3pm
Backfill: mid-afternoon snack — handful of almonds
```

## Strava + Oura wiring

`strava_oura_reconcile: true` activates two scripts:

### `strava-ingest.py` — heartbeat-driven Strava-first ingest

- Polls `/athlete/activities?after=<last_seen>` since the last good run.
- Auto-refreshes the OAuth access token when within 5 min of expiry.
- Filters out wearable-generated zero-distance pseudo-activities (Apple Watch / Oura step-count mirrors).
- Reclassifies "Ride" activities slower than 3:00/km as "Run" (Apple Watch auto-detect misfires).
- UPSERT into `bfl_runs` via `strava_activity_id` UNIQUE.
- Posts a 4-field summary (type, distance, pace, duration, avg HR) to `HEALTH_WEBHOOK_URL`.
- After 3 consecutive ticks with insert failures, escalates to `OPS_WEBHOOK_URL`.

OAuth token persistence: V1 reference round-tripped rotated tokens through Vaultwarden. The chassis port does in-process rotation only by default (next invocation must re-refresh). To round-trip tokens through your secret store, override `_persist_token_updates()` in the installer's bootstrap. See the docstring in `plugins/bfl/scripts/strava-ingest.py`.

To wire a fresh Strava OAuth app:
1. Create an app at https://www.strava.com/settings/api (any redirect URL works for personal use).
2. Authorize a one-time auth-code grant. Strava returns `access_token`, `refresh_token`, `expires_at`.
3. Store all three plus `client_id` + `client_secret` in your secret store (Vaultwarden / Doppler / 1Password / `.env`).
4. The bootstrap exports them as `STRAVA_*` env vars at chassis startup.

### `bfl-run-reconcile.py` — deeper enrichment

Pulls Oura `/v2/usercollection/heartrate?start_datetime=...&end_datetime=...` for the activity window, re-samples HR samples into 1-min buckets, computes 0-10 intensity per minute (Karvonen-style with `hr_max=190, hr_rest=55` defaults — tune per user via `chassis.config.yaml modules.bfl.hr_max / hr_rest`).

To wire Oura:
1. Create a personal access token at https://cloud.ouraring.com/personal-access-tokens.
2. Store as `OURA_TOKEN`.
3. Critically: the user must explicitly start **Workout Heart Rate** mode on the ring before runs. Without WHR, `/heartrate` returns 5-min-interval samples that won't resolve a 4-peak intensity profile. The script flags `oura_pull_status='coarse'` so the channel summary nudges the user.

## FDC API key setup

`fdc_enrich: true` activates the macro enrichment scripts. They use the USDA FoodData Central public API.

- `DEMO_KEY` works at low volume but rate-limits aggressively (1000 req/hour per IP).
- For real usage, register a personal key at https://api.data.gov/signup/ — free, 1000 req/hour per key, no PII required.
- Store as `USDA_FDC_API_KEY`.

The cache layer (`fdc_food_cache`) means a given query is hit once across all of an installer's meal history — the API only sees novel queries. After the first month or two of meals, most lookups are cache hits.

## Cost expectations

Per the V1 reference's cost log (sample size: ~100 photos):

| Photo type | Model | Tokens in/out | Cost |
|---|---|---|---|
| Workout / aerobic / meal log | Claude Opus 4.7 | ~1500 / 600 | ~$0.07 |
| Food photo | gpt-4o-mini | ~1500 / 200 | ~$0.0004 |
| Classifier | gpt-4o-mini | ~1000 / 5 | ~$0.0002 |

For a typical BFL week (12 notebook pages + ~6 food photos): ~$0.85/week handwriting extraction + ~$0.003/week food photos.

Strava + Oura calls have no marginal cost (free tiers cover personal usage with margin).

USDA FDC: free.

## What does NOT ship

- V1-installer-specific coupling: hard-coded user-home paths, fixed-TZ assumptions, Vaultwarden item IDs for Strava token rotation, hard-coded Discord channel IDs, references to specific reMarkable document IDs for the BFL planner PDF, Apple Find My / Mac Mini topology bindings.
- The `Backfill:` trigger handler is a **stub** — full handler body lands once the chassis trigger dispatch framework + parser library ships (tracked at <v1-reference-install>#506).
- The future `bfl_food_corrections` correction-learning table is referenced in skill docs but not implemented.

## Lesson references

- **#11** (LESSONS_FROM_V1) — register every heartbeat in `HEARTBEATS.md` in the same PR as the supporting scripts; scaffolding without registering = silent dormancy
- **#7 / #20** — gather-first dispatcher; cheap no-op gates short-circuit before any paid API call (the gather script returns `{"count": 0}` when nothing new + nothing orphaned)
- **#13** — destructive shared reads need a cached digest layer; this plugin reads its own Discord channel state, no cross-heartbeat collision
- **installer-1 lesson (FDC default)** — `fdc_enrich` defaults to `false` after installer-1's install revealed FDC has been systematically under-reporting macros (cooked vs raw density mismatch, portion-unit confusion). Claude vision extraction is 5-15x more accurate per meal. FDC key is stashed in VW for future hybrid path (vision-primary + FDC sanity-check) but NOT required at install. Tracked at <v1-reference-install>#513.
- **installer-1 lesson (Strava opt-in)** — `strava_oura_reconcile` defaults to `false`. The previous `true` default forced Ben (a gym-only athlete) to create a Strava OAuth app he will never use. Ask the installer up front: "Do you GPS-track running or cycling?" If no, skip Strava. If yes, flip the flag and provision Strava creds.

## Cross-references

- `plugins/bfl/openclaw.plugin.json` — manifest with full configSchema + contracts
- `plugins/bfl/skills/bfl.md` — full skill body (extraction rules, vocabulary, failure modes)
- `plugins/bfl/db/migrations/001_bfl.sql` — schema
- `plugins/bfl/scheduled-tasks/gather-bfl-photos.sh` — heartbeat gather script
- `plugins/bfl/scheduled-tasks/bfl-ingest-prompt.md` — heartbeat fallback prompt
- `plugins/bfl/triggers/backfill.sh` — Discord trigger handler (stub pending <v1-reference-install>#506)
- `${CHASSIS_HOME}/HEARTBEATS.md` — installer-side heartbeat registry (rendered from `chassis/HEARTBEATS.md.template` at install)
- `chassis.config.yaml modules.bfl` — installer-side activation knob
- `docs/LESSONS_FROM_V1.md` — chassis V1 lessons; relevant lessons referenced inline
