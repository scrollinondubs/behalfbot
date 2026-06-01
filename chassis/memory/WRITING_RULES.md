# Memory Writing Rules

The chassis has a persistent file-based memory system at `${CHASSIS_HOME}/memory/`. Build it up over time so that future conversations have a complete picture of the installer's role, preferences, ongoing work, and the context behind tasks.

If the installer explicitly asks to remember something, save it immediately as whichever type fits best. If they ask to forget something, find and remove the relevant entry.

## Types of memory

| Type | When to save | Body structure |
|---|---|---|
| **user** | Information about the installer's role, goals, responsibilities, knowledge | Plain prose. Tailor future behavior to fit |
| **feedback** | Guidance about how to approach work — corrections AND confirmations | Lead with the rule, then `**Why:**` line + `**How to apply:**` line |
| **project** | Ongoing work, goals, initiatives, bugs, incidents | Lead with the fact/decision, then `**Why:**` + `**How to apply:**` line |
| **reference** | Pointers to where information lives in external systems | Plain prose with the URL or path |

Plus per-plugin namespaced types (only when the relevant plugin is active):

| Type | When to save | Plugin |
|---|---|---|
| **lead:firstname-lastname** | Outreach interactions | dating, sdr-outreach |
| **topic:slug** | Research topics | content-research |
| **student:firstname** | Student situations | (per installer) |
| **task:YYYY-MM-DD-slug** | Significant task outcomes | (per installer) |

## What NOT to save in memory

- Code patterns, conventions, architecture, file paths, project structure — derive from current project state instead
- Git history, recent changes, who-changed-what — `git log` / `git blame` are authoritative
- Debugging solutions or fix recipes — the fix is in the code; the commit message has the context
- Anything already documented in CLAUDE.md
- Ephemeral task details: in-progress work, temporary state, current conversation context

These exclusions apply EVEN WHEN the installer explicitly asks to save. If they ask to save a PR list or activity summary, ask what was *surprising* or *non-obvious* about it — that's the part worth keeping.

## How to save memories

Two-step:

**Step 1** — write the memory to its own file (e.g. `user_role.md`, `feedback_testing.md`):

```markdown
---
name: {{memory name}}
description: {{one-line description — used to decide relevance later}}
type: {{user|feedback|project|reference}}
---

{{content — for feedback/project, structure as: rule/fact, then **Why:** and **How to apply:** lines}}
```

**Step 2** — add a pointer to that file in `MEMORY.md`:

```markdown
- [Title](file.md) — one-line hook
```

`MEMORY.md` is the index, not memory itself. Each entry should be one line, under ~150 characters.

## When to access memory

- When memories seem relevant, or the installer references prior-conversation work
- ALWAYS access when the installer explicitly asks to check, recall, or remember
- If the installer says to *ignore* or *not use* memory: do not apply remembered facts, cite, compare against, or mention memory content
- Memory records can become stale. Verify against current state before acting on a memory; trust what you observe now over what's remembered

## Before recommending from memory

A memory naming a specific function, file, or flag is a claim that it existed *when the memory was written*. It may have been renamed, removed, or never merged. Before recommending it:

- If the memory names a file path: check the file exists
- If the memory names a function or flag: grep for it
- If the installer is about to act on your recommendation (not just asking about history), verify first

"The memory says X exists" is not the same as "X exists now."

## Memory vs. other persistence

- **Plan files**: when starting a non-trivial implementation task and aligning with the installer on approach. Use a Plan, not a memory.
- **Tasks**: discrete steps within the current conversation. Use the task system, not memory.
- **Memory**: cross-session continuity. Information that should persist past this conversation.
