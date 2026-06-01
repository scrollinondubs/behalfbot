---
name: restaurant-booking
description: >
  Book a restaurant table via TheFork (Lisbon-strong). Triggers on natural-language
  booking requests from Discord. Handles the full flow: parse intent, drive TheFork
  via Playwright, soft-confirm with Sean via Discord reaction, submit booking, and
  optionally create a Google Calendar event.
---

# Restaurant Booking Plugin

## When to invoke

Invoke this skill when Sean says anything in Discord like:

- "book me a table at [restaurant] for [time] [party]"
- "reserve [restaurant] tomorrow at 1pm for 4"
- "can you book Contrabando Saldanha for lunch Friday, party of 6"
- "book the Contrabando https://www.thefork.com/restaurant/... for 8 people at 13:00"

Do NOT invoke for:
- Restaurants not on TheFork (redirect to Sean with the restaurant's direct website)
- Past dates
- Requests where intent_confidence < 0.7 (ask for clarification first)

## Step-by-step execution

### 1. Parse the intent

```bash
python3 $CHASSIS_HOME/plugins/restaurant-booking/scripts/parse-booking-intent.py \
  "<Sean's free-text message>"
```

Parse the JSON output. If `intent_confidence < 0.7`, reply to Sean asking for
clarification before continuing. Do NOT proceed with a low-confidence parse.

Example clarification message:
> I can see you want to book somewhere, but I'm not sure about the date/time/party size.
> Could you rephrase? e.g. "book Contrabando Saldanha for 4 tomorrow at 1pm"

### 2. If restaurant_url_hint is null, confirm the URL with Sean

TheFork has two Contrabando locations. When `restaurant_url_hint` is null and
multiple locations might match the name:

Reply to Sean with the options and ask which one:
> "Found two Contrabando locations on TheFork. Which one?
>   A: Saldanha - https://www.thefork.com/restaurant/contrabando-restaurante-e-bar-saldanha-r832103
>   B: 24 de Julho - https://www.thefork.com/restaurant/contrabando-restaurante-e-bar-24-de-julho-r362875"

Sean replies "A" or "B" (or pastes a URL) - then proceed.

### 3. Run the booking flow

```bash
python3 $CHASSIS_HOME/plugins/restaurant-booking/scripts/book-restaurant.py \
  --restaurant-url "<THEFORK_URL>" \
  --restaurant-name "<NAME>" \
  --datetime "<ISO8601>" \
  --party-size <N> \
  [--notes "<special requests>"] \
  [--dry-run]
```

This script:
- Fetches TheFork credentials from Vaultwarden (`thefork-credentials` item)
- Launches headless Chromium, logs in, navigates to the restaurant
- Selects date, time, and party size
- Screenshots the pre-confirm form
- Calls `confirm-via-discord.py` (sends screenshot + summary to Discord, waits for reaction)
- On Sean's checkmark reaction: clicks Confirm, captures confirmation number
- On X reaction or timeout: aborts

### 4. Report outcome

On success, report back to Sean in Discord:
> "Booked! Contrabando Saldanha on Thursday 15 May at 13:00, party of 4. Confirmation: #ABC123."

On abort:
> "Booking aborted. No reservation made."

On TheFork error (restaurant not found, no slots available):
> "TheFork couldn't complete the booking: [reason]. You can book directly at [URL]."

## Dry-run smoke test (before real use)

To test the flow without actually booking:

```bash
python3 $CHASSIS_HOME/plugins/restaurant-booking/scripts/book-restaurant.py \
  --restaurant-url "https://www.thefork.com/restaurant/contrabando-restaurante-e-bar-saldanha-r832103" \
  --restaurant-name "Contrabando Saldanha" \
  --datetime "2026-05-15T13:00:00+01:00" \
  --party-size 4 \
  --dry-run
```

Exit code 3 = dry run complete. Check the screenshot saved to
`plugins/restaurant-booking/logs/booking-*-preconfirm.png`.

## Google Calendar setup (one-time, optional)

To enable automatic calendar event creation after a booking:

1. Create a Google Cloud project (or use the existing ${ASSISTANT_NAME} project)
2. Enable the Google Calendar API
3. Create OAuth2 credentials (Desktop app type)
4. Add `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` to $CHASSIS_HOME/.env
5. Run the one-time authorization:
   ```bash
   python3 $CHASSIS_HOME/plugins/restaurant-booking/scripts/create-calendar-event.py --setup
   ```

If calendar is not set up, the booking still completes - the calendar step is
non-fatal and Sean can create the event manually.

## Vaultwarden prerequisite

The booking flow requires a Vaultwarden item named `thefork-credentials` with:
- **username**: Sean's TheFork email address
- **password**: Sean's TheFork password

Sean adds this item himself in Vaultwarden. The plugin never stores or logs credentials.

## Known limitations (V1)

- TheFork UI changes can break Playwright selectors. If the booking fails with a
  Playwright error, check `logs/booking-*-error.png` for a screenshot of what
  went wrong.
- Exact time slot availability depends on TheFork's widget. If the requested time
  is not available, the script finds the nearest slot within 30 minutes and surfaces
  both options in the soft-confirm message.
- The `--dry-run` screenshot shows the form state BEFORE time/date/party-size are
  submitted (depends on TheFork's SPA update cycle). The screenshot may look different
  from the final confirmed form.
- Google Calendar event creation requires one-time OAuth setup (see above).

## Future chassis port

This plugin directory is structured to be cleanly portable to `scrollinondubs/behalfbot`
via `git mv plugins/restaurant-booking/ chassis/plugins/restaurant-booking/`.
No <assistant>-specific deps other than `scripts/_loadenv.py` (path is parameterized).
