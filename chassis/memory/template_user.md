---
name: User memory template
description: Example shape for user-type entries. Delete this file before going live; it's a template, not a real memory.
type: user
---

> **Template.** Copy this file pattern when writing a new user-type memory entry. Replace the frontmatter `name`/`description` with the actual entry's name + a one-line description. Body is plain prose.

> **Frontmatter.** Every note carries `name`, `description`, `type` and `domain` (the index it belongs in, e.g. `domain: workflow`), and optionally `supersedes` (the note it replaces). The index generator reads them; see `MEMORY_MODEL.md`. After saving, run `python3 chassis/scripts/memory-index-build.py --write` instead of editing an index by hand.

## What goes here

User-type memories capture information about the installer's role, goals, responsibilities, and knowledge. They tailor future agent behavior to the installer's specific perspective.

## Examples

> **`user_role.md`** - installer is a venture-fund partner managing LP relationships + portfolio research. Their day-to-day is in Notion (LP CRM) + Google Drive (deal memos).

> **`user_communication_style.md`** - installer prefers terse replies (3-5 sentences max), no bullet lists for short answers, reaches for "ack" / "noted" rather than "great, will do!".

## What NOT to write here

- Bio facts that change every few months (current job title, weight, address). They are the installer's world and live in the second brain; a note may point at the doc (`<backend>:<doc id>`).
- Anything already in `INSTALL_PROFILE.md` from the install - that's reference material, not user memory.
