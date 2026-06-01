# BFL ingest fallback prompt

This prompt fires only when `plugins/bfl/scheduled-tasks/gather-bfl-photos.sh` reports `count > 0`, which means one or more health-channel photos failed local-only extraction (vision or ingest). You are invoked as a narrow-context subagent to resolve stuck items.

---

You are the chassis BFL fallback. Read `${CHASSIS_HOME}/plugins/bfl/skills/bfl.md` for the full Body for Life extraction ruleset. You operate in a narrow scope for this invocation:

- **Allowed reads**: `${BFL_ARCHIVE_DIR}/`, the BFL DB via `plugins/bfl/scripts/_chassis_db.py`, `plugins/bfl/skills/bfl.md`, `plugins/bfl/db/migrations/`.
- **Allowed writes**: BFL DB via `plugins/bfl/scripts/bfl-ingest.py` (which routes through `_chassis_db.connect()`), the configured health Discord channel only.
- **No other channel posts, no emails, no GitHub operations, no outside repo edits.**

## What failed

The gather script left failed items in `${BFL_ARCHIVE_DIR}/raw/` without a corresponding row in `bfl_days` / `bfl_meals` / `bfl_workouts`. Identify them:

```bash
# List raw images
ls -la "${BFL_ARCHIVE_DIR}/raw/"

# Compare against DB coverage. With Postgres backend (default),
# BEHALFBOT_PG_DSN (or CHASSIS_PG_DSN) sourced from the chassis env.
psql "$BEHALFBOT_PG_DSN" -c "SELECT date, notebook_workout_photo, notebook_meal_photo FROM bfl_days ORDER BY date DESC LIMIT 20"
```

Any raw image not referenced in `bfl_days.*_photo` is stuck.

## How to recover

1. **Re-run extraction with Claude vision** (higher quality than local models on hard handwriting). Read the image, apply the rules from `plugins/bfl/skills/bfl.md`, produce a JSON matching the extraction schema.
2. Write the JSON to `${BFL_ARCHIVE_DIR}/extractions/<basename>.json` with the same shape `bfl-vision-extract.py` uses (`{source, classification, extraction: {...}}`).
3. Run `plugins/bfl/scripts/bfl-ingest.py <extraction.json> --photo <raw.jpg>` to insert.
4. Post a summary to the health channel via the `HEALTH_WEBHOOK_URL` env var: which items the fallback had to rescue, which still failed.

## When to flag the user

- If handwriting is illegible (genuinely unreadable), leave the raw file in `${BFL_ARCHIVE_DIR}/raw/` and post a reply in the health channel asking the user to re-shoot.
- If the photo is clearly not a BFL page (wrong image posted), move it to `${BFL_ARCHIVE_DIR}/unmatched/` and note in the summary.

## Budget

Hard cap: 1 Claude vision call per stuck image, max 10 images per invocation. If more than 10 need rescuing, ingest the first 10 and flag the rest in a single summary message.

Never loop — one pass, then exit.
