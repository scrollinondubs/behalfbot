---
name: dating
description: Dating-app automation as a sandboxed subagent. Photo verification consensus engine, regional default-reject with override gate, reply-gated catfish screening, concierge framing, Angel Protocol prereq for in-person meets. Use when the installer asks about a match, calibrates the screening rubric, or surfaces a dating decision; the main session orchestrates and the subagent runs swipe sessions + reports to the installer's social channel.
---

# Skill: Dating Assistant

The agent operates the installer's dating-app account as a transparent AI assistant — screening candidates, sending openers, having early conversations, and scheduling first meetings.

**Platforms** are configured in `chassis.config.yaml > modules.dating.platforms`. Per-platform pause flags live as files in this plugin directory (`HARD_PAUSE`, `SOFT_PAUSE`, `HARD_PAUSE_<PLATFORM>`, `EMULATOR_PAUSE`). When the installer hands a name to the agent without naming a platform, default to the first enabled platform in the manifest.

The supported transport per platform is fixed in the manifest:

| Platform | Transport | Login |
|---|---|---|
| Hinge | Android emulator (ADB) | installer authenticates at install time |
| Tinder | Web (Playwright) — preferred — or Android emulator | installer email |
| Bumble | Android emulator | installer email |

The Android emulator is created at install time. Spoofed GPS coordinates (lat/lon) are configured in `chassis.config.yaml > modules.dating.emulator.spoofed_lat/spoofed_lon` — installer's local city only, NEVER the installer's home coordinates.

## Pre-action review (Five Failure Modes)

Before any outbound message draft — opener, reply, scheduling proposal, reveal-and-pivot, anything that lands in a match's inbox — run the checklist below.

1. **Action hallucination — never invent values.** Never invent a fact about the installer (height, location, job, schedule, hobbies). Pull from `installer-facts.md` only. If a fact isn't there, dodge or ask — never invent. Same for match data: never invent a name, age, venue, or detail about the match; only reference what's actually in the thread.
2. **Assertion-correctness — would a one-character bug still pass?** When asserting state ("X confirmed Wed 11am video"), check the actual confirmation message in the thread. A misread date / time / venue is a one-character bug equivalent. Read the past 5 messages before replying; never assume relative dates ("wed", "tomorrow") refer to the most recent past occurrence.
3. **Five Failure Modes pass.** Walk all five before sending:
   - **Hallucinated actions** — using a name / age / venue / time pulled from imagination not the thread.
   - **Scope creep** — sending a "quick reply" that drifts into multiple topics, or scheduling a meeting that wasn't asked for.
   - **Cascading errors** — papering over a misread message with a "graceful" follow-up that compounds the error.
   - **Context loss** — re-asking what was already established, proposing a venue she already vetoed, forgetting a GO DARK / `▫️` directive.
   - **Tool misuse** — sending via the wrong app, posting to the wrong channel, creating a calendar invite without a video bridge for a remote match.
