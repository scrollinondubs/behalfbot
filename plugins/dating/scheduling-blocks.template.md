# Scheduling Blocks

When the installer says a window is blocked, bake it here. The dating subagent reads this file every run and MUST NOT propose dates inside a blocked window.

## Format

Each entry is one block. Include: dates (absolute, ISO where possible), reason, who set it, when. Delete entries once the blocked window has passed.

## Example block (delete on first commit)

### 2026-01-20 — 2026-01-22 — Travel: <city>

- **Blocked:** all date proposals (in-person AND video) for these dates.
- **Reason:** travel; offline Sat/Sun.
- **Set by:** installer in social-channel message <message_id>.
- **Lift condition:** auto-expire on 2026-01-23 (delete entry then). The installer can delete sooner if plans change.
- **Action when a match asks "are you free this weekend?":** propose times the following week.

### Until further notice — In-person meets blocked until Angel Protocol Phase 0 is live

- **Blocked:** all in-person meet proposals, time-suggestions, calendar-placeholder creations, and reveal-and-pivot asks that target a physical location. **Video proposals are still allowed.**
- **Reason:** safety floor — any in-person meet requires the angel-protocol plugin to be enabled and Phase 0 monitoring (live-location share + duress codeword listener + auto-checkin schedule + emergency-contact escalation) wired and tested. See `chassis.config.yaml > modules.dating.safety_floor.angel_protocol_required_before_in_person`.
- **Lift condition:** the angel-protocol plugin is enabled, Phase 0 is live, and the installer confirms in the social channel that in-person meets are unblocked. Until then, ALL meet proposals deflect to video-first regardless of how warm the thread is.
- **Action when a match asks to meet in person:** "Let's do a quick video call first — <installer> does that with everyone before in-person. 30 minutes, low pressure, just to put a real human to the messages. After that we'll set up the real-world hang." If she pushes back hard on video, treat it as a possible operator/scammer signal — auto-reject after the installer reviews.
- **Action if a placeholder calendar invite already exists for an in-person meet:** cancel the invite + send a rebook message proposing video instead.

## Active blocks

(empty by default — installer adds blocks over time)

## How the subagent applies this

At the start of each swipe/reply session:

1. Read this file.
2. For every active block, compute the date range in the installer's local timezone.
3. When drafting any message that proposes a time, verify the proposed datetime does NOT fall inside any active block.
4. When creating a calendar placeholder, verify the same.
5. If a match asks "are you free this weekend?" inside a blocked window, reply with the next-available proposal rather than literally answering the weekend question.

## Precedence

Scheduling blocks in this file OVERRIDE anything else in the dating pipeline — skills, memory, prior conversation context. When in doubt, do not propose the blocked slot and flag to the installer via the social channel.
