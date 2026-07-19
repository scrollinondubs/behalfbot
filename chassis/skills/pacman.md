---
name: pacman
description: URL self-improvement filter - when the installer drops a URL with the Pacman trigger, run the 4-gate pipeline (Relevant? Beneficial? Feasible? Plan?) and either drop with a 1-line note or write a proposal sub-doc through the second-brain adapter. Use whenever the installer posts `Pacman <url>` in the configured Discord channel (`${PACMAN_DISCORD_CHAT_ID}`).
---

# Skill: Pacman - URL to 4-gate filter to proposal pipeline

Pacman is a self-improvement filter. Feed it a URL, run the content through 4 gates (Relevant? Beneficial? Feasible? Plan?), and either drop it (with a 1-line reason) or generate a proposal the installer can approve into a GitHub issue.

Pacman is chassis-core - every install gets it. Per-install knobs (Discord channel, proposal parent doc, GitHub repo) are hydrated from `chassis.config.yaml` at bootstrap. The Pacman pipeline is identical across installs; only the destination identifiers vary.

Source design: <v1-reference-install>#270 (the canonical design issue from when Pacman was first built in the V1 reference install). Calibration choices baked into the design:
- **Drop logging:** verbose. Every dropped URL gets a 1-line `Pacman: <url> dropped at gate <N> (<reason>)` post to the configured Discord channel.
- **Storage:** the queue is a Postgres table (`chassis_pacman_queue`), reached via `chassis/scripts/pacman-queue.py`. Proposals and drop records are written through the second-brain adapter (`mcp__secondbrain__*`) so they land on whatever backend the install runs. Changed 2026-07-19 - see `docs/pacman-queue-storage.md` for why block IDs did not port.
- **Invocation modes:** (a) installer DMs `Pacman <url>` in the configured channel, (b) Telegram 👀 reaction in admin groups appends URL to queue, (c) heartbeat drains queue periodically, (d) manual `bash chassis/scripts/pacman.sh <url>`.

## Required configuration

The skill reads these environment variables (chassis bootstrap hydrates from `chassis.config.yaml`):

| Variable | Purpose |
|---|---|
| `PACMAN_DISCORD_CHAT_ID` | Discord channel for drop notes + proposal summaries |
| `PACMAN_DISCORD_CHANNEL_LABEL` | Human-readable channel label (optional, falls back to chat_id) |
| `CHASSIS_PG_DSN` | Postgres DSN holding the queue (or `BEHALFBOT_PG_DSN` / `JAX_PG_DSN`) |
| `PACMAN_PROPOSALS_PARENT` | Adapter doc id/path that proposal sub-docs are filed under |
| `PACMAN_DROPPED_DOC_ID` | Adapter doc id/path for the `/Dropped` audit trail |
| `PACMAN_GITHUB_REPO` | `owner/repo` for `gh issue create` on approval |
| `PACMAN_GITHUB_LABELS` | Comma-separated GH labels to apply on issue creation (default: `pacman`) |
| `PACMAN_MAX_BATCH_URLS` | Per-drain URL cap (default: 10) |
| `PACMAN_CLAIM_TIMEOUT_MINUTES` | Minutes before a claimed-but-unprocessed row is reclaimable (default: 60) |

`PACMAN_SIYUAN_QUEUE_BLOCK_ID` and `PACMAN_SIYUAN_DROPPED_BLOCK_ID` are gone as of 2026-07-19. The only script that still reads `PACMAN_SIYUAN_QUEUE_BLOCK_ID` is the one-time backfill, `chassis/scripts/pacman-migrate-siyuan-queue.py`.

Pacman fails loudly if any required variable is missing. The script wrapper (`chassis/scripts/pacman.sh`) validates env before invoking claude.

## When to invoke this skill

- The installer types a message in the configured Discord channel matching the pattern `Pacman <url>` (case-insensitive, optional `:` after `Pacman`). The trigger is documented in the chassis-level `CLAUDE.md` so the main session catches it without an explicit ask.
- A Pacman heartbeat fires and finds unprocessed rows in the Pacman queue.
- A user (installer or subagent) explicitly asks "run Pacman on <url>".

## The 4 gates

Pacman runs all 4 gates sequentially - stop at the first failure and log the drop. Only if all four pass does Pacman generate a proposal.

