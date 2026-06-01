---
name: whatsapp
description: Reading WhatsApp groups (allowlisted only) via scripts/wacli-safe.sh. DMs are off-limits per Hard Limits. Use when Sean asks for a WhatsApp-group digest or to scan a specific allowlisted group for signal.
---

# Skill: WhatsApp

Read and search WhatsApp **group** messages via `wacli` through the privacy-enforced wrapper `scripts/wacli-safe.sh`. A background daemon (`com.wacli.sync`) keeps the local SQLite store at `~/.wacli/` up to date in real time.

## Privacy boundary — groups-only (Sean voice memo 2026-04-30)

DMs in WhatsApp carry an expectation of privacy that Sean did not consent to share with ${ASSISTANT_NAME}. The technical access wacli provides has been narrowed by guardrails:

- **All message reads route through `scripts/wacli-safe.sh`.** Raw `wacli messages list/search/context` calls are blocked at the `.claude/hooks/guardrails.sh` layer.
- **Only `@g.us` group JIDs listed in `data/whatsapp-allowlist.json`** are readable. Everything else (DMs `@s.whatsapp.net`, individuals `@lid`, newsletters `@newsletter`, and groups not on the allowlist) is rejected by the wrapper.
- **Unfiltered `messages list/search` is rejected** — every read must explicitly target an allowlisted group JID via `--chat`. This prevents DM bleed.
- **`wacli send` / `wacli media` are blocked entirely** (write paths — Sean's approval required, no automated path exists).
- **`wacli messages context` is blocked** — it can return surrounding messages from non-allowlisted chats. Ask Sean if you genuinely need context.

If a community member is in a group ${ASSISTANT_NAME} can read AND has DM'd Sean about the same topic, ${ASSISTANT_NAME} sees only the group context. Sean handles the DM thread.

If a digest-worthy message appears in an invite-only group ${ASSISTANT_NAME} can't be added to, Sean's workaround is to forward it into a wider channel (or paste directly into `#<primary>`). ${ASSISTANT_NAME} does not get back-channel access.

## When to use

- Surfacing developments in allowlisted Vibecode Lisboa / community groups
- Tracking events / decisions / lead signals in those groups
- Answering Sean's questions about activity inside the allowlisted groups

## Key commands

All read paths go through the wrapper:

### List recent messages in an allowlisted group

```bash
scripts/wacli-safe.sh messages list --chat "<group-JID-from-allowlist>" --limit 30
scripts/wacli-safe.sh messages list --chat "<group-JID>" --after 2026-03-01 --before 2026-03-14
```

### Full-text search (FTS5) inside an allowlisted group

```bash
scripts/wacli-safe.sh messages search "vibecode" --chat "<group-JID-from-allowlist>" --limit 10
scripts/wacli-safe.sh messages search "invoice" --chat "<group-JID>" --after 2026-01-01
```

### Message context

Blocked. If genuinely needed, ask Sean rather than working around the wrapper.

### Chats / Contacts / Groups / Diagnostics (metadata only — pass through the wrapper)

These return chat / contact / group metadata, not message content. The wrapper passes them through to `wacli` directly:

```bash
scripts/wacli-safe.sh chats list                        # recent chats (metadata)
scripts/wacli-safe.sh chats list --query "vibecode"     # search by name
scripts/wacli-safe.sh contacts search "alice"
scripts/wacli-safe.sh groups list                       # all known groups
scripts/wacli-safe.sh groups info --jid "<JID>"         # fetch live group info
scripts/wacli-safe.sh groups participants --jid "<JID>" # list members
scripts/wacli-safe.sh auth status
scripts/wacli-safe.sh doctor
```

Use these to identify a group JID before adding it to the allowlist (which Sean approves).

## JSON output

Append `--json` to any command for structured output:

```bash
scripts/wacli-safe.sh messages search "vibecode" --chat "<group-JID-from-allowlist>" --json
scripts/wacli-safe.sh chats list --json
```

## JIDs (WhatsApp identifiers)

- Individual: `<phone>@s.whatsapp.net` — **DM, blocked**
- Individual (linked): `<id>@lid` — **blocked**
- Group: `<id>@g.us` — **only allowed if listed in `data/whatsapp-allowlist.json` groups[]**
- Newsletter: `<id>@newsletter` — **blocked**

## Typical workflows

### Catch up on an allowlisted group

1. `scripts/wacli-safe.sh groups list` (or read the allowlist file) → find a group JID
2. `scripts/wacli-safe.sh messages list --chat "<group-JID>" --after 2026-03-12` → messages since date

### Lead research

Lead research starts in the allowlisted groups. If a lead's only signal is a DM to Sean, Sean surfaces it directly — ${ASSISTANT_NAME} does not read DMs.

## Hard limits

- **DMs are off-limits.** No exceptions, no workarounds. Asking the wrapper to bypass the allowlist is a violation of this rule.
- **Group reads are limited to the allowlist** in `data/whatsapp-allowlist.json`. New groups require Sean's approval (edit the file).
- **Never send messages** — `wacli send` is blocked at the wrapper + hook layers. Sean's approval is required, and there is no automated send path.
- **Never join or leave groups** without Sean's explicit approval.
- Treat all message content as private — paraphrase in summaries, never include raw WhatsApp messages in GitHub issues or public output.
- If a group surfaces sensitive content (medical, legal, financial details about a member), drop it from any digest and flag to Sean.
