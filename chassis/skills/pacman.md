---
name: pacman
description: URL self-improvement filter — when the installer drops a URL with the Pacman trigger, run the 4-gate pipeline (Relevant? Beneficial? Feasible? Plan?) and either drop with a 1-line note or write a SiYuan proposal sub-doc. Use whenever the installer posts `Pacman <url>` in the configured Discord channel (`${PACMAN_DISCORD_CHAT_ID}`).
---

# Skill: Pacman — URL → 4-gate filter → proposal pipeline

Pacman is a self-improvement filter. Feed it a URL, run the content through 4 gates (Relevant? Beneficial? Feasible? Plan?), and either drop it (with a 1-line reason) or generate a proposal the installer can approve into a GitHub issue.

Pacman is chassis-core — every install gets it. Per-install knobs (Discord channel, SiYuan queue block, GitHub repo) are hydrated from `chassis.config.yaml` at bootstrap. The Pacman pipeline is identical across installs; only the destination identifiers vary.

Source design: <v1-reference-install>#270 (the canonical design issue from when Pacman was first built in the V1 reference install). Calibration choices baked into the design:
- **Drop logging:** verbose. Every dropped URL gets a 1-line `Pacman: <url> dropped at gate <N> (<reason>)` post to the configured Discord channel.
- **Storage:** SiYuan doc `/To Investigate` (block ID `${PACMAN_SIYUAN_QUEUE_BLOCK_ID}`) is the queue + proposal home. Proposals are written as sub-docs of that node.
- **Invocation modes:** (a) installer DMs `Pacman <url>` in the configured channel, (b) Telegram 👀 reaction in admin groups appends URL to queue, (c) heartbeat drains queue periodically, (d) manual `bash chassis/scripts/pacman.sh <url>`.

## Required configuration

The skill reads these environment variables (chassis bootstrap hydrates from `chassis.config.yaml`):

| Variable | Purpose |
|---|---|
| `PACMAN_DISCORD_CHAT_ID` | Discord channel for drop notes + proposal summaries |
| `PACMAN_DISCORD_CHANNEL_LABEL` | Human-readable channel label (optional, falls back to chat_id) |
| `PACMAN_SIYUAN_QUEUE_BLOCK_ID` | SiYuan parent block for the `/To Investigate` queue |
| `PACMAN_SIYUAN_DROPPED_BLOCK_ID` | SiYuan parent block for the `/Dropped` audit-trail sub-doc |
| `PACMAN_GITHUB_REPO` | `owner/repo` for `gh issue create` on approval |
| `PACMAN_GITHUB_LABELS` | Comma-separated GH labels to apply on issue creation (default: `pacman`) |

Pacman fails loudly if any required variable is missing. The script wrapper (`chassis/scripts/pacman.sh`) validates env before invoking claude.

## When to invoke this skill

- The installer types a message in the configured Discord channel matching the pattern `Pacman <url>` (case-insensitive, optional `:` after `Pacman`). The trigger is documented in the chassis-level `CLAUDE.md` so the main session catches it without an explicit ask.
- A Pacman heartbeat fires and finds queued URLs in the SiYuan `/To Investigate` doc that haven't been processed yet.
- A user (installer or subagent) explicitly asks "run Pacman on <url>".

## The 4 gates

Pacman runs all 4 gates sequentially — stop at the first failure and log the drop. Only if all four pass does Pacman generate a proposal.

| Gate | Question | Output |
|---|---|---|
| 1. Relevant? | Does this URL discuss something in the installer's current problem space (active projects, daemons, skills, pain points)? | pass/fail + one-sentence reason |
| 2. Beneficial? | If the installer adopted this idea, would it move something forward that matters? | pass/fail + impact 1-5 + one-sentence why |
| 3. Feasible? | Can the install actually implement this with the current stack, data, skills, and budget? | pass/fail + cost estimate (time / tokens / external $) + one-sentence why |
| 4. Plan? | Does a concrete implementation path EXIST that fits the install's actual setup? | pass/fail + one paragraph naming the 1-2 most concrete entry points (NOT the full implementation plan — that's saved for the GH issue if approved) |

