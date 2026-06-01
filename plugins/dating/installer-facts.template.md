# Installer Facts (Dating KB)

Canonical source of truth for facts about you (the installer) that the agent may relay to a match. Read every dating session. If a fact isn't here, the agent dodges or asks — never invents.

**Fill this in at install time. Each section is a category the agent will reference when composing concierge-framed openers, replies, and small-talk for video calls / coffee.**

## Identity

- **Name:** <your first name>
- **Age:** <int>
- **Pronouns:** <he/she/they>
- **City (public-facing):** <city only — never neighborhood, address, or coords>
- **Languages:** <comma-separated, list confidence levels>

## Physical

- **Height:** <e.g. 6'2 / 188cm>
- **Build:** <one short adjective>
- **Notable features the agent may relay if asked:** <e.g. "tall, brown hair" — keep it generic enough that a stranger couldn't identify you from a description>

## Work / lifestyle

- **What you do (high level):** <one sentence; no employer, no exact title>
- **Schedule patterns the agent should know:** <e.g. "evenings free Mon-Thu after 7pm"; "weekends mostly free"; "occasional travel" — agent uses this for scheduling proposals only>
- **Day-of-week routine the agent must NOT reveal:** <e.g. "always at gym Tue/Thu 6am"; "co-working space M/W/F" — list anything that could be used to narrow your physical location at a known time>

## Interests / hobbies

(populate freely — these are wedge material for openers and replies)

- <interest 1>
- <interest 2>
- <interest 3>

## Preferences (for screening)

- **Age range:** <X-Y>
- **Age sweet spot:** <X-Y>; gradated penalty as candidates diverge
- **Character traits (must-haves):** <bullet list>
- **Deal-breakers:** <bullet list>
- **Non-negotiables:** <bullet list — chemistry, integrity, willingness to live in your city, etc.>

## Boundaries

- **Volume cap:** <N firmed first-meets per week max>
- **Goal:** <e.g. "low-commitment first meetings; the match picks coffee or video">
- **Never:** dinner first; private location first; dropping your home neighborhood; sharing your phone number; sharing your IG handle without explicit nod (the agent uses your IG only in the breakup message — see skill).

## OPSEC dodges

- **If asked which neighborhood:** "I keep the neighbourhood off the apps, sorry — happy to share once you two have actually met."
- **If pressed:** "Central-ish, walkable distance to most of the city." Nothing more specific.
- **If asked phone number / WhatsApp / IG before meeting:** "I'll pass it on to <installer> and let them decide." Never give it out.
- **If asked your exact job / employer:** "<role-or-industry> — happy to get into it more once we meet."

## Voice prompt (if applicable)

If your dating profile uses a voice prompt to disclose the AI assistant, paste the canonical text here so the agent can match the wording in messages:

> <voice prompt text — the agent will reference this when introducing itself>

## Pronoun handling

The agent always uses third-person about you in messages. Decide on the canonical pronoun and stick to it. If you're using a name in lieu of pronouns ("Jane is curious...") rather than ("She's curious..."), note that here.

---

This file is read by the dating subagent at the start of every session. Edit freely; the agent picks up changes on the next heartbeat fire. Never commit this file to a public repo — `.gitignore` should cover `plugins/dating/installer-facts.md`.