| Gate | Question | Output |
|---|---|---|
| 1. Relevant? | Does this URL discuss something in the installer's current problem space (active projects, daemons, skills, pain points)? | pass/fail + one-sentence reason |
| 2. Beneficial? | If the installer adopted this idea, would it move something forward that matters? | pass/fail + impact 1-5 + one-sentence why |
| 3. Feasible? | Can the install actually implement this with the current stack, data, skills, and budget? | pass/fail + cost estimate (time / tokens / external $) + one-sentence why |
| 4. Plan? | Does a concrete implementation path EXIST that fits the install's actual setup? | pass/fail + one paragraph naming the 1-2 most concrete entry points (NOT the full implementation plan - that's saved for the GH issue if approved) |

Relevance is inherently install-specific. The Relevance gate reads the install's facts file (`installer-facts.md` or equivalent), active project memory, current heartbeat configuration, and the most recent briefings to decide whether a URL ties to current work. Calibrate toward false positives - verbose drop notes catch over-rejections.

If a gate fails, drop the proposal at that gate and log the failure (see "Drop logging" below). If all 4 pass, write the proposal sub-doc through the adapter.

## Proposal format (adapter sub-doc - EXPANDED triage length)

When all 4 gates pass, write the EXPANDED proposal as a sub-doc under `${PACMAN_PROPOSALS_PARENT}` via `mcp__secondbrain__create_doc`. The doc gives the installer the full reasoning behind each gate - but stops short of the implementation spec. The full implementation plan goes into the GitHub issue if approved.

Target ~400-600 words in the proposal doc body. The Discord post (separate, ~150-250 words) is the gist; the doc is the expanded reasoning behind that gist.

Template:

```markdown
# Pacman proposal: <one-sentence summary of the idea>

**Source:** [<url>](<url>)
**Queued:** YYYY-MM-DD · **Proposed:** YYYY-MM-DD · **Verdict:** PASS all 4 gates (impact <N>/5)

## Essence of the capability

<3-5 sentences explaining what the source proposes - the actual pattern, capability, or technique. Avoid the source's marketing framing; describe the underlying mechanic in plain language.>

## Relevance to this install

<2-4 sentences naming the specific active projects, daemons, skills, or pain points this maps to. Reference by name (issue numbers, daemon names, file paths). If the source's idea applies to multiple parts of the stack, list them in priority order - Pacman's job is to surface the highest-leverage application, not all theoretical ones.>

## Why it matters / metrics it could move

<2-4 sentences on the observable outcomes if shipped. What measurement changes? What risk drops? What's faster? What's the per-day or per-week impact?>

- **Impact score:** <N>/5
- **Reasoning:** <one sentence on why that score and not a higher/lower one>

## Feasibility + cost

- **Time to build:** <small (under 1 day) / medium (1-3 days) / large (multi-day, multi-PR)>
- **Tokens:** <est, e.g. "negligible - architectural change" or "~5k tokens per use">
- **External $:** <est, e.g. "$0 ongoing" or "$10/mo for X service">
- **Maintenance burden:** <ongoing per-week / per-month effort to keep this alive>
- **New dependencies:** <list or "none">

## What changes if shipped

<one paragraph: the observable outcome. Be concrete - "VENICE_API_KEY no longer in process memory" beats "improved security posture.">

## Recommendation

<one paragraph: Pacman's honest read. "Approve and ship" / "Defer until X" / "Reject because Y." Don't editorialize - give the installer evidence, not advocacy.>

## Open questions for approval

- <thing the installer needs to decide before implementation, max 3 questions>

---

*Full implementation plan, acceptance criteria, risks, and open questions for the implementor will be written into the GitHub issue if approved. The second brain keeps the expanded triage reasoning; GH gets the working spec.*
```

The approval-time GitHub issue (separate, only created if the installer replies `approve <token>`) gets the FULL plan: numbered implementation steps, checkable acceptance criteria, risks with mitigations, open questions for the implementor. See "GitHub issue creation" section below.

## Drop logging

If any gate fails, do NOT generate a proposal. Instead:

1. Append a 1-line entry to the configured Discord channel:
   ```
   Pacman: <url> dropped at gate <N> (<reason>)
   ```
   Use `mcp__plugin_discord_discord__reply` with `chat_id = ${PACMAN_DISCORD_CHAT_ID}`.