Relevance is inherently install-specific. The Relevance gate reads the install's facts file (`installer-facts.md` or equivalent), active project memory, current heartbeat configuration, and the most recent briefings to decide whether a URL ties to current work. Calibrate toward false positives — verbose drop notes catch over-rejections.

If a gate fails, drop the proposal at that gate and log the failure (see "Drop logging" below). If all 4 pass, write the SiYuan proposal sub-doc.

## Proposal format (SiYuan sub-doc — EXPANDED triage length)

When all 4 gates pass, write the EXPANDED proposal as a SiYuan sub-doc under `/To Investigate`. The SiYuan doc gives the installer the full reasoning behind each gate — but stops short of the implementation spec. The full implementation plan goes into the GitHub issue if approved.

Target ~400-600 words in the SiYuan doc body. The Discord post (separate, ~150-250 words) is the gist; SiYuan is the expanded reasoning behind that gist.

Template:

```markdown
# Pacman proposal: <one-sentence summary of the idea>

**Source:** [<url>](<url>)
**Queued:** YYYY-MM-DD · **Proposed:** YYYY-MM-DD · **Verdict:** PASS all 4 gates (impact <N>/5)

## Essence of the capability

<3-5 sentences explaining what the source proposes — the actual pattern, capability, or technique. Avoid the source's marketing framing; describe the underlying mechanic in plain language.>

## Relevance to this install

<2-4 sentences naming the specific active projects, daemons, skills, or pain points this maps to. Reference by name (issue numbers, daemon names, file paths). If the source's idea applies to multiple parts of the stack, list them in priority order — Pacman's job is to surface the highest-leverage application, not all theoretical ones.>

## Why it matters / metrics it could move

<2-4 sentences on the observable outcomes if shipped. What measurement changes? What risk drops? What's faster? What's the per-day or per-week impact?>

- **Impact score:** <N>/5
- **Reasoning:** <one sentence on why that score and not a higher/lower one>

## Feasibility + cost

- **Time to build:** <small (under 1 day) / medium (1-3 days) / large (multi-day, multi-PR)>
- **Tokens:** <est, e.g. "negligible — architectural change" or "~5k tokens per use">
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

*Full implementation plan, acceptance criteria, risks, and open questions for the implementor will be written into the GitHub issue if approved. SiYuan keeps the expanded triage reasoning; GH gets the working spec.*
```

The approval-time GitHub issue (separate, only created if the installer replies `approve <block-id>`) gets the FULL plan: numbered implementation steps, checkable acceptance criteria, risks with mitigations, open questions for the implementor. See "GitHub issue creation" section below.

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

3. Append an entry to the SiYuan `/To Investigate/Dropped` sub-doc (block ID `${PACMAN_SIYUAN_DROPPED_BLOCK_ID}`) using `mcp__siyuan__append_block`:
   ```markdown
   - **YYYY-MM-DD** [<url>](<url>) - gate <N> (<reason>)
   ```
   The Dropped sub-doc is the audit trail the installer reads to catch over-rejections (false negatives) and tune the gate calibration over time.

4. **Remove the source URL block from the queue** (the `/To Investigate` parent doc, block ID `${PACMAN_SIYUAN_QUEUE_BLOCK_ID}`). Pacman processes URLs as queue entries — once processed (drop OR proposal), the source block must be deleted via `mcp__siyuan__delete_block`. Otherwise the next heartbeat fire re-processes the same URL. **This is non-negotiable.** True queue semantics — items removed once consumed.

## Approval flow (proposal passes all gates)

1. Write the EXPANDED proposal (per "Proposal format" section above — ~400-600 words) to a new SiYuan sub-doc under `/To Investigate` via `mcp__siyuan__create_doc`. Path: `/To Investigate/YYYY-MM-DD-<slug>` where slug is derived from the URL's title (lowercase, hyphenated, max 8 words).

