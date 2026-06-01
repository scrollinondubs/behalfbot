---
name: Project memory template
description: Example shape for project-type entries. Delete this file before going live.
type: project
---

> **Template.** Copy this file pattern when writing a project-type memory. Body MUST include `**Why:**` and `**How to apply:**` lines because project state decays fast.

## Body structure

Lead with the fact or decision, then `**Why:**` + `**How to apply:**`.

## Examples

```markdown
---
name: Auth middleware rewrite — compliance-driven
description: Legal flagged session token storage as non-compliant; rewrite is mandatory by Q2 close.
type: project
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

Project memories decay fast — re-read them periodically and prune what's no longer load-bearing. Project state is "what was true when this was written"; verify against current state before acting on it.

A good rule: if a project memory hasn't been touched in 3 months, ask whether it's still relevant. Half the time it's done; the other half it needs updating.
