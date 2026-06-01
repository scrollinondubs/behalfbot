---
name: welfare-check
description: Operator-welfare escalation. Detect when the principal has been silent for an extended period (no Discord activity, no signal-hook fires, no Oura sync if configured) and ladder through a 5-stage emergency cascade: Discord ping → direct SMS → tier-1 local contacts → broader contact list → designated family contact + repeat email. Generic across installs — no live-location, duress codewords, or mode inference (those personal-security components live in the operator's private install, not in the open chassis).
---

# Welfare Check

The chassis-shipped welfare-check pattern. Triggers when the principal goes silent past a configured threshold; runs a graduated emergency-contact cascade.

This is the **operator-welfare-only** subset of what was originally a larger personal-safety skill. The bodyguard layer — live location, duress codewords, mode inference, camera integration, iMessage emergency-group cascade — is intentionally NOT in the open chassis. Revealing those components publicly would hand a would-be adversary the playbook to undermine them. Operators who need the bodyguard layer build it as a private plugin in their install-specific repo, alongside this welfare-check core.

---

## What this skill does

1. Watches an aggregated "silence signal":
   - Last principal-authored Discord message (via `welfare-signal-hook.sh`)
   - Optional: Oura ring sleep/activity sync timestamps (if `OURA_TOKEN` is configured)
   - Optional: any other heartbeat-state evidence of life
2. When silence exceeds `silence_threshold_hours` (default 22h), the welfare-check heartbeat prompt fires
3. The prompt invokes `welfare-cascade-send.py` to step through escalation stages

## Escalation ladder

| Stage | Trigger | Action |
|-------|---------|--------|
| 0 | Silence threshold hit | Discord ping to `${DISCORD_PRIMARY_CHANNEL_ID}` |
| 1 | T+1h after Stage 0 | Direct SMS to `${PRINCIPAL_MOBILE}` via Twilio |
| 2 | T+2h after Stage 0 | SMS tier-1 (priority 1+2) contacts asking them to try to reach the principal |
| 3 | T+4h after Stage 0 | AgentMail to the full contact list + optional iMessage group (install-specific helper) |
| 4 | T+6h after Stage 0 | SMS the designated "mother" / family contact + second-round AgentMail to all |

Each stage is state-tracked in `data/welfare-escalation-state.json` so re-runs are idempotent. The `welfare-pause.sh` script provides an off-switch (sets a pause sentinel; the heartbeat reads it and goes silent).

## Required configuration

`.env` (per-install secrets):
```bash
PRINCIPAL_NAME="Jane Doe"
PRINCIPAL_FIRST_NAME="Jane"
PRINCIPAL_MOBILE="+14155551234"
PRINCIPAL_DISCORD_USERNAME="janedoe123"    # for the signal-hook to recognise her messages
ASSISTANT_NAME="${ASSISTANT_NAME}"                        # or "Asimov", "Ozzy", etc.
ASSISTANT_DISPLAY_NAME="${ASSISTANT_NAME} - Jane Doe's AI assistant"
DISCORD_PRIMARY_CHANNEL_ID="123456789012345678"
DISCORD_BOT_TOKEN="..."
TWILIO_ACCOUNT_SID="..."
TWILIO_AUTH_TOKEN="..."
TWILIO_FROM="+14155550000"
AGENTMAIL_API_KEY="..."
AGENTMAIL_FROM="welfare@example.com"
OURA_TOKEN="..."                            # optional
```

`plugins/angel-protocol/data/emergency-contacts.json` (gitignored — never committed). See `emergency-contacts.template.json` for the schema. Minimum fields per contact: `name`, `phone`, `email`, `tier` (`local`/`remote`), `priority` (1+2 are tier-1 cascade), and optionally `relationship` (`mother`/`father`/`partner`/etc., used by Stage 4).

## Configuration in `chassis.config.yaml`

```yaml
modules:
  welfare_check:
    enabled: true
    silence_threshold_hours: 22
    emergency_contacts_path: ${CHASSIS_HOME}/plugins/angel-protocol/data/emergency-contacts.json
    sms_provider: twilio
```

## What gets stripped from chassis vs kept in your private install

If you want a full personal-safety system on top of this welfare-check base, add these to your **install-specific private repo** (NOT to the chassis):

- Live location polling (iCloud / OwnTracks / Tailscale phone / Google Timeline)
- Duress codeword listener + cascade trigger
- Home/away/sleep mode inference
- Camera integration (Ivideon, Frigate, etc.) for visual welfare confirmation
- iMessage emergency-group cascade helper (`_imessage_group.py`)
- Compound-signal dormancy detection across phone activity + ring + location

Reason: each of these reveals a piece of the operator's security playbook. Stating publicly "the agent will check the camera at hour 4" is exactly the kind of information you don't want indexable by an adversary. Welfare-check by itself (silence → contact emergency list) is a low-information-leak primitive that's still useful to anyone running an operator-assistant.

## Hard rules

1. **Real emergency contacts before enabling.** A welfare-check that fires with empty/dummy contacts is worse than no welfare-check — it'll fail silently and you'll trust it.
2. **Test in dry-run first.** `WELFARE_DRY_RUN=true python3 welfare-cascade-send.py stage 0 --hours 22` exercises the full cascade without sending anything. Run it; watch the stdout; confirm contact names + emails + phones look right.
3. **Pause it when travelling intentionally off-grid.** `bash welfare-pause.sh on` writes a sentinel; `welfare-pause.sh off` resumes. Remember to resume — a paused welfare-check silently never fires.
4. **Update emergency-contacts.json out-of-band when contacts move / change number.** Stale phones = stale escalations.

## Related files

- `scripts/welfare-cascade-send.py` — the cascade executor (stages 0-4, clear)
- `scripts/welfare-check-gather.sh` — heartbeat gather, returns `count=1` when threshold breached
- `scripts/welfare-check-replay.py` — replay tool for analyzing a fired cascade
- `scripts/welfare-pause.sh` — pause/resume toggle
- `scripts/welfare-signal-hook.sh` — UserPromptSubmit hook that updates the last-seen timestamp on principal messages
- `scheduled-tasks/welfare-check-prompt.md.template` — the reasoning prompt the dispatcher fires
- `scheduled-tasks/dormant-operator-prenudge-prompt.md.template` — earlier-stage pre-nudge prompt
- `data/emergency-contacts.template.json` — schema reference (real contacts go in the gitignored `emergency-contacts.json`)
