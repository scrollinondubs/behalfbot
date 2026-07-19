# Pacman queue drain

The Pacman queue has unprocessed URLs. Drain them via the 4-gate pipeline.

## Backend support

This prompt is backend-neutral end to end, as of the queue move to Postgres
(2026-07-19, `docs/pacman-queue-storage.md`). It runs identically on SiYuan,
Obsidian, Notion, and on an install running `second_brain.mode: adapter`.

- **Queue storage is Postgres**, reached through `chassis/scripts/pacman-queue.py`.
  No SiYuan MCP tool is named anywhere in this prompt. That was the defect: the
  queue used SiYuan block IDs as handles, so on adapter-mode installs - where
  the native SiYuan MCP server is deliberately not registered - the drain
  silently did nothing.
- **Everything Pacman writes goes through `mcp__secondbrain__*`**, so proposals
  and drop records land on whatever backend the install runs.

If `pacman-queue.py` exits non-zero, do NOT improvise a substitute and do NOT report a clean drain. Print its stderr and stop. Silently draining nothing is
the failure mode this whole line of work exists to remove.

## Steps

1. **Read `chassis/skills/pacman.md` in full** before doing anything else. The
   skill defines the 4 gates, drop logging, proposal format, approval flow, and
   queue management. Follow it exactly.

2. **Claim a batch.** Oldest-first, capped:
   ```bash
   python3 "$CHASSIS_ROOT/scripts/pacman-queue.py" claim --limit ${PACMAN_MAX_BATCH_URLS}
   ```
   This returns a JSON array of `{token, url, source, entry_group, created_at}`
   and marks those rows claimed so a concurrent drain cannot take them. An empty
   array means no work - stop here and post nothing.

   `token` is the row's approval token: six lowercase letters, e.g. `qhtnbz`.
   It is what the installer types to approve. Do not invent one, do not use a
   document id, and do not use a SiYuan block ID.

3. **For each claimed row:** run the URL through the 4 gates per
   `chassis/skills/pacman.md`. One row is one URL, so there is no per-block URL
   extraction to do any more.

4. **After processing each URL** (drop OR proposal - verdict regardless), mark
   the row done:
   ```bash
   python3 "$CHASSIS_ROOT/scripts/pacman-queue.py" complete <token> --verdict <drop|proposal|fetch_failed> [--gate N] [--doc-id <adapter-doc-id>]
   ```
   Non-negotiable per the skill - a row that survives processing gets
   re-processed forever. `complete` is exactly-once: calling it twice for the
   same token is a harmless no-op that reports `"changed": false`, so retrying
   a step is safe.

   If you cannot finish a claimed row (you ran out of context, a tool failed),
   hand it back instead of leaving it claimed:
   ```bash
   python3 "$CHASSIS_ROOT/scripts/pacman-queue.py" release <token>
   ```

5. **Drops** post a 1-line note to the configured Discord channel
   (`chat_id: ${PACMAN_DISCORD_CHAT_ID}`) AND append a row to the Dropped
   archive via `mcp__secondbrain__append_to_doc(doc_id: "${PACMAN_DROPPED_DOC_ID}", content: ...)`.
   Format per the skill. Then `complete <token> --verdict drop --gate N`.

6. **Proposals** write a sub-doc via
   `mcp__secondbrain__create_doc(parent: "${PACMAN_PROPOSALS_PARENT}", title: "YYYY-MM-DD-<slug>", body: ...)`
   AND post a 4-section TLDR to the configured Discord channel quoting the
   row's `token` in the approve/reject/defer line. Use the `deeplink` that
   `create_doc` returns - do not construct a URL yourself, the format differs
   per backend. Then
   `complete <token> --verdict proposal --doc-id <the id create_doc returned>`,
   which is what lets the approval step later find the document from the token.

7. **End-of-batch summary** to the configured Discord channel:
   ```
   Pacman drain: processed N URLs (P proposals, D drops). Next drain in ~4h.
   ```
   Skip the summary if N=0 (no work, no noise).

## Hard rules

- Cap `${PACMAN_MAX_BATCH_URLS}` URLs per fire. `claim --limit` enforces this;
  if the queue has more, leave them and the next drain picks them up.
- Process oldest-first (FIFO). `claim` already orders by `created_at ASC`.
- Always `complete` a row after processing, even on drops.
- Respect `PACMAN_HARD_PAUSE` - if the file exists at
  `$CHASSIS_HOME/PACMAN_HARD_PAUSE`, post a one-liner to the configured channel
  confirming the pause and exit without processing or claiming.
- Don't editorialize in proposal recommendations beyond what the skill
  specifies. Pacman surfaces evidence; the installer decides.
- If a URL can't be fetched (404, timeout, paywall), drop it at gate 0 with
  reason "fetch failed" and a 1-line note, then
  `complete <token> --verdict fetch_failed --gate 0`. Don't retry within the
  same fire.

## Logging

Every URL processed (drop OR proposal OR fetch-failure) gets a JSONL entry in
`$CHASSIS_HOME/logs/pacman/YYYY-MM-DD.jsonl`:
```json
{"ts": "...", "token": "...", "url": "...", "verdict": "drop|proposal|fetch_failed", "gate": 0|1|2|3|4|null, "doc_id": "...", "source": "telegram|discord|manual|heartbeat-drain"}
```