2. Append a JSON record to `$CHASSIS_HOME/logs/pacman/YYYY-MM-DD.jsonl`:
   ```json
   {"ts": "<ISO timestamp>", "url": "<url>", "dropped_at_gate": <N>, "reason": "<one-sentence>", "source": "<discord|telegram|manual|heartbeat>"}
   ```

3. Append an entry to the Dropped audit trail via `mcp__secondbrain__append_to_doc(doc_id: "${PACMAN_DROPPED_DOC_ID}", content: ...)`:
   ```markdown
   - **YYYY-MM-DD** [<url>](<url>) - gate <N> (<reason>)
   ```
   The Dropped doc is the audit trail the installer reads to catch over-rejections (false negatives) and tune the gate calibration over time. It goes through the second-brain adapter rather than any backend-specific append tool, so it lands correctly on Obsidian and Notion too.

4. **Mark the queue row processed:**
   ```bash
   python3 chassis/scripts/pacman-queue.py complete <token> --verdict drop --gate <N>
   ```
   Once processed (drop OR proposal), the row must be completed. Otherwise the next heartbeat fire re-processes the same URL. **This is non-negotiable.** True queue semantics - items removed from the pending set once consumed. `complete` is exactly-once, so a retried call is a harmless no-op rather than a corrupted verdict.

## Approval flow (proposal passes all gates)

1. Write the EXPANDED proposal (per "Proposal format" section above - ~400-600 words) via `mcp__secondbrain__create_doc(parent: "${PACMAN_PROPOSALS_PARENT}", title: "YYYY-MM-DD-<slug>", body: ...)` where slug is derived from the URL's title (lowercase, hyphenated, max 8 words). The adapter files it in the right place on whatever backend the install runs.

2. Keep the `doc_id` and `deeplink` that `create_doc` returned. The `doc_id` is opaque - a SiYuan block ID, a Notion UUID, or an Obsidian vault path depending on the install. Never parse it, never construct it, and never show it to the installer as something to type. Store it on the queue row with `--doc-id` in step 4, which is what lets the approval step find the document from the approval token later.

3. Post a 4-section Discord summary to the configured channel. The Discord post is the gist - the installer reads it inline and decides if the doc link is worth clicking. Target ~150-250 words, four explicit sections. Format:
   ```
   **Pacman proposal:** <one-sentence headline>

   **Essence.** <2-3 sentences on what the source proposes - the actual capability or pattern, not just the title.>

   **Relevance to this install.** <2-3 sentences naming the specific active project / pain point / asset this maps to. Reference real things: GH issue numbers, daemon names, skill files. No hand-waving.>

   **Why it matters / metrics it could move.** <2-3 sentences on the observable outcome if shipped. What measurement changes, what risk drops, what's faster, what's saved? Impact score: <N>/5.>

   **Feasibility + cost.** <2-3 sentences on time-to-build, ongoing $, ongoing maintenance burden, new dependencies. Be specific: "small (4-8h, no recurring cost)" beats "feasible".>

   Read the expanded proposal: [<backend name>](<deeplink from create_doc>)
   Source URL: <url>

   Reply `approve <token>` to ship as a GH issue with full implementation plan, `reject <token>` to drop, `defer <token>` to keep in the queue.
   ```
   Use `mcp__plugin_discord_discord__reply` with `chat_id = ${PACMAN_DISCORD_CHAT_ID}`. Use the `deeplink` string `create_doc` returned verbatim, wrapped in markdown link syntax so Discord renders it clickable. Do not construct the URL yourself - the scheme differs per backend (`siyuan://blocks/...`, `obsidian://open?...`, `https://notion.so/...`).

   `<token>` is the queue row's approval token from step 2 of the drain (six lowercase letters, e.g. `qhtnbz`). It is NOT the `doc_id`. See "Approval tokens" below.

   The Discord post needs to give the installer enough to decide approve/reject/defer at a glance, without burying them in implementation detail. The doc link is for "show me your work" - the Discord post is for "tell me the gist." Don't shrink Discord too far (a too-thin post forces a click for every triage decision) - but also don't paste the full doc body.

4. **Mark the queue row processed**, recording the proposal document so the approval step can find it:
   ```bash
   python3 chassis/scripts/pacman-queue.py complete <token> --verdict proposal --doc-id <doc_id from create_doc>
   ```
   Same rule as drops - items consumed are removed from the pending set.