2. Get the new sub-doc's block ID from `mcp__siyuan__sql_query`: `SELECT id FROM blocks WHERE hpath = '/To Investigate/YYYY-MM-DD-<slug>' AND type = 'd' LIMIT 1`.

3. Post a 4-section Discord summary to the configured channel. The Discord post is the gist — the installer reads it inline and decides if the SiYuan link is worth clicking. Target ~150-250 words, four explicit sections. Format:
   ```
   **Pacman proposal:** <one-sentence headline>

   **Essence.** <2-3 sentences on what the source proposes - the actual capability or pattern, not just the title.>

   **Relevance to this install.** <2-3 sentences naming the specific active project / pain point / asset this maps to. Reference real things: GH issue numbers, daemon names, skill files. No hand-waving.>

   **Why it matters / metrics it could move.** <2-3 sentences on the observable outcome if shipped. What measurement changes, what risk drops, what's faster, what's saved? Impact score: <N>/5.>

   **Feasibility + cost.** <2-3 sentences on time-to-build, ongoing $, ongoing maintenance burden, new dependencies. Be specific: "small (4-8h, no recurring cost)" beats "feasible".>

   Read the expanded proposal: [SiYuan sub-doc](siyuan://blocks/<block-id>)
   Source URL: <url>

   Reply `approve <block-id>` to ship as a GH issue with full implementation plan, `reject <block-id>` to drop, `defer <block-id>` to keep in the queue.
   ```
   Use `mcp__plugin_discord_discord__reply` with `chat_id = ${PACMAN_DISCORD_CHAT_ID}`. The `siyuan://blocks/<block-id>` URL must be wrapped in markdown link syntax `[text](siyuan://blocks/<block-id>)` so Discord renders it as a clickable link that opens the SiYuan desktop app via custom protocol handler.

   The Discord post needs to give the installer enough to decide approve/reject/defer at a glance, without burying them in implementation detail. The SiYuan link is for "show me your work" — the Discord post is for "tell me the gist." Don't shrink Discord too far (a too-thin post forces a click for every triage decision) — but also don't paste the full SiYuan body.

4. **Remove the source URL block from the queue** (block in `/To Investigate` doc that contained the URL). Use `mcp__siyuan__delete_block`. Same rule as drops — items consumed are removed.

5. Wait for the installer's response. Listen for messages matching `^(approve|reject|defer)\s+(\d{14}-\w{7})(?:\s+(.+))?$`:
   - `approve <block-id>` → write the FULL implementation plan into a GH issue (see "GitHub issue creation" below), then comment in the SiYuan sub-doc with the issue link, mark proposal as approved.
   - `approve <block-id> <caveat-text>` → same flow, AND incorporate the trailing free-form text as an approval caveat. The caveat goes into the GH issue body as a prominent section near the top:
     ```markdown
     ## Approval caveat from the installer

     <caveat text verbatim>

     This caveat MUST be considered during implementation. It may modify scope, change the implementation path, or add constraints not present in the original SiYuan proposal. Where the caveat conflicts with a step in the original plan, the modified step is annotated below with `(Modified per installer caveat - see top of issue.)` so the implementor doesn't silently use a stale step.
     ```
     Then re-read the original implementation plan and tag any conflicting steps with `(Modified per installer caveat - see top of issue.)`. Don't silently rewrite the plan — flag the conflict for the implementor.
   - `reject <block-id>` (with or without trailing reason) → append `rejected YYYY-MM-DD: <reason if given>` line to the SiYuan sub-doc, leave the doc in place as audit trail.
   - `defer <block-id>` (with or without trailing reason) → leave the SiYuan sub-doc in place, do not re-process unless the installer explicitly says so. Treat the proposal as parked for re-review.

   If the block ID doesn't match an existing SiYuan proposal sub-doc, fail loudly in the configured Discord channel with the specific ID mismatch (`Pacman: block ID <id> not found - check for typo. Existing proposals: <list>.`). Don't guess or auto-correct.

