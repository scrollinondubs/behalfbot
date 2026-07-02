# daily-log heartbeat

Nightly synthesis of every operational surface Jax touched over the past 24
hours. Produces a structured markdown log under SiYuan that the morning
briefing consumes for context.

Ships in chassis as of PR
`feat(daily-log): multi-surface gather + prompt restructure`.

## What it does

- Runs nightly at 02:00 (customer install can adjust in HEARTBEATS.md).
- `chassis/scripts/daily-log-gather.py` pre-fetches the day's activity from
  four surfaces: GitHub, Gmail, SiYuan, Discord postmortem mining.
- The dispatcher passes that JSON to
  `${CUSTOMER_HOME}/scheduled-tasks/daily-log-prompt.md`, which synthesizes
  the log and writes it to SiYuan under a customer-configured parent block.

## Surfaces scanned

### GitHub (dynamic repo discovery)

Uses `gh api graphql` to list every repo the `DAILY_LOG_GH_USER` viewer has
push access to, ordered by recent push. Any repo pushed in the last 14
days gets its PRs + open issues scanned. This is deliberately dynamic:
on the VCL platform, customers will have students assigning tasks on
their own repos, and the daily log needs to capture that activity without
a hardcoded org allowlist.

For each active repo:

- PRs merged / opened / closed-unmerged in the 24h window by
  `DAILY_LOG_GH_USER`
- Open issues where Jax has commented (candidate "awaiting input")

### Gmail

Only invoked when `DAILY_LOG_GMAIL_IDENTITY` is set. The current chassis
gather returns `gmail_scan_deferred: true` because Gmail-API auth is
customer-side; the prompt performs the actual search via the Gmail MCP
using the identity as a filter.

### SiYuan

Queries the blocks table via the HTTP API for any doc-type block updated
inside the 24h window with content length > 200 chars (filters out empty
auto-created docs). For docs created (not just updated), also pulls the
first 300 chars of first-paragraph content as a hint for the prompt.

### Discord postmortem mining

Fetches the last 100 messages from `DAILY_LOG_DISCORD_CHANNEL_ID` via the
Discord HTTP API. Regex-filters bot-authored messages against a fixed
pattern set that captures Jax's usual postmortem shape:

- `Surprises:` / `Sanity-check priorities:` / `Review priorities:`
- `deviated from spec` / `didn't work` / `broke because`
- `Root cause:` / `Gotcha:` / `Learned:`

Each match becomes a `{source, timestamp, excerpt}` object for the prompt
to turn into `Learnings & Tribal Knowledge` bullets.

## Env var contract

| Var | Required | Purpose |
|---|---|---|
| `CHASSIS_HOME` | yes | Customer install root (already set by dispatcher) |
| `DAILY_LOG_GH_USER` | recommended | GitHub username Jax pushes as (e.g. `jacketyjax`). If unset, GitHub scan is skipped with a warning. |
| `DAILY_LOG_DISCORD_CHANNEL_ID` | recommended | The `#jax` channel ID for postmortem mining. If unset, Discord scan is skipped. |
| `DAILY_LOG_GMAIL_IDENTITY` | optional | Gmail address Jax sends from (e.g. `jax@vibecodelisboa.com`). Enables prompt-side Gmail MCP search. |
| `DAILY_LOG_SIYUAN_URL` | optional | SiYuan HTTP API base URL. Falls back to `SIYUAN_URL`. |
| `DAILY_LOG_SIYUAN_TOKEN` | optional | SiYuan API token. Falls back to `SIYUAN_TOKEN`. |
| `DAILY_LOG_EXTRA_METRICS_SCRIPT` | optional | Path to a customer-side executable that emits extra metrics as JSON. Chassis calls it and merges output under `metrics.custom`. |
| `DISCORD_TOKEN` or `DISCORD_BOT_TOKEN` | needed for Discord | Discord bot token. Either name is accepted. |

Any missing env var degrades gracefully: the corresponding surface is
skipped, a note is added to `warnings`, and the rest of the gather runs.
The gather never crashes.

## Customer-install setup

1. Copy the prompt template into the customer install:

   ```bash
   cp "${CHASSIS_HOME}/chassis/scheduled-tasks/daily-log-prompt.md.template" \
      "${CUSTOMER_HOME}/scheduled-tasks/daily-log-prompt.md"
   ```

