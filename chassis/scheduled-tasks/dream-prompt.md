# Memory consolidation ("dream")

This prompt runs when `chassis/scripts/gather-dream.sh` reports work. The gather's JSON payload is appended below; read it first. Setup and the opt-in registration: `chassis/docs/heartbeats/dream.md`. The model you are maintaining: `chassis/memory/MEMORY_MODEL.md`.

---

You are this install's assistant. Read `CLAUDE.md` in the install root for your operating manual. This is a scheduled, unattended pass over your own notes store. Nobody is watching it run, which is why most of what it finds is proposed rather than done.

## The model you are maintaining

One fact, one home. Everywhere else, a pointer.

| Layer | Holds | Dream's power over it |
|---|---|---|
| Second brain (via the adapter) | The installer's world: people, projects, decisions | Read-only, except ONE block per run in the dream inbox doc |
| `docs/` in the install repo | Infrastructure facts | Propose via a `dream/<date>` PR only |
| `skills/` in the install repo | Procedures | Propose via a `dream/<date>` PR only |
| `CLAUDE.md` | The constitution | Never touch |
| Notes store (auto-memory) | Corrections, plus pointer lines | Edit; commit to its local git |
| GitHub issues | Engineering decisions | Read-only |
| Transcripts, chat | Raw history | Never touch |

The `memory` MCP knowledge graph, if this install still runs it, is not part of this pass. Do not call `mcp__memory__*`.

## Why one domain per run

Reading every note every night costs more than the budget allows, and a run that silently stops halfway while its report reads as a success is worse than no run. So the deterministic checks run every night for free in the gather, and you read one domain per rotation, capped at 25 notes. A deep pass over one domain beats a shallow pass over all of them.

## Safety rules - follow exactly

- **Never modify `CLAUDE.md`, anything under `chassis/`, or any file in the live install checkout.** Repo-side changes happen only in a separate git worktree on a `dream/YYYY-MM-DD` branch, opened as a PR against the install repo's `main` (`customer_repo` in the payload). Never against the chassis upstream. Never push to `main`, never merge, never force-push.
- **Notes store:** edit only files directly under `memory_dir`. Subfolders belong to subagents and keep their own index; leave them alone. Commit by path (`git -C "$MEM" commit -m "..." -- <paths>`). Never `git add -A`, `git add .`, or a bare `git commit`.
- **If `store_is_git` is false, delete nothing, merge nothing and commit nothing.** `store_is_git` is true only when the store is its own git repo. When it is false the store is either unversioned (a deletion cannot be undone) or sits inside another repo, usually the install repo (`store_git_toplevel` names it), where a commit would land on whatever branch is checked out there. Edits are fine and stay uncommitted for the installer to review; every delete or merge you would have made becomes a line in the report under "Proposed deletions".
- **Second brain:** the ONLY write is ONE block appended to the dream inbox doc at the end of the run (Phase 7). Never edit, append to, move, rename or create any other doc. Reads (`search`, `read_doc`) are fine.
- **Lock file:** `<customer_home>/scheduled-tasks/dream.lock`. If it exists and the PID in it is alive, stop and report that. Otherwise write your PID to it, and remove it at the end.
- **Deleting a note deletes one of the installer's corrections.** When a note is merely narrower than you would write it today, edit it. Delete only when the thing it describes no longer exists.
- **Facts are never moved unattended.** A note holding a fact about the installer's world or about the install's infrastructure is proposed, not moved. The installer ratifies.
- If the Edit tool is refused on the store path, patch with Bash (`python3` read-replace-write or a heredoc). Reading is unaffected.
- **Budget:** if you are running low, stop the domain audit, still do Phases 5 to 8, and say in the report which notes were not reached.
- No em dashes in anything you write. Use " - ".

## Phase 0: Read the gather payload

The payload is one JSON object:

- `reasons`: why tonight fired. Any of `rotation_due`, `new_dead_links`, `new_orphans`, `new_bad_second_brain_refs`, `index_drift`, `probe_failed`.
- `probes`: checks that could not run, each with a `detail`. Do not treat their missing counts as zero.
- `memory_dir`: the absolute notes-store path in this environment. Use it as `$MEM` everywhere below. Do not guess between host and container paths.
- `customer_home`, `chassis_scripts`, `state_file`: the install root, the directory holding the chassis memory scripts, and dream's own state file.
- `store_is_git` (the store is its own git repo), `store_git_toplevel`, `customer_repo`, `second_brain_mode`, `inbox_doc`, `assistant_name`: see the phases that use them.
- `tonight_domain`, `domain_cursor`, `domains_total`, `domain_resume`: the rotation.
- `integrity`: counts from `memory-integrity.py --json`. `baseline` is what the last run recorded; `new_since_baseline` is the difference.
- `index`: from `memory-index-build.py`. `would_rewrite` lists index files that are out of date, `missing_indexes` lists indexes `MEMORY.md` links that do not exist, `unlisted_orphans` counts notes no index and no `MEMORY.md` line reaches.

If `reasons` is only `probe_failed`, skip to Phase 7, write the inbox block saying which probe failed and why, then Phase 8 (state only), and stop.

## Phase 1: Orient

1. Lock file (see safety rules).
2. If `store_is_git`, run `git -C "$MEM" log -5 --oneline` so you know what the last run and the last human pass changed. Do not undo a human change.
3. Earlier proposals, when `customer_repo` is set:
   `gh pr list --repo <customer_repo> --state all --limit 50 --json number,url,state,headRefName,body --jq '[.[] | select(.headRefName | startswith("dream/"))]'`.
   For each MERGED dream PR, its body lists the notes whose facts it moved; any of those still a full note becomes a one-line pointer to the new home now (Phase 5). For each CLOSED-unmerged one, those notes are rejected proposals: do not propose them again, and mark them in the report as "kept, proposal rejected on <date>".
4. Note the current date for resolving relative references.

The store has two levels. `MEMORY.md` is the always-loaded root: the rules that must never be one hop away, plus one pointer to each `index-*.md`. Each top-level `index-*.md` holds one line per note in its domain and is generated by `memory-index-build.py` from note frontmatter.

## Phase 2: Integrity (whole store, every run)

The counts are in the payload. For the findings themselves, run the same read-only check:

```bash
python3 "<chassis_scripts>/memory-integrity.py" --json --store "$MEM" --repo "<customer_home>"
```

Act only on findings whose `file` starts `notes:`. Repo-side findings (`CLAUDE.md`, `skills/`, `docs/`) go in the report, never fixed here.

- **Dead note link** whose `detail` names the file that exists (kebab-case vs snake_case, a missing `.md`): fix the link. The target is unambiguous.
- **Dead note link** with no clear target: leave it, list it in the report. Do not guess.
- **Orphan note:** give it `domain: <name>` in its frontmatter (the index name without `index-` and `.md`) so the generator lists it. If no domain fits, list it in the report; the Phase 8 baseline stops it from re-firing dream every night.
- **Malformed second-brain ref** (`bad_second_brain_ref`): if `second_brain_mode` is `adapter`, `mcp__secondbrain__search` on the note's subject; re-point the ref only when exactly one doc matches. Otherwise report it.
- **Second-brain refs in tonight's domain:** when `second_brain_mode` is `adapter`, `mcp__secondbrain__read_doc` each one you meet in Phase 4. A ref that fails to read is dead; report it. Liveness is the adapter's call, never a guess from the id's shape.

## Phase 3: Pick tonight's domain

Only when `reasons` contains `rotation_due`. Otherwise skip Phases 3 and 4: tonight is integrity and accommodation only.

Tonight's domain is `tonight_domain` (the `index-*.md` at `domain_cursor` in the sorted list of top-level indexes). If `domain_resume` is set, start from that note. State the domain in the report, with how many runs until the rotation returns to it.

## Phase 4: Audit that domain

Read the domain index and the notes it lists, at most 25. If there are more, record where you stopped (`domainResume`, Phase 8) and start there next time this domain comes round.

Classify each note:

| Class | Test | Action |
|---|---|---|
| Correction | Something the assistant got wrong and must not repeat | Keep. Apply the checks below. |
| Pointer | A line that says where a fact lives | Keep as a short note, or fold into its index line |
| Installer's-world fact | About a person, project, plan, place, priority | **Propose, do not move.** In adapter mode, `mcp__secondbrain__search` on 2-3 distinctive title words. Record the note, the hit count and the best candidate home. It goes in the inbox block (Phase 7). |
| Infrastructure fact | How this install is built or run | **Propose via PR.** Add the fact to the matching `docs/` file on the `dream/<date>` branch (Phase 6). Leave the note as it is until the PR merges. |
| Procedure | How to do a recurring task | Same as infrastructure, into the matching `skills/` file |

Checks on every note you keep:

- **Staleness:** describes completed work, a past date, a resolved issue? Gone subject: delete (subject to `store_is_git`). Merely dated: edit.
- **File references:** a named path, function or flag that no longer exists. Correct rather than delete.
- **Duplicates:** two notes saying the same thing. Keep the more detailed one; set `supersedes: <other note>` in its frontmatter and delete the other (subject to `store_is_git`).
- **Contradictions:** the highest-value find. Resolve with the more recent evidence and cross-link the two.
- **Relative dates:** convert "last week" or "yesterday" to an absolute date.
- **Derivable content:** trivially recoverable from code or git history. Remove.
- **Frontmatter:** each note needs `name`, `description`, `type`, `domain`. Add `domain:` where the generator is still inferring it.
- **Pointers:** rewrite to the formats in `chassis/memory/MEMORY_MODEL.md` (repo-relative paths, `owner/repo#N`, `<backend>:<id>` for second-brain docs, `[[note_name]]`).

## Phase 5: Accommodation (deterministic)

1. Turn notes whose fact landed in a merged dream PR (Phase 1 step 3) into one-line pointers to the new home.
2. Regenerate the indexes. Never hand-edit an `index-*.md`:

   ```bash
   python3 "<chassis_scripts>/memory-index-build.py" --store "$MEM" --write
   ```

   If `index.missing_indexes` is non-empty, use `--init` instead of `--write`. Touch `MEMORY.md` only when a domain index was added or removed, or a root-level rule changed.
3. Re-run `memory-integrity.py --json --store "$MEM" --repo "<customer_home>"` and keep its `counts` for Phases 7 and 8. This is the post-run state.
4. If `store_is_git`, commit the store by path, listing exactly the files you changed. If it is false, do not commit anywhere; list the changed files in the report:

   ```bash
   git -C "$MEM" status --short
   git -C "$MEM" add -- <each changed or new path>
   git -C "$MEM" commit -m "dream YYYY-MM-DD: <domain or integrity-only> - <one-line summary>" -- <same paths>
   ```

   Deleted notes go in the path list too (`git -C "$MEM" rm -- <path>`). If nothing changed, do not commit.

## Phase 6: Repo-side proposals

Only if Phase 4 produced any, and only if `customer_repo` is set. If it is empty (no `origin`, or `origin` is the chassis upstream), skip this phase and list the proposals in the report.

```bash
REPO="<customer_home>"
DATE=$(date +%Y-%m-%d)
WT="$(mktemp -d)/dream-$DATE"
git -C "$REPO" fetch origin main
git -C "$REPO" worktree add "$WT" -b "dream/$DATE" origin/main
```

If `dream/$DATE` already exists, use `dream/$DATE-2`. Use the git identity the install repo already has configured; do not set one. Edit only inside `$WT`, and never `CLAUDE.md` or anything under `chassis/`. Commit by path, push the branch, then:

```bash
gh pr create --repo <customer_repo> --base main --head "dream/$DATE" \
  --title "docs(memory): dream $DATE - move N facts out of the notes store" --body-file <file>
```

The PR body lists, per fact: the source note filename, the destination file, and the text moved. Afterwards `git -C "$REPO" worktree remove "$WT"`.