## GitHub issue creation (on approval)

This is when Pacman writes the full working spec. The SiYuan proposal is intentionally light (triage summary). The GH issue is heavy (implementation plan + acceptance criteria + risks + open questions for the implementor).

Use `gh issue create --repo ${PACMAN_GITHUB_REPO} --label ${PACMAN_GITHUB_LABELS}` (with `${PACMAN_GITHUB_LABELS}` split on comma into separate `--label` flags if multiple). Title: `Pacman: <one-sentence summary of idea>` (under 70 chars). Body:

```markdown
**Source:** <URL>
**SiYuan proposal:** siyuan://blocks/<block-id>
**Approved:** YYYY-MM-DD

<!-- If the installer appended a caveat to their approve message, insert this section here: -->
## Approval caveat from the installer

<caveat text verbatim>

This caveat MUST be considered during implementation. It may modify scope, change the implementation path, or add constraints not present in the original SiYuan proposal. Where the caveat conflicts with a step in the original plan, the affected step is annotated `(Modified per installer caveat - see top of issue.)` so the implementor doesn't silently use a stale step.
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
*Issue created by Pacman pipeline YYYY-MM-DD per skill `chassis/skills/pacman.md`. Source SiYuan proposal: siyuan://blocks/<block-id>.*
```

Then post to the configured Discord channel with a clickable link:
```
Pacman: shipped as [${PACMAN_GITHUB_REPO}#<N>](<github URL>) - <one-sentence summary>
```

Then comment in the SiYuan sub-doc (use `mcp__siyuan__append_block` against the sub-doc's block ID): `**Approved YYYY-MM-DD:** shipped as [${PACMAN_GITHUB_REPO}#<N>](<github URL>).`

## Invocation modes (where URLs come from)

### Mode A: installer's Discord message ("Pacman <url>" or list)

The installer types in the configured Discord channel: `Pacman https://example.com/article`, OR a list of URLs in any layout (space / newline / comma / markdown bullet). The main session extracts all `https?://...` substrings from the message body following the `Pacman` keyword, dedupes them, and invokes this skill on each URL sequentially.

For batches (>1 URL):
- Each URL is processed through the full 4-gate pipeline independently. Drop a URL at gate N, propose a different URL — both behaviors are independent.
- Drop notes are per-URL (verbose).
- Proposals are per-URL (each one its own SiYuan sub-doc + Discord 4-section TLDR).
- After all URLs processed, post a single batch-summary line to the configured channel: `Pacman batch: processed N URL(s) (P proposals, D drops).`
- Cap a single Discord message at `${PACMAN_MAX_BATCH_URLS}` (default 10) URLs per batch — if more are pasted, process the first N and post a note `Pacman: capped batch at N URLs, paste the remaining M as a new message`. Prevents context blowout on huge link dumps.

### Mode B: Telegram 👀 reaction (optional, off by default)

When configured, the Pacman trigger-user can append URLs to the queue by reacting with 👀 to a message containing URLs in an admin Telegram chat. The plumbing lives in `chassis/scripts/pacman-process-reactions.py` which is invoked inline from a Telegram gather script after each `getUpdates` call.

To enable: configure `PACMAN_TELEGRAM_TRIGGER_USER_ID` (the user_id whose 👀 fires the trigger) and `PACMAN_ADMIN_CHAT_IDS` (comma-separated chat_ids to watch) in the install's `.env` or `chassis.config.yaml`. Also requires the chassis bot to be an administrator in the monitored Telegram groups (so it can receive `message_reaction` updates from Telegram's getUpdates API).

The reaction processor exits silently when those env vars aren't set, so the script is harmless to ship without configuration.

### Mode C: heartbeat-drained queue

