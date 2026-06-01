---
name: Feedback memory template
description: Example shape for feedback-type entries. Delete this file before going live.
type: feedback
---

> **Template.** Copy this file pattern when writing a feedback-type memory. Body MUST include `**Why:**` and `**How to apply:**` lines so future-you can judge edge cases.

## Body structure

Lead with the rule (the actual guidance), then explain WHY and HOW TO APPLY.

## Examples

```markdown
---
name: Don't mock the database in integration tests
description: Tests against the real DB caught a migration bug that mocked tests missed last quarter.
type: feedback
---

Integration tests must hit a real database, not mocks.

**Why:** Last quarter we shipped a broken migration to prod because the
mocked tests didn't see the schema mismatch the live DB would have caught.
**How to apply:** When writing or reviewing tests in the integration suite,
reject any new mock of the DB layer. Use a `-preview` test database instead.
```

## When to save feedback

- Any time the installer corrects your approach ("no, not that", "stop doing X")
- Any time the installer confirms a non-obvious approach worked ("yes exactly", "perfect, keep doing that")
- After a near-miss or a successful save where the rule emerged

Both directions matter. Confirmations are quieter than corrections — watch for them. If you only save corrections, you'll avoid past mistakes but drift away from validated patterns.
