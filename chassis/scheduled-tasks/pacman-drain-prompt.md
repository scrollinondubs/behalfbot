# Pacman queue drain

The `/To Investigate` queue (SiYuan block `${PACMAN_SIYUAN_QUEUE_BLOCK_ID}`) has unprocessed URL blocks. Drain them via the Pacman 4-gate pipeline.

## Backend support - read this before running

This prompt is only PARTLY backend-neutral, and the split is deliberate rather than an oversight.

- **Queue storage is SiYuan-only.** Pacman stores its queue as SiYuan blocks and uses block IDs as queue-entry handles. Notion and Obsidian have no equivalent - see `docs/pacman-queue-storage.md` for why block IDs do not port and what replaces them. Steps 2 and 4 below therefore still name `mcp__siyuan__*`, and they are the only steps that do.
- **Everything Pacman writes is backend-neutral.** Proposals and drop records go through `mcp__secondbrain__*` (steps 5 and 6), so they land correctly on whatever backend the install runs.

Concretely: on a Notion or Obsidian install, and on ANY install running `second_brain.mode: adapter` (where `mcp__siyuan__*` is not registered at all), the queue steps cannot run. If `mcp__siyuan__sql_query` is unavailable, do NOT improvise a substitute and do NOT report a clean drain. Print

```
ERROR: pacman queue drain requires the siyuan MCP server, which is not available on this install. See docs/pacman-queue-storage.md.
```

to stdout and exit non-zero so the dispatcher alerts. Silently draining nothing is the failure mode this whole Stage 2 pass exists to remove.

## Steps

1. **Read `chassis/skills/pacman.md` in full** before doing anything else. The skill defines the 4 gates, drop logging, proposal format, approval flow, and queue management. Follow it exactly.

2. **Read the queue.** SiYuan-only, per the backend note above. Use `mcp__siyuan__sql_query` to list all blocks under `/To Investigate` that contain a URL:
   ```sql
   SELECT id, content FROM blocks
   WHERE root_id = '${PACMAN_SIYUAN_QUEUE_BLOCK_ID}'
     AND id != '${PACMAN_SIYUAN_QUEUE_BLOCK_ID}'
     AND type IN ('h', 'p', 'l', 'i')
     AND content LIKE '%http%'
   ORDER BY created ASC
   LIMIT ${PACMAN_MAX_BATCH_URLS}
   ```
   Cap at `${PACMAN_MAX_BATCH_URLS}` URLs per fire (default 10). The `created ASC` order means oldest queued URL is processed first (FIFO).

3. **For each block:** extract the URL(s) from the `content` field. A block may contain one or more URLs (use a regex match on `https?://...`). Process each URL through the 4 gates per `chassis/skills/pacman.md`.

4. **After processing each URL** (drop OR proposal - verdict regardless): **delete the source block** from `/To Investigate` via `mcp__siyuan__delete_block` (SiYuan-only, per the backend note above). Non-negotiable rule per the skill - true queue semantics. If a single block had multiple URLs, delete the block only after ALL URLs from it have been processed.

5. **Drops** post a 1-line note to the configured Discord channel (`chat_id: ${PACMAN_DISCORD_CHAT_ID}`) AND append a row to the Dropped archive via `mcp__secondbrain__append_to_doc(doc_id: "${PACMAN_DROPPED_DOC_ID}", content: ...)`. Format per the skill.

6. **Proposals** write a sub-doc via `mcp__secondbrain__create_doc(parent: "${PACMAN_PROPOSALS_PARENT}", title: "YYYY-MM-DD-<slug>", body: ...)` AND post a 4-section TLDR to the configured Discord channel. Use the `deeplink` that `create_doc` returns - do not construct a URL yourself, the format differs per backend. Format per the skill.

7. **End-of-batch summary** to the configured Discord channel:
   ```
   Pacman drain: processed N URLs (P proposals, D drops). Next drain in ~4h.
   ```
   Skip the summary if N=0 (no work, no noise).

## Hard rules

- Cap `${PACMAN_MAX_BATCH_URLS}` URLs per fire. If queue has more, leave them - the next drain picks them up.
- Process oldest-first (FIFO).
- Always delete the source block after processing, even on drops. Otherwise the next drain re-processes the same URLs.
- Respect `PACMAN_HARD_PAUSE` - if the file exists at `$CHASSIS_HOME/PACMAN_HARD_PAUSE`, post a one-liner to the configured channel confirming the pause and exit without processing.
- Don't editorialize in proposal recommendations beyond what the skill specifies. Pacman surfaces evidence; the installer decides.
- If a URL can't be fetched (404, timeout, paywall, etc.), drop it at gate 0 with reason "fetch failed" and a 1-line note. Don't retry within the same fire - the next drain will re-encounter the URL only if the source block wasn't deleted, which it should have been.

## Logging

Every URL processed (drop OR proposal OR fetch-failure) gets a JSONL entry in `$CHASSIS_HOME/logs/pacman/YYYY-MM-DD.jsonl`:
```json
{"ts": "...", "url": "...", "verdict": "drop|proposal|fetch_failed", "gate": 0|1|2|3|4|null, "doc_id": "...", "source": "telegram|discord|manual|heartbeat-drain"}
```
