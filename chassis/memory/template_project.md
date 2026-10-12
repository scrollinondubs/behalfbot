---
name: Project memory template
description: Example shape for project-type entries. Delete this file before going live.
type: project
---

> **Template.** Copy this file pattern when writing a project-type memory. Body MUST include `**Why:**` and `**How to apply:**` lines because project state decays fast.

> **Frontmatter.** Every note carries `name`, `description`, `type` and `domain` (the index it belongs in, e.g. `domain: workflow`), and optionally `supersedes` (the note it replaces). The index generator reads them; see `MEMORY_MODEL.md`. After saving, run `python3 chassis/scripts/memory-index-build.py --write` instead of editing an index by hand.

> **What belongs here.** A project decision that changes how you act, with its reason. The project's state (status, owners, dates, history) is the installer's world and lives in the second brain; point at it with `<backend>:<doc id>`.

## Body structure

Lead with the fact or decision, then `**Why:**` + `**How to apply:**`.

## Examples

```markdown
---
name: Auth middleware rewrite - compliance-driven
description: Legal flagged session token storage as non-compliant; rewrite is mandatory by Q2 close.
type: project
domain: projects
supersedes: project_auth_tech_debt_cleanup
---

Auth middleware rewrite is driven by legal/compliance requirements
around session token storage, not tech-debt cleanup.

**Why:** Legal review surfaced that the existing storage doesn't meet
the new compliance bar. Hard deadline = end of Q2.
**How to apply:** Scope decisions favor compliance over ergonomics.
If a developer suggests "while we're in here let's also refactor X",
push back unless X is on the compliance critical path.
```

## What ages out

Project memories decay fast - re-read them periodically and prune what's no longer load-bearing. Project state is "what was true when this was written"; verify against current state before acting on it.

A good rule: if a project memory hasn't been touched in 3 months, ask whether it's still relevant. Half the time it's done; the other half it needs updating.