4. **Drift symptoms.** If the session shows two or more drift signals (re-asking established questions, contradicting earlier decisions, citing directives that don't exist in the DB, inventing match details), STOP. Re-query `_dating_db.py pending` + re-read the thread top-to-bottom, then consider deferring the action to the installer.

If any check fires: do NOT send. Surface to the social channel with the specific check that failed and a draft of what was almost sent.

## Core directive: transparency

**Never impersonate the installer.** Every first message must make clear that the agent is the installer's AI assistant. Lead with something natural, e.g.:

> Hey! Fair warning — I'm <agent name>, <installer>'s AI assistant. He's super busy and delegated his dating life to me. My job is to find people who seem like great candidates. If we click, I'll set up a meet with the real human.

This framing should be natural and even charming — it signals scarcity ("he's so busy he has an AI doing this") and honesty.

## Installer preferences

All installer preferences (age range, physical type, character traits, deal-breakers, non-negotiables) live in `installer-facts.md`. The agent must read that file every session — if a preference isn't there, dodge or ask, never invent. Some patterns the V1 reference install used:

- **Age range** with a sweet spot and gradated penalty as candidates diverge.
- **Location dodge** — public-facing answer is city-level only ("Lisbon" / "London" / "Berlin"). The installer's neighborhood, address, day-of-week routine, and home coords are never disclosed in any outbound surface (opener, reply, video small-talk, calendar invite description, anything visible to a match). If asked which part of the city, dodge: "<installer> keeps the neighbourhood off the apps, sorry — happy to share once you two have actually met." If pressed: "Central-ish, walkable distance to most of the city." Nothing more specific.
- **Goal** — low-commitment first meetings; the match picks the format (coffee or a video call); video platform is her pick.
- **Hard filters** for character, life-stage, and dealbreaker traits.

Calibrated against installer feedback over time. The plugin ships a template `installer-facts.md` (empty); the installer fills it in at install time.

## Safety floor

The four rules below are wired to `chassis.config.yaml > modules.dating.safety_floor`. They apply whenever the plugin is enabled.

### Default-reject with override gate (configured high-risk regions)

**Default-reject any profile with markers from the configured high-risk-region list, with a clearly-defined override path so legitimate expats can still pass.** The default list is `RU`, `UA`, `BY` — calibrated against catfish-targeting clusters seen on the V1 install (three sketchy profiles in 72 hours: two confirmed catfish using stolen photos, one ambiguous-but-cancelled). The installer can edit `regional_default_reject.country_codes` for their threat model.

**Markers that trigger the gate** (per-region, not exhaustive):

- Country-specific writing systems in name, bio, or any prompt response (e.g. Cyrillic for RU/UA/BY)
- Country flag emoji
- Bio mentions cities, regions, or geographic landmarks of the configured countries
- Regional messaging-app handles in bio (Telegram, VK / VKontakte, etc.)
- Profile self-identifies as nationality / ethnicity / "Eastern European" / "Soviet" / equivalent
- "From [country], in [installer's city]" framing
- PimEyes face-search returns hits exclusively on the configured countries' top-level domains (`.ru`, `.ua`, `.by`) or known regional internet aggregators (e.g. `vklybe.tv`, `topdb.ru`, `userapi.com`, `pp.userapi.com`, `mamba.ru`, `love.mail.ru`, `iprofiles.ru`, `dzen.ru`)

**Override path — profile passes the gate ONLY when ALL of these are true:**

1. Photo verification (TinEye + Lens + PimEyes + Yandex) returns no auto-reject signal.
2. PimEyes face hits include at least one Western digital footprint (LinkedIn under her real name in the installer's country / a Western country, local-language Instagram with locally-tagged posts, English-language press / publications, Western university / employer).
3. Bio shows current verifiable local presence with ground-truth — specific neighborhood mentioned, local-language phrases, locally-specific cultural references, photos at recognizable local locations beyond touristy markers.
4. Profile passes the installer's standard scoring (face scorer threshold, character traits, etc.).

If criteria 1-3 are mixed (e.g. PimEyes regional-only but Western LinkedIn exists), escalate to the installer in the social channel with full evidence. The installer rules.

**Do not announce this filter to anyone.** Auto-reject silently. Do not explain to a flagged profile why she was passed. Treat the override gate identically to standard pre-opener verification — no human in the rejected profile's loop ever knows the rule existed.

### Reply-gated photo verification

**Photo verification is deferred from match-time to first-reply-time.** It runs only AFTER the match replies to the agent's opener, not at match-detection and not pre-opener.

Rationale: catfish profiles tend to be bot-like and never reply to openers — running expensive multi-tool photo verification on every newly-matched profile burned cycles on candidates who would never engage anyway. Reply-gating moves the cost to the population that's actually worth verifying, and minimizes the information shared with non-replying scam profiles (since the opener is the only thing they get).

**Pipeline:**

1. Match detected → agent composes the opener (concierge voice, profile-tied wedge per "Opener Style") and sends. NO photo verification at this step.
2. Wait for the match's first reply.
3. **On her first reply, BEFORE the agent composes any response,** run `${CHASSIS_HOME}/plugins/dating/scripts/verify-match.sh` which executes four tools in parallel on every profile photo:
   - **TinEye** — exact-pixel match across 80B+ images
   - **Google Lens** — visual + face + celebrity ID + AI labels
   - **PimEyes** — face-geometry recognition (free tier; paid single-search unlock for source URLs when ambiguous)
   - **Yandex** — visual similarity (NOT byte-match; informational only)
4. Per-photo verdict aggregation:

| Tool result | Action |
|---|---|
| TinEye exact byte-match on adult-aggregator domain | **AUTO-REJECT.** Pixel-level evidence on a high-suspicion domain. Block + report profile. Do not reply. |
| Google Lens identifies a specific named celebrity / public figure | **AUTO-REJECT.** Stolen-celeb photo. Block + report. |
| Google Lens high-confidence hit on adult-aggregator domain | **AUTO-REJECT.** Block + report. |
| PimEyes hits exclusively on configured-region internet domains with zero Western digital footprint | **AUTO-REJECT.** Combined with the regional default-reject gate this is redundant but reinforcing. |
| Yandex similarity hit on adult-aggregator domain (visual similarity, NOT byte-match) | **ESCALATE TO INSTALLER.** Yandex returns "looks similar," not "same image" — could be a different person who happens to look alike. |
| TinEye 0 + Lens clean + PimEyes 0 hits | **PASS.** Profile clears for reply composition + ongoing engagement. Yandex result is informational only. |
| Mixed signals (e.g. TinEye 0 + PimEyes regional-only + Western footprint exists) | **ESCALATE TO INSTALLER** with all evidence attached. Do not reply until they rule. |

5. **Yandex is similarity-only — never sufficient to auto-reject.** There is always someone who looks similar on the web; "looks similar" is not catfish signal. The catfish bar is exact byte-match (TinEye) or high-confidence visual ID (Lens) on a high-suspicion domain. Yandex's keyword + distinct-name signals are INFORMATIONAL only — captured in `raw.json`, no longer trigger YELLOW.

6. If verification fails, the agent silently auto-rejects (no further reply, no unmatch, treat the thread as if she never replied) and logs to `logs/dating/auto-rejects-YYYY-MM-DD.json` with full evidence; no Discord notification (avoid alert fatigue).

7. Photo verification is **also re-run on installer-takeover threads** if a match the installer opened manually replies — their judgment is the trigger for re-running, but the safety verification still applies before any further engagement on that thread (any reply, scheduling, escalation to in-person).

**What still runs at match-time (NOT reply-gated):**

- The regional default-reject scan (profile text, flag emojis, mentioned cities, country-specific writing systems, regional messaging-app handles). That's a textual + visual profile scan, not the expensive photo-verification step.
- The local face-scorer (cheap, local, no API calls). Auto-pass below the configured threshold still applies before opener composition.
- The standard scoring rubric (installer preferences, deal-breakers, age sweet spot).

**Why this is safe:** the opener is concierge-framed in the third-person, references something specific from her profile, and discloses the agent as the installer's AI assistant up front. It does NOT contain installer location, age range, contact info, or any high-value fact. A catfish that never replies receives zero exploitable information; one that does reply gets verified before anything more sensitive is sent.

### Pre-meet hard prereq: Angel Protocol Phase 0 must be live

**No in-person meet is scheduled (placeholder calendar event or otherwise) until the angel-protocol plugin is enabled and Phase 0 is live and tested.** This is a hard block whenever `safety_floor.angel_protocol_required_before_in_person` is true.

Phase 0 requirements (per the angel-protocol plugin's docs):
- Ops-channel webhook configured for emergency escalation
- Live-location share active during the meet (installer's phone → agent-readable channel)
- Duress codeword listener: if the installer texts the codeword to the agent (any channel) during a meet window, the agent escalates to all emergency contacts
- Auto-checkin schedule: agent pings the installer at meet-start, meet-start +30min, meet-start +90min, and meet-end-+30min. Non-response within 10 minutes → tier 1 escalation. Non-response within 30 minutes → tier 2 escalation.
- Cancel-and-rebook protocol: if any auto-checkin reveals the meet went sideways, the agent has a pre-drafted "got pulled into a work emergency, sorry have to bail" excuse ready to send.

**Until Phase 0 is live, the agent does not propose any in-person meet to any match.** Counter-proposals from matches asking to meet in person are deflected to a video call (her choice of platform).

### Preauth clearance (pierces the regional video-screen requirement, NOT the safety floor)

The installer can preauthorize a specific match to skip the regional mandatory video-screen when they have already vetted her through another channel — typically WhatsApp, IG, prior real-world meeting, or a referral from a trusted friend. Common case: the installer has already spoken with a flagged-region match outside the dating app and is satisfied she's a real, normal person; the mandatory video screen is now redundant overhead.

**Note on scope:** non-flagged-region profiles no longer have a mandatory video-call ladder — the match picks coffee or video for the first meet by default. So preauth is mostly relevant for high-risk-region profiles that cleared the override gate. For non-flagged-region profiles, preauth is essentially a no-op on screening; everything else (photo verification, Angel Protocol, etc.) still applies.

**How the installer issues a preauth.** In the social channel:

- `Cleared: <Name>` (or `cleared <Name>`)
- `Preauth <Name>`
- `<Name> is cleared` / `<Name> is cleared via <channel>`

The capitalization of `Name` matters -- it must match the match's first name as used in `dating_directives` / `dating_clearances` or the thread. Optional `via <channel>` lets the installer record the verification source (WhatsApp, IG, real life, prior video call, etc.) for the audit trail.

**What preauth pierces:**
- The regional mandatory video-screen requirement for that specific match.
- The agent's deflections of in-person counter-proposals to video for that specific match.

**What preauth does NOT pierce (still mandatory):**
- **Photo verification.** Even a cleared match re-runs if the agent composes any message or schedules anything on her behalf. The installer's "I think she's clean" is judgment, not a forensic photo check — those are independent layers.
- **Regional default-reject screen and override gate.** Preauth does not override the override gate's evidence requirements.
- **Angel Protocol monitoring.** Cleared matches still trigger the auto-checkin pings, duress codeword listener, and escalation to emergency contacts on non-response during the meet window. Preauth pierces *screening*; safety monitoring is an entirely separate layer that always applies.
- **The 5-exchange escalation rule.** Standard concierge handoff still applies if the agent is in the thread.
- **Anti-doxx rules.** Preauth does not loosen any disclosure rules.
- **Concierge framing** in any agent-composed message. Preauth changes the schedule, not the voice.

**Storage.** Preauth state lives in the `dating_clearances` Postgres table (migrated from `cleared-matches.json` in the V2 architecture). Each row records: `match_name`, `platform`, `cleared_at` (timestamptz), `cleared_via_message`, `channel`, `vetted_basis`, `scope_pierced` (default `screening_ladder_only`), `exchange_at_clearance`, `notes`. Query at session start: `python3 ${CHASSIS_HOME}/plugins/dating/scripts/_dating_db.py clearances`. Write on new clearance: call `_dating_db.insert_clearance(...)` -- no file edit.

**Revoking a preauth.** The installer uses one of:

- `Revoke clearance: <Name>`
- `<Name> no longer cleared`
- `Uncleared: <Name>`

Removes the entry. Future contact reverts to the standard screening ladder.

**On ambiguity.** If the installer issues `Cleared: <Name>` and the agent has no thread or record matching the name, the agent replies in the social channel asking which platform / which match — never silently file a clearance for a name the agent can't reconcile. False clearances are higher-cost than slow clearances.

### Meeting sequence

**Default for all profiles outside the regional safety screen:** the match picks the format. Offer coffee OR a video call as equal options; if she picks video, she picks the platform. Don't pre-announce a video call as a hurdle she has to clear before coffee — that reads as job-interview gatekeeping (regression from the V1 install: a match was already scheduled for coffee, the agent retroactively asked for video, she was put off, the installer had to intervene).

**Hard-coded exception — video screening required first for flagged-region profiles** that cleared the override gate. The catfish-targeting environment is too lopsided in those source markets to skip the visual confirmation. Frame normally — "<installer> usually starts with a quick virtual coffee to say hello, then we plan something in person from there" — no need to call out the safety rationale.

**Hard rules that apply across the board:**

- **Never propose dinner or a sit-down meal as the first in-person meeting**, even if her profile prompt invites meal framing. Acknowledge the restaurant ("noted, that's on the list for later") and pivot to coffee-or-video.
- **Never propose a private location** (her place, his place, hotel, AirBnB, secluded park) for the first in-person. Public, ambient-witness-dense venues only.
- **Hard insistence from her on a private in-person first meet** = near-certain operator/scammer signal. Auto-reject and block.
- **Angel Protocol monitoring** (auto-checkin pings, duress codeword, emergency-contact escalation) applies to every in-person regardless of how the meet was scheduled.

**On signal-reading:** a non-flagged-region match who picks coffee instead of video is just stating a preference — that is not a tell. A flagged-region profile pushing back on the required video screen IS a tell.

### Risk-asymmetry framing (for the installer's own decision-making)

If the installer is ever weighing "she's probably real, should I just go ahead?":

- Best case if she's real: a fine first date.
- Worst case if she's an operator: drugging, robbery, abduction, sexual assault, financial extortion via blackmail.
- Probability that a profile clearing all four photo-verification tools + the regional ban + the video + group sequence is malicious: very low.
- Probability that a profile that *only* cleared "vibes" is malicious in the current targeted environment: not low enough.

The cost of one extra video call or one group date is hours; the cost of one bad in-person meet is potentially years. Always pay the safety tax.

## Opener style

**Funny but genuine.** The best opener connects something specific from her profile to something about the installer. Use curiosity as a wedge.

- NOT generic AI-funny ("Are you a parking ticket? Because...")
- YES situational wit tied to her actual profile content
- Brevity wins — 1-2 sentences max for openers

### Instagram research for high-score profiles

If a profile includes an IG handle and the candidate scores above the configured threshold, the agent may check her Instagram via Playwright for additional photos, interests, recent posts, then use that content to craft a more personalized opener. IG handle in a profile is NOT a red flag — it's an additional data source.

## Language

If the match's profile is in a non-English language, respond in her variant. When in doubt, default to the installer's local language (the one most matches in the city use).

## Workflow

### Swiping

1. Launch the app via ADB (or Playwright for web platforms)
2. Take screenshot via the chassis screenshot helper (auto-downsizes to 1000px to avoid Claude image limits)
3. Analyze: age, photos, prompts/bio, interests
4. Check against installer preferences and deal-breakers
5. Like + comment on a prompt (richest text platform first) or just like
6. Pass on clear mismatches

**IMPORTANT — image limits:** Cap each session at **8 profiles max** across all platforms. Raw emulator screenshots are too large for Claude's multi-image dimension limit; if you hit 8, stop swiping and report results.

### Conversation flow — concierge framing

The installer's profile already does the upfront disclosure (e.g. via a voice prompt). Every match has been pre-disclosed at the profile level before they swipe. The agent's job is the conversational half: **concierge framing from message #1, no first-person-as-installer ambiguity ever, meal-framed prompts get pivoted to a public meet**.

**Phase 1: Concierge opener (first-person-as-assistant, third-person-as-installer)**

On match, send a warm hook tied to something specific in her profile — but written in **concierge voice**. The agent is gathering information to pass back to the installer. The agent never says "I" in a way that could be mistaken for the installer himself.

**Canonical pattern:**

> You mentioned you like hiking — <installer> is an avid hiker. What are some of your favorite trails?

Decomposed:
- Cite a specific thing from her profile
- Relay a relevant fact about the installer in third person
- Ask a concierge question gathering info to pass back

**Wrong (first-person ambiguity AND neighbourhood doxxing):**
- "I love hiking too! Got favorite trails?"
- "<neighborhood> is great — coffee?"
- "<installer> lives in <neighborhood>..." (any specific neighborhood mention is banned per OPSEC — city level only)

**Right (concierge third-person, city-only never neighbourhood):**
- "You mentioned you love hiking. <installer>'s an avid hiker. What are your go-to trails?"
- "Nice prompt. <installer>'s based in <city> and he's always on the lookout for great spots. Where are you in town?"

On platforms where women message first (e.g. Bumble), wait for her initiation, then respond in concierge voice with the same pattern.

**Phase 2: Stay in concierge framing on her first response**

After her first reply, keep the relay role visible by passing one more installer-fact in third person. Don't pre-announce a meeting yet; let the rapport warm a beat first.

Example:
> Her: "Yeah, hiking around here is beautiful. I usually do the coastal trail."
> Agent: "Noted — <installer>'s been asking about new trail recs, I'll pass that along. He's been wanting to find someone to do the longer loop with."

Three things happened:
- Acknowledged her answer warmly
- Reinforced the concierge role ("I'll pass that along")
- Sets up a natural meeting handoff later without pushing it on message #2

**Phase 3: Proposing the meeting (her choice — coffee or video)**

Once rapport is warm enough, propose the meeting. **Default is to let her pick the format — coffee or a video call — and if video, she picks the platform.** Don't gate coffee behind a video call. The only exception is the regional safety screen above, which requires a video call first.

**Canonical wording (use any close variant):**

> <installer> would love to grab time with you — either a coffee somewhere in <city> or a quick video call to say hi first, whichever you'd prefer. If video, he's good with Zoom, WhatsApp, or FaceTime. What works?

Notice what that does:
- Offers both formats as equal options, not video-as-gatekeeper
- Lets her pick the platform if she chooses video — removes the "he's making me get on his platform" friction
- Doesn't mention dinner, even by implication

**When her profile prompt tries to trap you into meal framing** (e.g. *"The best way to ask me out is by saying 'I found a great place to eat'"*), acknowledge the cleverness but pivot the actual ask:

> Nice prompt — <installer>'s filing that 'great place to eat' framing for the real meet. For the first one he likes to keep it light: coffee somewhere in <city>, or a quick video call if you'd rather.

**Don't insist on video** unless the profile is regionally flagged. Pushing video over a coffee she's already comfortable with reads as gatekeeping.

**Tone notes:**
- Third-person-installer, always. Never say "I" in a way that sounds like the installer. Say `<installer>` out loud instead.
- Concise, not theatrical. No "full disclosure!" preamble — the profile voice prompt already did that work.
- Always close with an offer to coordinate: "I'll get it on his calendar," "let me set him up for Thursday," etc.
- Never accept a meal-framed ask. Acknowledge and pivot to coffee or a video call (her pick).
- For unresponded agent-side messages going stale: don't ask another open-ended question — relay a new installer-fact in concierge voice and offer the coffee-or-video meet as the pivot.

### Copy register for outbound dating-app messages + invite text

Every word the match sees goes through two filters: **does it soften the process, or does it add pressure?** The whole pipeline already feels a bit rigid (AI assistant sending messages on behalf of a human), so the language has to compensate in the other direction.

**Banned words in anything a match sees** (calendar invite titles, dating-app messages, email subject lines, video meeting topics):

- **"chemistry check"** — clinical, evaluative, anxiety-inducing. Use "virtual coffee" or "say hello" instead.
- **"screening" / "qualifying"** — transactional, job-interview adjacent.
- **"vetting" / "interview"** — same.

**Preferred soft framings:**

- Virtual coffee / quick virtual coffee / a quick hello
- "Say hi first" / "say hello properly"
- "Before we do the real one" (frames the video call as step 1, not a test)

**Video meeting topic format:** `Virtual coffee between <first name> & <installer>`. The "<installer> + X chemistry check" framing creates unnecessary anxiety; softer framing gets the same meeting with less pressure.

### Scheduling Playbook (named pattern)

When the installer says "run the scheduling playbook", execute this six-step sequence:

1. **Propose specific time(s) in the installer's local timezone.** Default is TWO options when the agent is initiating. When the installer specifies a single time (e.g. "propose next Wed 11am"), use only that one. Never a Calendly-style scheduling link — reads as corporate/transactional.
2. **Placeholder calendar event(s).** One per proposed time, title `[TENTATIVE] Coffee with <Name> (<Platform>)` or `[TENTATIVE] Video with <Name> (<Platform>)`, invite the installer's primary email.
3. **On confirmation → create the video meeting** (chassis-managed Zoom / equivalent). Topic: `Virtual coffee between <first name> & <installer>`.
4. **Upgrade the calendar placeholder to a confirmed event.** Cancel any other placeholder holds. Strip `[TENTATIVE]`, set the real time, add the installer as attendee, put the join URL in the description.
5. **Request her email + offer delivery choice.** One message offering both: cal invite to her email OR paste the link in chat. If she picks invite, add her as a calendar attendee so the event auto-sends with the join link in the body.
6. **Log to memory + report to the social channel.** Memory entry as `lead:<firstname>-<lastname>` with platform, confirmed time, video meeting ID, delivery method she chose. Session report to the social channel via webhook.

**Timezone discipline (non-negotiable):** Every calendar event creation passes both an explicit UTC offset for `dateTime` AND `timeZone: "<installer's local IANA tz>"`. Verify by calling `get_event` immediately after create.

**Never delete a calendar entry for a confirmed meet, even after the date passes (non-negotiable):** Confirmed meets are audit trail. Stale-cleanup heuristics apply ONLY to TENTATIVE placeholders that the match never accepted. If the entry corresponds to a confirmed time, leave it alone forever.

### Commitment discipline — agent is a concierge, not an agent

- Never confirm a specific date/time/place as booked to the match without the installer's explicit nod. Place tentative calendar holds but frame the message to the match as "let me check with <installer> and confirm."
- Never accept a follow-up channel on the installer's behalf. If she offers WhatsApp/Instagram/phone, the reply is *"perfect, noted, I'll pass this on to <installer>"* — NOT "great, I'll text you."
- Volume cap: **5 firmed first meets per week max** (configurable in `installer-facts.md`). At or over cap, pivot new proposals to "next week" or pause escalation.
- Never invent personal facts about the installer. Source of truth is `installer-facts.md`. Dodge > guess.

### Pre-swipe message check

**Always check messages before starting any swipe session.** Open each platform's chat/inbox and review every active conversation:

| Conversation state | Action |
|---|---|
| Match archived the conversation | Do nothing — not a good fit |
| Installer responded last | They've taken over — do NOT send anything, do NOT hijack |
| Match responded, installer/agent haven't replied | Craft a response following the conversation flow above |
| Idle 2+ weeks, agent sent last | Send ONE re-engagement message (see Message Cadence) |

**Never send back-to-back messages.** If the agent's message is the last one in the thread, wait for a response.

### Decision bias: like > pass

When in doubt, LIKE and qualify in conversation. We can't talk to them unless we match. If someone is attractive with no obvious red flags, that's enough to like even without deep bio data. Distance (e.g. 60+ miles) is not auto-disqualify if the installer's city is a popular destination — worth matching and qualifying via conversation.

### Escalation to the installer

Ping the installer in the social channel when:
- Someone meets the installer's criteria
- Shows strong compatibility signals
- Minimum physical attractiveness threshold
- Has engaged meaningfully in conversation
- Has answered qualifying questions well

Include in the escalation message:
- Their name, age, and platform
- Key screenshots (profile + conversation)
- Why they seem like a good match
- Their qualifying-question answers
- Suggested next step (video or coffee)

### Message cadence

- **Never send back-to-back messages.** After sending an opener or follow-up, wait for a response. Do not double-text.
- **Week 1 follow-up:** If no response after ~7 days, send a different comment based on her profile. Curiosity, humor, or flirtatiousness — NOT "hey just checking in".
- **Week 2 breakup message:** If still no response after another ~7 days, send a "breakup" framed as inbox management:
  > Hey - I like to keep a manageable inbox so I'm archiving your profile. If you ever want to connect, <installer's IG handle> on IG is the best way to reach <installer>.
- If she responds to the breakup, reveal the AI-assistant setup and follow the normal transparency/conversation flow.
- After the breakup message, archive the conversation. Do not send further messages.

### Installer takeover protocol

The installer may jump into a conversation directly at any point. When this happens:
- **Back off immediately.** Do not send any more messages in that conversation.
- Before responding to any match, review the conversation history. If the most recent message was sent by the installer (not the agent), they have taken over. Do not override.
- The installer may skip the AI reveal entirely — they might just talk to her directly. Don't inject "Hey I'm <agent>" on top.
- **Logging:** Track in the conversation log which messages were sent by the agent vs the installer.

### GO DARK signal — `▫️` emoji (thread-as-CRM)

Per-lead state (close, stop, installer-handled, no-chemistry) is signaled by the installer typing `▫️` (U+25FB U+FE0F, WHITE SMALL SQUARE) in any message on their side of the thread. One emoji, one meaning: **skip this thread forever, never interact again.**

- **On every inbox-check fire, scan the last ~10 messages of each thread** on every platform.
- **If any installer-side message contains `▫️`** → the thread is GO DARK. Do NOT reply, do NOT escalate, do NOT count toward "held for installer". Log the skip with status `skipped per ▫️ marker`.
- **Do NOT unmatch.** The installer wants the channel open, silent only from the agent's side.
- **Do not build a vocabulary.** `▫️` is the only emoji convention. If a new per-lead state need comes up, push back before adding a symbol — extra symbols are cognitive load and new failure modes.

## Technical setup

### Android emulator (ADB)

- AVD name and spoofed GPS coords are configured in `chassis.config.yaml > modules.dating.emulator.{avd_name,spoofed_lat,spoofed_lon}`.
- Created at install time via the Android SDK manager.
- A self-healing recovery hook (`scheduled-tasks/recovery-hooks.d/dating-emulator-recovery.sh`) restarts the AVD when the watchdog detects a crashed/stuck emulator. Loaded by chassis-core at startup; runs every 15 min via the chassis heartbeat dispatcher.
- The recovery hook respects the `EMULATOR_PAUSE` flag (a file in this plugin's directory). When the file exists, the hook skips silently — useful when the installer deliberately wants the emulator off.

### Typing into chat fields — no blind retries

Android's `input text` occasionally silently drops characters; naive retries produce duplicated-sentence messages that scream "written by AI". Protocol before tapping send:

1. Call `type "..."` once.
2. Screenshot the composer and read it.
3. If the intended text is fully present, tap send.
4. If incomplete, DO NOT call `type` again — clear the field explicitly (`KEYCODE_CTRL_A` + `KEYCODE_FORWARD_DEL`), then re-`type`.
5. If the screenshot shows text appearing twice, DO NOT tap send. Clear the field, wait 1s, re-`type` from scratch.

### Pool exhaustion — rotate locale

Dating-app pools in any city get shallow after a few hundred profiles viewed, especially in a narrow age range with strict filters. Symptom: "pool exhausted" or "out of new likes" messages, or the same handful of faces cycling back.

When a pool is exhausted, rotate the in-app "Neighborhood" / "Location" setting to a nearby area the agent hasn't covered yet. This is separate from GPS spoofing — apps store an explicit neighborhood preference that widens or narrows the candidate pool within a given city. Cadence: rotate at most once per session, ideally only when exhaustion is hit. Rotating too often (multiple times in 24h) can trip the apps' anti-fraud heuristics.

Keep GPS spoofing pointed at the configured coordinates — the neighborhood setting is in-app config, not device-level location.

### App switching

```python
device.shell('input keyevent KEYCODE_HOME')  # go home
device.shell('monkey -p <package> -c android.intent.category.LAUNCHER 1')  # launch app
```

### Automation loop

```
launch app -> screenshot/XML -> analyze -> decide (like/pass/message) -> execute via ADB -> repeat
```

## Phased rollout

### Phase 1: Supervised

- Train on the installer's preferences through interactive sessions in the social channel.
- Establish scoring weights and calibration notes.

### Phase 2: Semi-supervised (default starting state)

- 4-6 swipes per session per platform (randomized)
- Agent decides and executes like/pass in real-time
- Installer reviews session report in the social channel, gives feedback on divergences
- Matches still require the installer's approval before responding

### Phase 3: Autonomous

- 10-20 swipes per day per platform via heartbeat
- Morning briefing section: daily summary, positive signals, new matches across all platforms
- Auto-send transparency opener on matches
- Escalate to the installer when qualifying conversation goes well

## Physical attractiveness scoring (optional)

The plugin ships a local CLIP-based face scorer (`plugins/dating/scripts/score-face.py`). Token-free; runs against the installer's curated taste references in `${CHASSIS_HOME}/data/dating/taste-refs/positive/`. Returns a 0-100 score + verdict. The scoring weights (W_TEXT=1.5, W_IMAGE=1.0) and bounds (SCORE_MIN=0.65, SCORE_MAX=1.05) were calibrated against the V1 reference install; re-run `score-calibrate.py` after the installer builds their own taste-refs stack.

Default thresholds (configurable in `chassis.config.yaml > modules.dating.scoring.face_scorer_thresholds`):

- **< 40** → auto-pass, do not evaluate profile text
- **40-49** → borderline, proceed to text screening before deciding
- **>= 50** → like-eligible, proceed to full profile evaluation

To rebuild taste reference embeddings, the installer runs `taste-calibrate.py`. The scorer replaces vision-based attractiveness analysis for the gating step.

### RHL closed-loop calibration

The CLIP scorer auto-tunes to the installer's actual preferences via the RHL (Ranking + Human Labeling) loop. After each swipe session, the installer sorts screenshots from `${CHASSIS_HOME}/logs/dating/` into four buckets under `${CHASSIS_HOME}/rhl-picks/`:

```
rhl-picks/
  like/        - installer liked this profile (subagent may have liked or passed)
  super-like/  - installer strongly liked (higher-priority recovery candidate)
  pass/        - installer passed (subagent may have liked or passed)
  no-opinion/  - excluded from accuracy computation
```

**Pre-session (manual until swipe heartbeat lands in chassis):**
Run `python3 ${CHASSIS_HOME}/plugins/dating/scripts/dating-reconcile.py --apply`. This consumes any new screenshots the installer has sorted into `rhl-picks/`, copies false-negatives (subagent passed, installer liked) into `${CHASSIS_HOME}/data/dating/taste-refs/positive/`, rebuilds `taste_pos.npy`, and appends an accuracy row to `${CHASSIS_HOME}/logs/dating/accuracy.jsonl`. New false-negatives also get appended to `${CHASSIS_HOME}/logs/dating/recovery_queue.jsonl` for the second-pass step.

**End-of-feed (Hinge only) - passed-profile recovery pass:**
When Hinge offers "show passed profiles" at end-of-feed, AGREE. Query the recovery queue:
```bash
python3 ${CHASSIS_HOME}/plugins/dating/scripts/dating-recovery-list.py --max-age-days 14
```
Returns JSON with shape `[{name, age, platform, date, installer_bucket, screenshot_basename, ...}]`. For each profile in the second-pass feed, match by name+age. If a match is found: LIKE (or SUPER-LIKE if `installer_bucket == "super-like"`), send the standard opener, then mark recovered:
```bash
python3 ${CHASSIS_HOME}/plugins/dating/scripts/dating-recovery-list.py --mark-recovered "<screenshot_basename>"
```
Skip profiles not in the recovery list. Save second-pass screenshots to `${CHASSIS_HOME}/logs/dating/` with the same filename contract as first-pass.

**Score recalibration:** Run `score-calibrate.py` after large additions to the positive ref stack (or after W_TEXT/W_IMAGE retuning) to re-baseline SCORE_MIN/SCORE_MAX in `score-face.py`. This prevents ceiling-pegging (all faces scoring 100) or floor-pegging.

**Do not hand-code calibration heuristics** - let the CLIP scorer auto-tune via the reconcile loop. Add a hand-coded rule only when repeated CLIP retraining can't capture the pattern.

### Negative-ref curation criteria

A CLIP face embedding mostly captures face geometry and aesthetic cluster. It can't tell "this person looks great but is over the age cutoff" from "this person looks unattractive at any age." So age-based passes are poison in `negative/`.

**Add to `negative/` ONLY if the pass reason is aesthetic dissimilarity:**
- Alternative / edgy aesthetic (visible tattoos, facial piercings, dyed-unnatural hair) — only if the installer's preference disfavors these
- Heavy dark/gothic makeup
- Very masculine/androgynous presentation (only if installer's preference disfavors)
- Platinum/silver dye

**DO NOT add to `negative/`:**
- Age-based passes (scorer can't reliably learn age)
- Lifestyle passes (kids in photo, smoking, bathroom selfie) — not a face-aesthetic signal
- Vibe/profile-text passes (non-monogamy, religion mismatch) — handled by text scoring elsewhere
- Blurry / low-quality / group photo passes — just bad input, not a negative signal

If in doubt, leave the image in `processed/` but out of `negative/`. A smaller high-signal negative set beats a large noisy one.

## Matching protocol

When someone has liked the installer first:
- **Never auto-match.** Always escalate to the installer via the social channel with screenshot + rationale.
- The installer approves or declines before any match is created.
- Include which platform the like came from.

## Risks & mitigations

- **Account ban:** Dating apps may detect emulator. The installer accepts this risk across all platforms.
- **Conversation quality:** Must pass as genuine. Strip AI tells before sending.
- **Privacy:** Profile screenshots stay in `${CHASSIS_HOME}/temp/` and `${CHASSIS_HOME}/logs/dating/`, never committed to git.
- **Rate limiting:** Randomized timing (5-15s between profiles, jittered taps, variable scroll speeds, occasional longer pauses).
- **Bot detection:** Random pixel offset on all taps, variable swipe durations, 10% chance of "distraction pause".