2. Fill in the four `{{ ... }}` placeholders in the copied prompt:

   - `{{ CUSTOMER_JAX_IDENTITY }}` - `"Sean Tierney's autonomous assistant"`, etc.
   - `{{ CUSTOMER_ORG_HANDLES }}` - space-separated org/domain handles
     for the Gmail `in:sent from:` filter
   - `{{ CUSTOMER_DAILY_LOG_PARENT_ID }}` - SiYuan block ID of the parent
     doc under which daily logs are written
   - `{{ CUSTOMER_DAILY_LOG_NOTEBOOK_ID }}` - SiYuan notebook (box) ID
     hosting that parent

3. Register the heartbeat in `${CUSTOMER_HOME}/HEARTBEATS.md`:

   ```yaml
   ## daily-log

   schedule: daily 02:00
   gather: ${CHASSIS_HOME}/chassis/scripts/daily-log-gather.py
   condition: always
   prompt: ${CUSTOMER_HOME}/scheduled-tasks/daily-log-prompt.md
   model: sonnet
   budget: 2
   criticality: normal
   output_validator: true
   ```

4. Set the env vars in `${CUSTOMER_HOME}/.env` (or the bake step that
   sources into the dispatcher context):

   ```bash
   DAILY_LOG_GH_USER=your-gh-username
   DAILY_LOG_GMAIL_IDENTITY=jax@your-domain.com
   DAILY_LOG_DISCORD_CHANNEL_ID=1234567890123456789
   ```

5. Smoke-test the gather standalone:

   ```bash
   CHASSIS_HOME=$HOME/.behalfbot \
   DAILY_LOG_GH_USER=your-gh-username \
     "${CHASSIS_HOME}/chassis/scripts/daily-log-gather.py" --verbose | jq .
   ```

   Expected: a valid JSON payload with populated `prs_by_repo` and
   `metrics`, and warnings for whichever surfaces you haven't configured
   yet.

## Extending metrics per install

`DAILY_LOG_EXTRA_METRICS_SCRIPT` lets you add install-specific metrics
without touching chassis. The script should emit a single JSON object on
stdout; chassis merges it under `metrics.custom`.

Example for Sean's install (adds dating funnel + outreach counts):

```bash
#!/bin/bash
# scripts/daily-log-extra-metrics.sh
# Emits Sean-install-specific metrics for the daily-log gather.

set -euo pipefail
: "${CHASSIS_HOME:?CHASSIS_HOME must be set}"

YESTERDAY=$(date -v-1d +%Y-%m-%d 2>/dev/null || date -d 'yesterday' +%Y-%m-%d)

dating_swipes=$(grep -h '"action":"swipe"' \
    "${CHASSIS_HOME}/logs/dating/${YESTERDAY}-"*.jsonl 2>/dev/null | wc -l | tr -d ' ')
outreach_sent=$(sqlite3 "${CHASSIS_HOME}/state/outreach.db" \
    "SELECT COUNT(*) FROM email_outreach_log WHERE date(sent_at) = '${YESTERDAY}'" \
    2>/dev/null || echo 0)

jq -n \
    --argjson swipes "${dating_swipes:-0}" \
    --argjson outreach "${outreach_sent:-0}" \
    '{dating_swipes: $swipes, outreach_sent: $outreach}'
```

Then in `.env`:

```bash
DAILY_LOG_EXTRA_METRICS_SCRIPT=/Users/jax/.behalfbot/scripts/daily-log-extra-metrics.sh
```

## Design decisions

- **Dynamic repo discovery** over a static allowlist. Rationale: on the VCL
  platform, students will start assigning tasks to Jax on their own project
  repos. Hardcoding the customer's own org would silently drop that
  activity.
- **Postmortem mining from Discord** over an every-subagent-writes log
  pipeline. Rationale: postmortems already surface in `#jax` when Jax
  reports back on PRs and heartbeats. Adding a parallel log write in every
  subagent is deferred until that first path proves insufficient.
- **Reflection header stays even when empty.** Rationale: consistency
  across daily logs makes the SiYuan tree scannable at a glance. An empty
  body communicates "nothing to reflect on" more clearly than a missing
  header.
- **Gmail scan is deferred to the prompt** for now. The gather returns a
  flag; the prompt runs the actual Gmail MCP search. Rationale: no
  chassis-side Gmail auth path exists yet, and adding one requires per-
  customer OAuth wiring that's out of scope for this PR.

## Related files

- `chassis/scripts/daily-log-gather.py`
- `chassis/scheduled-tasks/daily-log-prompt.md.template`
- `chassis/HEARTBEATS.md.template` (documents the recommended wiring)
- `chassis/scripts/test-daily-log-gather.sh` (smoke test)
