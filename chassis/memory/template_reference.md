---
name: Reference memory template
description: Example shape for reference-type entries. Delete this file before going live.
type: reference
---

> **Template.** Copy this file pattern when writing a reference-type memory. Body is plain prose pointing at the external system.

## Body structure

Plain prose. Include the URL or path, what's there, when to look. No `**Why:**`/`**How to apply:**` structure required (those are for feedback/project).

## Examples

```markdown
---
name: Pipeline bugs tracked in Linear INGEST project
description: Linear project "INGEST" is where pipeline bugs get triaged + fixed.
type: reference
---

Pipeline bugs are tracked in Linear project "INGEST". Engineers triage
inbound issues there during the morning standup; backlog reviewed weekly.
URL: https://linear.app/<workspace>/team/ING

When the installer mentions a pipeline bug, check INGEST first to see
whether it's already been filed.
```

```markdown
---
name: Oncall latency dashboard
description: grafana.internal/d/api-latency is the dashboard oncall watches.
type: reference
---

The Grafana board at grafana.internal/d/api-latency is what oncall watches.
If you're touching request handling, that's the dashboard that'll page
someone when latencies spike.

Check this before merging any request-path change.
```

## When to save reference entries

When the installer mentions a system you'd otherwise have to re-discover next time it comes up. Pointers to:

- Issue trackers (specific projects)
- Dashboards (specific URLs)
- Slack/Discord channels (specific channels)
- Documentation pages (canonical sources)
- People (contact methods, role context)