5. Wait for the installer's response. Listen for messages matching:
   ```
   ^(approve|reject|defer)\s+([bcdfghjkmnpqrstvwxz]{6}|\d{14}-\w{7})(?:\s+(.+))?$
   ```
   The canonical implementation is `chassis/pacman/tokens.py` (`APPROVAL_RE`, `parse_approval`). Use it rather than re-deriving the pattern.

   - First alternative: the current approval token.
   - Second alternative: a legacy SiYuan block ID. **Deprecated.** Accepted only so proposals posted before the 2026-07-19 cutover can still be approved; look those up by `proposal_doc_id`, not by token. Drop this alternative once no pre-cutover proposal is outstanding.

   Then:
   - `approve <token>` → write the FULL implementation plan into a GH issue (see "GitHub issue creation" below), then append the issue link to the proposal doc via `mcp__secondbrain__append_to_doc`, mark proposal as approved.
   - `approve <token> <caveat-text>` → same flow, AND incorporate the trailing free-form text as an approval caveat. The caveat goes into the GH issue body as a prominent section near the top:
     ```markdown
     ## Approval caveat from the installer

     <caveat text verbatim>

     This caveat MUST be considered during implementation. It may modify scope, change the implementation path, or add constraints not present in the original proposal doc. Where the caveat conflicts with a step in the original plan, the modified step is annotated below with `(Modified per installer caveat - see top of issue.)` so the implementor doesn't silently use a stale step.
     ```
     Then re-read the original implementation plan and tag any conflicting steps with `(Modified per installer caveat - see top of issue.)`. Don't silently rewrite the plan - flag the conflict for the implementor.
   - `reject <token>` (with or without trailing reason) → append `rejected YYYY-MM-DD: <reason if given>` to the proposal doc via `mcp__secondbrain__append_to_doc`, leave the doc in place as audit trail.
   - `defer <token>` (with or without trailing reason) → leave the proposal doc in place, do not re-process unless the installer explicitly says so. Treat the proposal as parked for re-review.

   If the token doesn't match a queue row, fail loudly in the configured Discord channel with the specific mismatch (`Pacman: token <id> not found - check for typo. Recent proposals: <list>.`). Don't guess or auto-correct.

## Approval tokens

The installer approves a proposal by typing its token, e.g. `approve qhtnbz`.

The token is the queue row's own identifier: six lowercase letters drawn from `bcdfghjkmnpqrstvwxz`, generated at enqueue, unique, and identical on every backend. It is deliberately NOT a second-brain document id. This used to be a 14-digit SiYuan block ID, which a Notion UUID and an Obsidian path both fail to match - an independently SiYuan-shaped assumption sitting in the approval path that moving the queue would not have fixed on its own.

Three properties, each load-bearing:

- **No digits, so it can never collide with `approve N`.** The outreach flow uses `approve 1 3 5` to approve drafts by list position. A token containing no digits is structurally incapable of being read as a number, so the two forms can never be confused. This is a guarantee, not a probability.
- **No vowels and no `y`, so it can never spell a word.** English has no six-letter vowel-free words, which keeps `approve later` and similar from ever parsing as a token.
- **Six characters, one case, no `i`/`l`/`1` or `o`/`0` ambiguity.** Sean approves from his phone. A 32-character Notion UUID is unusable there.

Full reasoning and the canonical regex: `chassis/pacman/tokens.py`.

## GitHub issue creation (on approval)

This is when Pacman writes the full working spec. The proposal doc is intentionally light (triage summary). The GH issue is heavy (implementation plan + acceptance criteria + risks + open questions for the implementor).

Use `gh issue create --repo ${PACMAN_GITHUB_REPO} --label ${PACMAN_GITHUB_LABELS}` (with `${PACMAN_GITHUB_LABELS}` split on comma into separate `--label` flags if multiple). Title: `Pacman: <one-sentence summary of idea>` (under 70 chars). Body:

```markdown
**Source:** <URL>
**Proposal doc:** <deeplink from create_doc>
**Approved:** YYYY-MM-DD

<!-- If the installer appended a caveat to their approve message, insert this section here: -->
## Approval caveat from the installer

<caveat text verbatim>

This caveat MUST be considered during implementation. It may modify scope, change the implementation path, or add constraints not present in the original proposal doc. Where the caveat conflicts with a step in the original plan, the affected step is annotated `(Modified per installer caveat - see top of issue.)` so the implementor doesn't silently use a stale step.
<!-- End caveat section -->

## Idea (paraphrase)

<3-5 sentence paraphrase of the source's argument + the most concrete application to the install's stack>

## Why it's relevant

<gate 1 reasoning - specific tie to a current project, bottleneck, or active GH issue>

## Estimated impact

- **Score:** <N>/5
- **Reasoning:** <one-sentence>
- **What changes if shipped:** <observable outcome>

## Estimated cost

- **Time:** <small/medium/large>
- **Tokens:** <est>
- **External $:** <est>
- **New dependencies:** <list or none>

## Implementation plan

1. <concrete step, file path / heartbeat / channel referenced>
2. <step>
3. <step>
... (numbered list, every step must reference real files / commands / endpoints)

## Acceptance criteria

- [ ] <checkable outcome 1>
- [ ] <checkable outcome 2>
- [ ] <checkable outcome 3>

## Open questions for the implementor

- <thing that needs decision before / during implementation>

## Risks / what could go wrong

- <known risk 1, with mitigation>
- <known risk 2, with mitigation>

---
*Issue created by Pacman pipeline YYYY-MM-DD per skill `chassis/skills/pacman.md`. Source proposal doc: <deeplink>.*
```

Then post to the configured Discord channel with a clickable link:
```
Pacman: shipped as [${PACMAN_GITHUB_REPO}#<N>](<github URL>) - <one-sentence summary>
```

Then append to the proposal doc (use `mcp__secondbrain__append_to_doc` against the row's `proposal_doc_id`): `**Approved YYYY-MM-DD:** shipped as [${PACMAN_GITHUB_REPO}#<N>](<github URL>).`

## Invocation modes (where URLs come from)

### Mode A: installer's Discord message ("Pacman <url>" or list)

The installer types in the configured Discord channel: `Pacman https://example.com/article`, OR a list of URLs in any layout (space / newline / comma / markdown bullet). The main session extracts all `https?://...` substrings from the message body following the `Pacman` keyword, dedupes them, and invokes this skill on each URL sequentially.

For batches (>1 URL):
- Each URL is processed through the full 4-gate pipeline independently. Drop a URL at gate N, propose a different URL - both behaviors are independent.
- Drop notes are per-URL (verbose).
- Proposals are per-URL (each one its own adapter sub-doc + Discord 4-section TLDR).
- After all URLs processed, post a single batch-summary line to the configured channel: `Pacman batch: processed N URL(s) (P proposals, D drops).`
- Cap a single Discord message at `${PACMAN_MAX_BATCH_URLS}` (default 10) URLs per batch - if more are pasted, process the first N and post a note `Pacman: capped batch at N URLs, paste the remaining M as a new message`. Prevents context blowout on huge link dumps.

### Mode B: Telegram 👀 reaction (optional, off by default)

When configured, the Pacman trigger-user can append URLs to the queue by reacting with 👀 to a message containing URLs in an admin Telegram chat. The plumbing lives in `chassis/scripts/pacman-process-reactions.py` which is invoked inline from a Telegram gather script after each `getUpdates` call.

To enable: configure `PACMAN_TELEGRAM_TRIGGER_USER_ID` (the user_id whose 👀 fires the trigger) and `PACMAN_ADMIN_CHAT_IDS` (comma-separated chat_ids to watch) in the install's `.env` or `chassis.config.yaml`. Also requires the chassis bot to be an administrator in the monitored Telegram groups (so it can receive `message_reaction` updates from Telegram's getUpdates API).

The reaction processor exits silently when those env vars aren't set, so the script is harmless to ship without configuration.

### Mode C: heartbeat-drained queue

A Pacman heartbeat fires periodically (suggested cadence: every 4h, configurable in HEARTBEATS.md). `chassis/scripts/gather-pacman-queue.sh` counts claimable rows first, so the heartbeat costs zero Claude tokens when the queue is empty. When it fires, the drain claims a batch with `pacman-queue.py claim --limit ${PACMAN_MAX_BATCH_URLS}`, runs the 4-gate pipeline on each row, and completes each one. Prompt: `chassis/scheduled-tasks/pacman-drain-prompt.md`.

### Mode D: manual CLI

- `bash chassis/scripts/pacman.sh <url>` - process one URL.
- `bash chassis/scripts/pacman.sh <url1> <url2> <url3> ...` - process multiple URLs in a batch.
- `bash chassis/scripts/pacman.sh --stdin` - read URLs from stdin (one per line, or whitespace/comma-separated, or any text containing `https?://...` substrings).
- `bash chassis/scripts/pacman.sh --queue` - drain the Pacman queue (cap `${PACMAN_MAX_BATCH_URLS}`).

