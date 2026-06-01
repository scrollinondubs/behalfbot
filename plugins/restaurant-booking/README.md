# restaurant-booking - Behalf.bot Chassis Plugin

A TheFork-based restaurant booking plugin for the Behalf.bot chassis pattern. Handles
the full booking flow: parse free-text intent, drive TheFork via Playwright, soft-confirm
with the operator via Discord, submit the booking, and optionally create a Google Calendar event.

**Aggregator:** TheFork (`thefork.com` / `thefork.pt`)
**Target locale:** Lisbon (works anywhere TheFork operates)

---

## Directory structure

```
plugins/restaurant-booking/
├── README.md                         # this file
├── skill.md                          # Claude skill file: invocation rules + step-by-step
├── scripts/
│   ├── parse-booking-intent.py       # free-text -> structured JSON via Claude Haiku
│   ├── book-restaurant.py            # Playwright-driven TheFork booking flow
│   ├── confirm-via-discord.py        # Discord soft-confirm + reaction polling
│   └── create-calendar-event.py      # Google Calendar event on confirmed booking
├── config/
│   └── restaurant-booking.yaml       # per-installer settings
├── tests/
│   └── test_parse_intent.py          # unit tests (mocked Haiku API)
└── logs/                             # per-booking audit logs + screenshots (gitignored)
```

---

## Prerequisites

### 1. TheFork credentials in Vaultwarden

Add a Vaultwarden item named `thefork-credentials`:
- **username**: your TheFork email
- **password**: your TheFork password

The booking script fetches these fresh at invocation time and never persists them.

### 2. Playwright / Node.js

The booking script uses Playwright via Node.js. Install if not present:

```bash
npm install -g playwright
npx playwright install chromium
```

Verify: `npx playwright --version`

### 3. Python 3 + requests

```bash
python3 -m pip install requests
```

All other dependencies are stdlib.

### 4. Google Calendar (optional)

To enable automatic calendar events after booking:

1. Create/use a Google Cloud project with Calendar API enabled
2. Create OAuth2 credentials (Desktop app type) from the Google Cloud Console
3. Add to `$CHASSIS_HOME/.env`:
   ```
   GOOGLE_CLIENT_ID=<your-client-id>
   GOOGLE_CLIENT_SECRET=<your-client-secret>
   ```
4. Run once to authorize:
   ```bash
   python3 plugins/restaurant-booking/scripts/create-calendar-event.py --setup
   ```

If not set up, the booking completes without a calendar event (non-fatal).

---

## Quick start

### Dry-run smoke test (no actual booking)

```bash
python3 plugins/restaurant-booking/scripts/book-restaurant.py \
  --restaurant-url "https://www.thefork.com/restaurant/contrabando-restaurante-e-bar-saldanha-r832103" \
  --restaurant-name "Contrabando Saldanha" \
  --datetime "2026-05-15T13:00:00+01:00" \
  --party-size 4 \
  --dry-run
```

Expected output (exit code 3):
```json
{
  "dry_run": true,
  "restaurant": "Contrabando Saldanha",
  "restaurant_url": "https://www.thefork.com/...",
  "datetime": "2026-05-15T13:00:00+01:00",
  "party_size": 4,
  "screenshot_path": "plugins/restaurant-booking/logs/booking-...-preconfirm.png",
  "status": "dry_run_complete"
}
```

Check the screenshot to verify the TheFork form loaded correctly.

### Intent parser test

```bash
python3 plugins/restaurant-booking/scripts/parse-booking-intent.py \
  "book Contrabando Saldanha for 4 people tomorrow at 1pm"
```

Expected output:
```json
{
  "restaurant_name": "Contrabando Saldanha",
  "restaurant_url_hint": null,
  "datetime_iso": "2026-05-15T13:00:00+01:00",
  "party_size": 4,
  "notes": null,
  "intent_confidence": 0.92
}
```

### Full booking flow (real booking)

Invoked by the ${ASSISTANT_NAME} orchestrator when Sean says "book me [restaurant] for [time] [party]" in Discord.
See `skill.md` for the orchestration logic.

---

## Booking flow detail

1. **Credential fetch** - pulls TheFork username + password from Vaultwarden at start; never cached
2. **Browser launch** - headless Chromium via Playwright
3. **Login** - navigates to `thefork.com/signin`, fills credentials, submits
4. **Restaurant navigation** - goes to the provided TheFork URL
5. **Form fill** - sets party size, date, and time slot (finds nearest within 30 min if exact not available)
6. **Pre-confirm screenshot** - captures form state before submission
7. **Discord soft-confirm** - sends screenshot + summary; adds checkmark/X reactions; polls 10s intervals
8. **On confirm (checkmark)** - clicks the final reserve button; captures confirmation # + screenshot
9. **On abort (X or timeout)** - closes browser, logs abort
10. **Calendar event** - creates Google Calendar event on success (if configured)

---

## Troubleshooting

**"Could not fetch TheFork credentials from Vaultwarden"**
- Verify VW item `thefork-credentials` exists with `username` and `password` fields
- Check VW session: `bw status`

**"Playwright booking script exited with code 1"**
- Check the error screenshot: `plugins/restaurant-booking/logs/booking-*-error.png`
- TheFork selectors may have changed. The script tries multiple selector patterns.
- Try running with headful (non-headless) mode by editing `headless: true` to `false` in the generated script

**"Google Calendar not authorized"**
- Run: `python3 plugins/restaurant-booking/scripts/create-calendar-event.py --setup`

**"DISCORD_BOT_TOKEN not found"**
- Ensure Vaultwarden item `discord-bot-token` is accessible via `bw-fetch.sh`

---

## Configuration

Edit `config/restaurant-booking.yaml` to change:
- `discord_channel_id` - where soft-confirm messages are posted
- `confirm_timeout_seconds` - how long to wait for Sean's reaction (default: 600)
- `time_slot_tolerance_minutes` - nearest-slot search window (default: 30)
- `vaultwarden_item_name` - VW item for TheFork credentials
- `gcal_calendar_id` - which calendar to add events to

---

## Chassis portability

This plugin has no hard-wired <assistant>-specific dependencies beyond:
- `scripts/_loadenv.py` - the env/VW loader (parameterized path)
- `scripts/bw-fetch.sh` - Vaultwarden credential fetcher

Both exist in the chassis base. When porting via `git mv`, update the
`REPO = Path(...)` references in each script to point to the new chassis root.

---

## V1 scope (implemented)

- TheFork (Lisbon-strong) via Playwright
- Free-text intent parsing via Claude Haiku
- Discord soft-confirm with screenshot + reaction polling
- Google Calendar event on success
- Dry-run mode for smoke testing
- Unit tests for intent parser (mocked)
- Gitignored logs + screenshots

## Out of scope (V1 deferred)

- Phone-call automation
- Multi-restaurant comparison
- Payment-on-file / auto-pre-pay
- Auto-book threshold (always soft-confirm in V1)
- Non-TheFork restaurants
- Heartbeat / scheduled triggers (user-triggered only)