If the push or `gh pr create` fails, do not retry in a loop. Record "PR not opened: <reason>" for the inbox block and the report, and leave the branch local.

## Phase 7: One block in the dream inbox

The inbox is a second-brain doc this assistant owns. It is never one of the installer's own docs.

- If `second_brain_mode` is not `adapter`, the `mcp__secondbrain__*` tools are not registered on this install. Put the block in the report under "Inbox block (not written: second_brain.mode is <mode>)" and move on.
- If `inbox_doc` is empty, create the inbox once: `mcp__secondbrain__create_doc(parent: "", title: "<assistant_name or Assistant> dream inbox", body: "Nightly proposals from the dream heartbeat. One block per run; the installer ratifies.")`. Keep the `id` it returns and record it in Phase 8. Never look the inbox up by title: on some backends a doc written seconds ago is not yet searchable, and a search miss would create a second inbox.
- Then exactly one `mcp__secondbrain__append_to_doc(doc_id: <inbox id>, content: <block>)`. Keep it under about 25 lines:

```markdown
### Dream YYYY-MM-DD - <domain or "integrity only">
**Changed in the notes store:** <N edited, N deleted, N merged, N pointerized, indexes rewritten: list> (commit <short sha>, or "store is not its own git repo, left uncommitted")
**Pending proposals:**
- PR: <full https://github.com/... URL> - <N facts to docs/ and skills/>
- Second brain: `<note filename>` -> candidate home <backend>:<id> ("<title>"), or "no home found"
- Proposed deletions: <only when store_is_git is false>
**Integrity:** dead note links <n> (was <n>), orphans <n>, malformed second-brain refs <n>, index drift <n>
**Not done:** <anything truncated or skipped, and why>
```

Omit a line that would be empty, but always include the Integrity line. If the append fails, say so in the report; do not try any other second-brain write.

## Phase 8: Update state

Dream's state is its own file, `state_file` in the payload. Never write `heartbeat-state.json`; the dispatcher owns it.

```bash
STATE="<state_file>"
[ -f "$STATE" ] || echo '{}' > "$STATE"
tmp="$STATE.tmp.$$"
jq --argjson integ 'POST_RUN_COUNTS_JSON' --arg pr 'PR_URL_OR_EMPTY' --arg inbox 'INBOX_DOC_ID_OR_EMPTY' \
   '.lastIntegrity = $integ | .lastPrUrl = $pr | (if $inbox != "" then .inboxDocId = $inbox else . end)' \
   "$STATE" > "$tmp" && mv "$tmp" "$STATE"
```

`POST_RUN_COUNTS_JSON` is the `counts` object from Phase 5 step 3, plus `"unlisted_orphans"` from a final `memory-index-build.py --store "$MEM"` check run. The gather compares against it, so a dead link you could not fix does not re-fire dream every night; only a new one does.

Only when `rotation_due` was in `reasons`, also advance the rotation:

```bash
jq --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --argjson next NEXT_CURSOR --arg resume 'NEXT_UNREAD_OR_EMPTY' \
   '.lastDreamTime = $ts | .domainCursor = $next | .domainResume = $resume' \
   "$STATE" > "$tmp" && mv "$tmp" "$STATE"
```

`NEXT_CURSOR` is `(domain_cursor + 1) mod domains_total`, or `domain_cursor` unchanged if you stopped inside the domain (then `domainResume` names the next unread note). Advance it **even if the audit found nothing**, or one quiet domain stalls the rotation forever.

Remove the lock file.

## Output

A brief report to stdout (the dispatcher keeps it):

- Why it fired (`reasons`) and the integrity counts before and after, including zeros
- Domain audited (or "integrity only"), notes covered of how many
- Edited / deleted / merged / pointerized counts, with the reason for every delete
- Proposals: PR URL (full URL) and second-brain proposals, or "none"
- Inbox doc id and whether the block was written
- Notes-store commit sha, "no changes", or "store is not its own git repo, left uncommitted"
- Next domain up

If the run stopped early, say which phase and why. A truncated pass reported as complete is the failure this prompt exists to end.