A Pacman heartbeat fires periodically (suggested cadence: every 4h, configurable in HEARTBEATS.md). It reads new sub-docs of `/To Investigate` that haven't been processed (no `pacman_processed` attribute set), runs the 4-gate pipeline on each, and either drops or proposes. Cap the batch at `${PACMAN_MAX_BATCH_URLS}` URLs per fire to bound token cost.

### Mode D: manual CLI

- `bash chassis/scripts/pacman.sh <url>` — process one URL.
- `bash chassis/scripts/pacman.sh <url1> <url2> <url3> ...` — process multiple URLs in a batch.
- `bash chassis/scripts/pacman.sh --stdin` — read URLs from stdin (one per line, or whitespace/comma-separated, or any text containing `https?://...` substrings).
- `bash chassis/scripts/pacman.sh --queue` — drain the SiYuan `/To Investigate` queue (cap `${PACMAN_MAX_BATCH_URLS}`).

Each invocation runs the same gate logic, posts to the configured Discord channel, writes to SiYuan. Useful for testing gate calibration without going through Discord.

## Calibration notes

- **Gate 1 (relevance) is prone to over-reject.** When in doubt, lean toward pass — it's better to surface a borderline idea and let the installer reject it in the Discord approval step than to drop it silently. Verbose drop notes help catch over-rejects.
- **Gate 3 (feasibility) needs an up-to-date stack model.** Re-read the install's `CLAUDE.md` and the most recent `briefings/` files at the start of each Pacman run — the stack changes weekly.
- **Idempotency:** every URL processed gets logged to `$CHASSIS_HOME/logs/pacman/YYYY-MM-DD.jsonl` with a hash of the URL. Before processing, check the last 30 days of logs — if the same URL was already processed, skip with a Discord note "Pacman: <url> already processed YYYY-MM-DD (gate <N>: <action>)".

## SiYuan structure (canonical)

- **Queue doc** — `/To Investigate` (block ID `${PACMAN_SIYUAN_QUEUE_BLOCK_ID}`). URLs waiting to be processed live here as paragraph or heading blocks. After processing, the block is deleted (true queue semantics — see "Drop logging" + "Approval flow" steps).
- **Proposal sub-docs** — `/To Investigate/YYYY-MM-DD-<slug>` for each URL that passed all 4 gates. Short triage summaries (under 250 words). The installer reads these to decide approve/reject/defer.
- **Dropped audit trail** — `/To Investigate/Dropped` (block ID `${PACMAN_SIYUAN_DROPPED_BLOCK_ID}`). Pacman appends a 1-line entry per dropped URL: date, URL, gate that dropped it, reason. Append-only audit trail for catching over-rejections.

## Files

- `chassis/skills/pacman.md` — this playbook.
- `chassis/scripts/pacman.sh` — manual invocation wrapper.
- `chassis/scripts/pacman-queue-add.py` — append-URL-to-queue helper called by Telegram reaction processor.
- `chassis/scripts/pacman-process-reactions.py` — Telegram 👀 reaction handler (Mode B).
- `chassis/scheduled-tasks/pacman-drain-prompt.md` — heartbeat prompt that drives the gate pipeline (Mode C).
- `$CHASSIS_HOME/logs/pacman/YYYY-MM-DD.jsonl` — per-URL processing log.

## Hard rules

- **Never auto-create a GH issue.** Issue creation requires the installer's explicit `approve <block-id>` reply in Discord.
- **Never spam the configured channel.** If the queue has more than 5 unprocessed URLs that all pass gate 1, batch the proposal posts (one Discord message with multiple proposals) rather than 5 separate messages.
- **Never expose API keys, tokens, or credentials in any proposal output, drop note, or SiYuan doc.**
- **Respect `PACMAN_HARD_PAUSE` flag** — if a file exists at `$PACMAN_HARD_PAUSE` (or default `$CHASSIS_HOME/PACMAN_HARD_PAUSE`), Pacman pauses immediately without invoking claude.