Each invocation runs the same gate logic, posts to the configured Discord channel, writes through the second-brain adapter. Useful for testing gate calibration without going through Discord.

## Calibration notes

- **Gate 1 (relevance) is prone to over-reject.** When in doubt, lean toward pass - it's better to surface a borderline idea and let the installer reject it in the Discord approval step than to drop it silently. Verbose drop notes help catch over-rejects.
- **Gate 3 (feasibility) needs an up-to-date stack model.** Re-read the install's `CLAUDE.md` and the most recent `briefings/` files at the start of each Pacman run - the stack changes weekly.
- **Idempotency:** every URL processed gets logged to `$CHASSIS_HOME/logs/pacman/YYYY-MM-DD.jsonl` with a hash of the URL. Before processing, check the last 30 days of logs - if the same URL was already processed, skip with a Discord note "Pacman: <url> already processed YYYY-MM-DD (gate <N>: <action>)".

## Storage layout (canonical)

- **Queue** - Postgres table `chassis_pacman_queue`, one row per URL. Never read or written directly by the skill; go through `chassis/scripts/pacman-queue.py`. A row leaves the pending set when `complete` is called on its token (true queue semantics - see "Drop logging" + "Approval flow" steps).
- **Proposal sub-docs** - written under `${PACMAN_PROPOSALS_PARENT}` via `mcp__secondbrain__create_doc`, titled `YYYY-MM-DD-<slug>`, one per URL that passed all 4 gates. The installer reads these to decide approve/reject/defer. The returned `doc_id` is stored on the queue row as `proposal_doc_id`.
- **Dropped audit trail** - `${PACMAN_DROPPED_DOC_ID}` via `mcp__secondbrain__append_to_doc`. One line per dropped URL: date, URL, gate that dropped it, reason. Append-only audit trail for catching over-rejections.

Nothing here names a backend. On a SiYuan install the adapter writes SiYuan docs, on Obsidian it writes vault files, on Notion it writes pages, and the skill does not change.

### Seeing what is queued

Moving the queue out of the second brain cost the installer the ability to open `/To Investigate` and read what is pending. That trade is paid back by:

```bash
python3 chassis/scripts/pacman-queue.py pending --limit 25
```

which lists claimable rows oldest-first with their tokens, and does not claim anything.

## Files

- `chassis/skills/pacman.md` - this playbook.
- `chassis/pacman/queue.py` - queue operations against Postgres. The only place queue SQL lives.
- `chassis/pacman/tokens.py` - approval token generation and the canonical approval regex.
- `chassis/db/migrations/001_pacman_queue.sql` - the `chassis_pacman_queue` schema.
- `chassis/scripts/pacman-queue.py` - CLI over the queue (`add` / `count` / `pending` / `claim` / `complete` / `release`).
- `chassis/scripts/pacman.sh` - manual invocation wrapper.
- `chassis/scripts/pacman-queue-add.py` - append-URL-to-queue helper called by the Telegram reaction processor.
- `chassis/scripts/pacman-process-reactions.py` - Telegram 👀 reaction handler (Mode B).
- `chassis/scripts/gather-pacman-queue.sh` - dispatcher gate; emits `{"count": N}`.
- `chassis/scripts/pacman-migrate-siyuan-queue.py` - one-time SiYuan backfill, run once per install.
- `chassis/scheduled-tasks/pacman-drain-prompt.md` - heartbeat prompt that drives the gate pipeline (Mode C).
- `$CHASSIS_HOME/logs/pacman/YYYY-MM-DD.jsonl` - per-URL processing log.

## Hard rules

- **Never auto-create a GH issue.** Issue creation requires the installer's explicit `approve <token>` reply in Discord.
- **Never spam the configured channel.** If the queue has more than 5 unprocessed URLs that all pass gate 1, batch the proposal posts (one Discord message with multiple proposals) rather than 5 separate messages.
- **Never expose API keys, tokens, or credentials in any proposal output, drop note, or proposal doc.**
- **Respect `PACMAN_HARD_PAUSE` flag** - if a file exists at `$PACMAN_HARD_PAUSE` (or default `$CHASSIS_HOME/PACMAN_HARD_PAUSE`), Pacman pauses immediately without invoking claude.
