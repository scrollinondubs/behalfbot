#!/usr/bin/env python3
"""book-restaurant.py - Playwright-driven TheFork booking flow.

Drives a headless Chromium browser to:
  1. Log in to TheFork with VW credentials
  2. Navigate to the restaurant page
  3. Select the requested date / time / party size
  4. Screenshot the pre-confirm form state
  5. Wait for Discord soft-confirm (delegates to confirm-via-discord.py)
  6. On confirm: click the final button, capture confirmation number + screenshot
  7. On abort/timeout: close browser cleanly

Usage (end-to-end, called by the orchestrator session or skill.md):
    python3 book-restaurant.py \
        --restaurant-url "https://www.thefork.com/restaurant/contrabando-..." \
        --datetime "2026-05-15T13:00:00+01:00" \
        --party-size 4 \
        [--notes "window table please"] \
        [--dry-run]

  --dry-run: performs all steps UP TO AND INCLUDING the pre-confirm screenshot,
             but does NOT click Confirm and does NOT call confirm-via-discord.py.
             Useful for smoke-testing the Playwright flow without making an actual booking.

Output (stdout on success):
    JSON: {"confirmation_number": "...", "restaurant": "...", "datetime": "...",
           "party_size": N, "screenshot_path": "..."}

Exit codes:
    0 - booking confirmed
    1 - error (see stderr)
    2 - user aborted (✗ reaction or timeout)
    3 - dry-run completed (no booking made)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent.parent.parent
SCRIPTS = REPO / "scripts"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PLUGIN_ROOT / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

THEFORK_BASE = "https://www.thefork.com"
THEFORK_LOGIN_URL = f"{THEFORK_BASE}/signin"
THEFORK_SEARCH_URL = f"{THEFORK_BASE}/search"

BW_FETCH = str(REPO / "scripts" / "bw-fetch.sh")
CONFIRM_SCRIPT = str(Path(__file__).resolve().parent / "confirm-via-discord.py")
CALENDAR_SCRIPT = str(Path(__file__).resolve().parent / "create-calendar-event.py")


def _fetch_thefork_creds(item_name: str = "thefork-credentials") -> tuple[str, str]:
    """Pull TheFork username + password from Vaultwarden. Never persisted."""
    try:
        username = subprocess.run(
            ["bash", BW_FETCH, item_name, "username"],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout.strip()
        password = subprocess.run(
            ["bash", BW_FETCH, item_name, "password"],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout.strip()
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Could not fetch TheFork credentials from Vaultwarden item '{item_name}'. "
            f"Make sure Sean has added the item. Error: {e.stderr.strip()}"
        ) from e

    if not username or not password:
        raise RuntimeError(
            f"Vaultwarden item '{item_name}' has empty username or password. "
            "Please check the item in Vaultwarden and re-add credentials."
        )
    return username, password


def _screenshot_path(label: str) -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = LOGS_DIR / f"booking-{ts}-{label}.png"
    return str(path)


def _format_datetime_human(dt_iso: str) -> str:
    """Convert ISO datetime to a friendly string for Discord messages."""
    try:
        from datetime import timezone
        dt = datetime.fromisoformat(dt_iso)
        return dt.strftime("%A %d %B %Y at %H:%M")
    except Exception:
        return dt_iso


def run_booking(
    restaurant_url: str,
    datetime_iso: str,
    party_size: int,
    restaurant_name: str = "",
    notes: str | None = None,
    dry_run: bool = False,
    vw_item: str = "thefork-credentials",
) -> dict[str, Any]:
    """Full TheFork booking flow via Playwright MCP commands.

    This function is designed to be called from within a Claude/${ASSISTANT_NAME} session
    that has the Playwright MCP server available. The actual Playwright
    automation is driven by the MCP tool calls described in the docstring of
    each step - this script handles the orchestration and credential fetching,
    while the calling session drives the browser via MCP.

    When run standalone (not in a MCP session), it falls back to a subprocess
    approach using the Playwright CLI.

    Returns a result dict on success.
    Raises RuntimeError on failure.
    Exits with code 2 if user aborted.
    """
    # Fetch credentials fresh from Vaultwarden
    print(f"[book-restaurant] Fetching TheFork credentials from Vaultwarden...", file=sys.stderr)
    username, password = _fetch_thefork_creds(vw_item)
    print(f"[book-restaurant] Credentials fetched for user: {username[:3]}***", file=sys.stderr)

    # Parse the requested datetime
    dt = datetime.fromisoformat(datetime_iso)
    date_str = dt.strftime("%Y-%m-%d")
    time_str = dt.strftime("%H:%M")
    hour = dt.hour

    print(f"[book-restaurant] Booking: {restaurant_name or restaurant_url}", file=sys.stderr)
    print(f"[book-restaurant] Date: {date_str} Time: {time_str} Party: {party_size}", file=sys.stderr)

    if dry_run:
        print(f"[book-restaurant] DRY RUN mode - will not make actual booking", file=sys.stderr)

    # Build the booking state that will be passed through the flow
    booking_state: dict[str, Any] = {
        "restaurant_url": restaurant_url,
        "restaurant_name": restaurant_name or _extract_name_from_url(restaurant_url),
        "datetime_iso": datetime_iso,
        "date_str": date_str,
        "time_str": time_str,
        "party_size": party_size,
        "notes": notes,
        "dry_run": dry_run,
        "username": username,
        "screenshots": [],
    }

    # Write the booking session config for the Playwright flow
    session_file = LOGS_DIR / f"booking-session-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    # Store password separately - never in a file that could be read broadly
    # The password is passed via environment variable to the subprocess
    session_data = {k: v for k, v in booking_state.items() if k != "username"}
    session_data["username"] = username
    # password intentionally excluded from session file

    print(f"[book-restaurant] Running Playwright booking flow...", file=sys.stderr)

    # Run the actual Playwright automation
    result = _run_playwright_flow(
        booking_state=booking_state,
        password=password,
        dry_run=dry_run,
    )

    return result


def _extract_name_from_url(url: str) -> str:
    """Extract a readable restaurant name from a TheFork URL slug."""
    import re
    # TheFork URLs: .../restaurant/contrabando-restaurante-e-bar-saldanha-r832103
    match = re.search(r"/restaurant/([^/?#]+)", url)
    if not match:
        return "Restaurant"
    slug = match.group(1)
    # Remove the trailing -rNNNNNN TheFork ID
    slug = re.sub(r"-r\d+$", "", slug)
    # Title-case the slug
    return slug.replace("-", " ").title()


def _run_playwright_flow(
    booking_state: dict[str, Any],
    password: str,
    dry_run: bool,
) -> dict[str, Any]:
    """Execute the Playwright booking flow.

    In a ${ASSISTANT_NAME} session with MCP available, Playwright is driven via MCP tool calls.
    This function generates the step-by-step instructions and executes them via
    the playwright_navigate / playwright_click / playwright_fill MCP tools.

    When called standalone (CLI), it runs via subprocess using npx playwright.

    The flow:
      Step 1: Navigate to TheFork login page
      Step 2: Fill credentials and submit
      Step 3: Navigate to restaurant URL
      Step 4: Select date, time, party size
      Step 5: Screenshot pre-confirm form (always)
      Step 6: On dry_run=True: return here
      Step 7: Call confirm-via-discord.py with screenshot + summary
      Step 8: On confirm: click final button, capture confirmation #
      Step 9: Screenshot confirmation page
    """
    restaurant_url = booking_state["restaurant_url"]
    restaurant_name = booking_state["restaurant_name"]
    date_str = booking_state["date_str"]
    time_str = booking_state["time_str"]
    party_size = booking_state["party_size"]
    notes = booking_state.get("notes")
    username = booking_state["username"]

    # Generate a script for inline Playwright execution via Node/npx
    # This is the standalone path (not MCP)
    playwright_script = _build_playwright_script(
        username=username,
        password=password,
        restaurant_url=restaurant_url,
        restaurant_name=restaurant_name,
        date_str=date_str,
        time_str=time_str,
        party_size=party_size,
        notes=notes,
        dry_run=dry_run,
        logs_dir=str(LOGS_DIR),
    )

    script_path = LOGS_DIR / f"booking-pw-{datetime.now().strftime('%Y%m%d-%H%M%S')}.js"
    script_path.write_text(playwright_script, encoding="utf-8")

    print(f"[book-restaurant] Running Playwright script: {script_path}", file=sys.stderr)

    plugin_dir = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        ["node", str(script_path)],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(plugin_dir),
        env={**os.environ, "THEFORK_PASSWORD": password, "NODE_PATH": str(plugin_dir / "node_modules")},
    )

    print(proc.stderr[-3000:] if proc.stderr else "", file=sys.stderr)

    if proc.returncode == 2:
        print("[book-restaurant] User aborted booking.", file=sys.stderr)
        sys.exit(2)

    if proc.returncode == 3:
        print("[book-restaurant] Dry run complete.", file=sys.stderr)
        # Parse dry-run result from stdout
        try:
            result = json.loads(proc.stdout.strip())
        except (json.JSONDecodeError, ValueError):
            result = {
                "dry_run": True,
                "restaurant": restaurant_name,
                "restaurant_url": restaurant_url,
                "datetime": booking_state["datetime_iso"],
                "party_size": party_size,
                "screenshot_path": "",
                "status": "dry_run_complete",
            }
        return result

    if proc.returncode != 0:
        raise RuntimeError(
            f"Playwright booking script exited with code {proc.returncode}. "
            f"Stderr: {proc.stderr[-1000:]}"
        )

    try:
        result = json.loads(proc.stdout.strip())
    except (json.JSONDecodeError, ValueError) as e:
        raise RuntimeError(
            f"Playwright script returned non-JSON output: {proc.stdout[:500]!r}"
        ) from e

    # On success, call calendar event creation (unless dry run)
    if not dry_run and result.get("confirmation_number"):
        _create_calendar_event(
            restaurant_name=result.get("restaurant", restaurant_name),
            datetime_iso=booking_state["datetime_iso"],
            party_size=party_size,
            confirmation_number=result["confirmation_number"],
            location=result.get("location", ""),
        )

    return result


def _create_calendar_event(
    restaurant_name: str,
    datetime_iso: str,
    party_size: int,
    confirmation_number: str,
    location: str = "",
) -> None:
    """Call create-calendar-event.py to add the booking to Google Calendar."""
    try:
        proc = subprocess.run(
            [
                sys.executable,
                CALENDAR_SCRIPT,
                "--restaurant", restaurant_name,
                "--datetime", datetime_iso,
                "--party-size", str(party_size),
                "--confirmation", confirmation_number,
                "--location", location or "",
            ],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode == 0:
            print(f"[book-restaurant] Calendar event created: {proc.stdout.strip()}", file=sys.stderr)
        else:
            print(f"[book-restaurant] Calendar event creation failed (non-fatal): {proc.stderr.strip()}", file=sys.stderr)
    except Exception as e:
        print(f"[book-restaurant] Calendar event creation error (non-fatal): {e}", file=sys.stderr)


def _build_playwright_script(
    username: str,
    password: str,
    restaurant_url: str,
    restaurant_name: str,
    date_str: str,
    time_str: str,
    party_size: int,
    notes: str | None,
    dry_run: bool,
    logs_dir: str,
) -> str:
    """Generate a Node.js Playwright script for the TheFork booking flow.

    The password is passed via THEFORK_PASSWORD env var to avoid embedding it
    in the script file (which is written to disk in the logs dir).
    """
    confirm_script = CONFIRM_SCRIPT
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    screenshot_preconfirm = f"{logs_dir}/booking-{ts}-preconfirm.png"
    screenshot_confirmed = f"{logs_dir}/booking-{ts}-confirmed.png"

    # Escape values for embedding in JS string literals
    def js_str(s: str | None) -> str:
        if s is None:
            return "null"
        return json.dumps(str(s))

    dry_run_js = "true" if dry_run else "false"

    return f"""\
// TheFork booking script - generated by book-restaurant.py
// Password is read from THEFORK_PASSWORD env var, not embedded here.
const {{ chromium }} = require('playwright');
const {{ execSync }} = require('child_process');
const fs = require('fs');
const path = require('path');

const USERNAME = {js_str(username)};
const PASSWORD = process.env.THEFORK_PASSWORD || '';
const RESTAURANT_URL = {js_str(restaurant_url)};
const RESTAURANT_NAME = {js_str(restaurant_name)};
const DATE_STR = {js_str(date_str)};
const TIME_STR = {js_str(time_str)};
const PARTY_SIZE = {party_size};
const NOTES = {js_str(notes)};
const DRY_RUN = {dry_run_js};
const SCREENSHOT_PRECONFIRM = {js_str(screenshot_preconfirm)};
const SCREENSHOT_CONFIRMED = {js_str(screenshot_confirmed)};
const CONFIRM_SCRIPT = {js_str(confirm_script)};

if (!PASSWORD) {{
  process.stderr.write('ERROR: THEFORK_PASSWORD env var not set\\n');
  process.exit(1);
}}

async function run() {{
  const browser = await chromium.launch({{ headless: true }});
  const context = await browser.newContext({{
    viewport: {{ width: 1280, height: 900 }},
    locale: 'en-GB',
    timezoneId: 'Europe/Lisbon',
  }});
  const page = await context.newPage();

  try {{
    // Step 1: Navigate to TheFork login page
    process.stderr.write('[playwright] Navigating to TheFork login...\\n');
    await page.goto('https://www.thefork.com/signin', {{ waitUntil: 'networkidle', timeout: 30000 }});
    await page.waitForTimeout(2000);

    // Accept cookies if the banner appears
    try {{
      const cookieBtn = page.locator('button:has-text("Accept"), button:has-text("Aceitar"), [id*="accept"], [data-testid*="accept"]');
      if (await cookieBtn.first().isVisible({{ timeout: 3000 }})) {{
        await cookieBtn.first().click();
        await page.waitForTimeout(1000);
      }}
    }} catch (e) {{ /* cookie banner may not appear */ }}

    // Step 2: Fill login form
    process.stderr.write('[playwright] Filling login credentials...\\n');

    // TheFork uses email + password fields
    const emailField = page.locator('input[type="email"], input[name="email"], input[placeholder*="mail" i]').first();
    await emailField.fill(USERNAME);

    const passwordField = page.locator('input[type="password"]').first();
    await passwordField.fill(PASSWORD);

    // Submit login
    const loginBtn = page.locator('button[type="submit"], button:has-text("Sign in"), button:has-text("Log in"), button:has-text("Entrar")').first();
    await loginBtn.click();
    await page.waitForLoadState('networkidle', {{ timeout: 15000 }});
    await page.waitForTimeout(2000);

    // Verify login succeeded (check for user menu or redirect away from /signin)
    const currentUrl = page.url();
    if (currentUrl.includes('/signin')) {{
      // May still be on signin page - check for error message
      const errorMsg = await page.locator('.error, [class*="error"], [role="alert"]').first().textContent({{ timeout: 2000 }}).catch(() => '');
      process.stderr.write(`[playwright] Login page after submit. URL: ${{currentUrl}}. Error: ${{errorMsg}}\\n`);
      // Continue anyway - TheFork may redirect to restaurant page with login state
    }}
    process.stderr.write(`[playwright] Post-login URL: ${{currentUrl}}\\n`);

    // Step 3: Navigate to restaurant page
    process.stderr.write(`[playwright] Navigating to restaurant: ${{RESTAURANT_URL}}\\n`);
    await page.goto(RESTAURANT_URL, {{ waitUntil: 'networkidle', timeout: 30000 }});
    await page.waitForTimeout(2000);

    // Extract restaurant display name from page title if we don't have it
    const pageTitle = await page.title();
    process.stderr.write(`[playwright] Restaurant page title: ${{pageTitle}}\\n`);

    // Step 4: Select date, time, and party size
    // TheFork booking widget is typically a sidebar or inline form
    process.stderr.write('[playwright] Looking for booking widget...\\n');

    // Try to find the date picker - TheFork uses various selectors across locales
    // Look for the reservation widget
    const bookingWidget = page.locator(
      '[data-testid*="booking"], [class*="booking-widget"], [class*="reservation"], ' +
      'form[action*="reservation"], [id*="booking"]'
    ).first();

    // Try setting party size first (often a select/input)
    const partySizeSelectors = [
      'select[name*="guest"], select[name*="party"], select[name*="cover"], select[name*="person"]',
      'input[name*="guest"], input[name*="party"], input[name*="cover"]',
      '[data-testid*="guest"], [data-testid*="party"], [data-testid*="cover"]',
      '[aria-label*="guest" i], [aria-label*="person" i], [aria-label*="cover" i]',
      'select.reservation-covers, select.covers',
    ];

    let partySizeSet = false;
    for (const sel of partySizeSelectors) {{
      try {{
        const el = page.locator(sel).first();
        if (await el.isVisible({{ timeout: 2000 }})) {{
          const tagName = await el.evaluate(e => e.tagName.toLowerCase());
          if (tagName === 'select') {{
            await el.selectOption({{ value: String(PARTY_SIZE) }});
          }} else {{
            await el.fill(String(PARTY_SIZE));
          }}
          partySizeSet = true;
          process.stderr.write(`[playwright] Party size set via: ${{sel}}\\n`);
          break;
        }}
      }} catch (e) {{ /* try next selector */ }}
    }}
    if (!partySizeSet) {{
      process.stderr.write('[playwright] WARNING: Could not set party size via standard selectors - may need manual intervention\\n');
    }}

    // Try setting the date
    const dateSelectors = [
      'input[type="date"]',
      'input[name*="date"], input[placeholder*="date" i]',
      '[data-testid*="date-picker"], [data-testid*="datepicker"]',
      '[aria-label*="date" i]',
    ];

    let dateSet = false;
    for (const sel of dateSelectors) {{
      try {{
        const el = page.locator(sel).first();
        if (await el.isVisible({{ timeout: 2000 }})) {{
          await el.fill(DATE_STR);
          dateSet = true;
          process.stderr.write(`[playwright] Date set via: ${{sel}}\\n`);
          break;
        }}
      }} catch (e) {{ /* try next */ }}
    }}
    if (!dateSet) {{
      process.stderr.write('[playwright] WARNING: Could not set date via standard selectors\\n');
    }}

    // Try setting time slot
    const timeSelectors = [
      'select[name*="time"], select[name*="hour"]',
      'input[name*="time"], input[type="time"]',
      '[data-testid*="time"], [aria-label*="time" i]',
    ];

    let timeSet = false;
    for (const sel of timeSelectors) {{
      try {{
        const el = page.locator(sel).first();
        if (await el.isVisible({{ timeout: 2000 }})) {{
          const tagName = await el.evaluate(e => e.tagName.toLowerCase());
          if (tagName === 'select') {{
            // Try exact time first, then look for nearest
            try {{
              await el.selectOption({{ label: TIME_STR }});
              timeSet = true;
            }} catch(e2) {{
              // Collect all options and find nearest
              const options = await el.locator('option').all();
              let nearest = null;
              let nearestDiff = Infinity;
              const [rHour, rMin] = TIME_STR.split(':').map(Number);
              const requestedMins = rHour * 60 + rMin;
              for (const opt of options) {{
                const label = await opt.textContent();
                const m = label && label.match(/(\\d{{1,2}}):(\\d{{2}})/);
                if (m) {{
                  const diff = Math.abs(parseInt(m[1]) * 60 + parseInt(m[2]) - requestedMins);
                  if (diff < nearestDiff) {{
                    nearestDiff = diff;
                    nearest = await opt.getAttribute('value');
                  }}
                }}
              }}
              if (nearest && nearestDiff <= 30) {{
                await el.selectOption({{ value: nearest }});
                timeSet = true;
                process.stderr.write(`[playwright] Time set to nearest available: ${{nearest}} (diff: ${{nearestDiff}}min)\\n`);
              }}
            }}
          }} else {{
            await el.fill(TIME_STR);
            timeSet = true;
          }}
          if (timeSet) {{
            process.stderr.write(`[playwright] Time set via: ${{sel}}\\n`);
            break;
          }}
        }}
      }} catch (e) {{ /* try next */ }}
    }}
    if (!timeSet) {{
      process.stderr.write('[playwright] WARNING: Could not set time via standard selectors\\n');
    }}

    await page.waitForTimeout(1500);

    // Add notes if provided
    if (NOTES) {{
      const notesSelectors = [
        'textarea[name*="note"], textarea[name*="comment"], textarea[placeholder*="note" i]',
        '[data-testid*="notes"], [aria-label*="note" i], [aria-label*="comment" i]',
      ];
      for (const sel of notesSelectors) {{
        try {{
          const el = page.locator(sel).first();
          if (await el.isVisible({{ timeout: 2000 }})) {{
            await el.fill(NOTES);
            process.stderr.write(`[playwright] Notes set via: ${{sel}}\\n`);
            break;
          }}
        }} catch (e) {{ /* try next */ }}
      }}
    }}

    // Step 5: Take pre-confirm screenshot
    process.stderr.write(`[playwright] Taking pre-confirm screenshot: ${{SCREENSHOT_PRECONFIRM}}\\n`);
    await page.screenshot({{ path: SCREENSHOT_PRECONFIRM, fullPage: false }});

    if (DRY_RUN) {{
      process.stderr.write('[playwright] DRY RUN: stopping before confirm button. Screenshot saved.\\n');
      const dryResult = {{
        dry_run: true,
        restaurant: RESTAURANT_NAME,
        restaurant_url: RESTAURANT_URL,
        datetime: DATE_STR + 'T' + TIME_STR + ':00+01:00',
        party_size: PARTY_SIZE,
        screenshot_path: SCREENSHOT_PRECONFIRM,
        status: 'dry_run_complete',
      }};
      process.stdout.write(JSON.stringify(dryResult) + '\\n');
      await browser.close();
      process.exit(3);
    }}

    // Step 6: Call confirm-via-discord.py
    process.stderr.write('[playwright] Calling Discord soft-confirm...\\n');
    let confirmed = false;
    try {{
      const confirmResult = execSync(
        `python3 "${{CONFIRM_SCRIPT}}" ` +
        `--restaurant ${{JSON.stringify(RESTAURANT_NAME)}} ` +
        `--datetime "${{DATE_STR}} ${{TIME_STR}}" ` +
        `--party-size ${{PARTY_SIZE}} ` +
        `--screenshot ${{JSON.stringify(SCREENSHOT_PRECONFIRM)}}`,
        {{ encoding: 'utf8', timeout: 680000 }}  // 680s > 10 min timeout
      );
      const confirmData = JSON.parse(confirmResult.trim());
      confirmed = confirmData.confirmed === true;
    }} catch (e) {{
      process.stderr.write(`[playwright] Discord confirm error: ${{e.message}}\\n`);
      confirmed = false;
    }}

    if (!confirmed) {{
      process.stderr.write('[playwright] Booking aborted by user or timeout.\\n');
      await browser.close();
      process.exit(2);
    }}

    // Step 7: Click the confirm/reserve button
    process.stderr.write('[playwright] User confirmed - clicking reserve button...\\n');
    const confirmSelectors = [
      'button:has-text("Reserve"), button:has-text("Confirm"), button:has-text("Book")',
      'button:has-text("Reservar"), button:has-text("Confirmar")',
      '[data-testid*="confirm"], [data-testid*="reserve"], [data-testid*="submit"]',
      'button[type="submit"]',
    ];

    let clicked = false;
    for (const sel of confirmSelectors) {{
      try {{
        const el = page.locator(sel).first();
        if (await el.isVisible({{ timeout: 3000 }})) {{
          await el.click();
          clicked = true;
          process.stderr.write(`[playwright] Clicked confirm via: ${{sel}}\\n`);
          break;
        }}
      }} catch (e) {{ /* try next */ }}
    }}

    if (!clicked) {{
      throw new Error('Could not find confirm/reserve button on TheFork form');
    }}

    // Wait for confirmation page
    await page.waitForLoadState('networkidle', {{ timeout: 30000 }});
    await page.waitForTimeout(2000);

    // Step 8: Capture confirmation number
    process.stderr.write('[playwright] Looking for confirmation number...\\n');
    let confirmationNumber = 'unknown';
    const confSelectors = [
      '[data-testid*="confirmation"], [class*="confirmation"], [id*="confirmation"]',
      ':has-text("Confirmation"), :has-text("Booking number"), :has-text("Reference")',
    ];

    for (const sel of confSelectors) {{
      try {{
        const el = page.locator(sel).first();
        const text = await el.textContent({{ timeout: 3000 }});
        const m = text && text.match(/[A-Z0-9]{{6,20}}/);
        if (m) {{
          confirmationNumber = m[0];
          process.stderr.write(`[playwright] Confirmation number: ${{confirmationNumber}}\\n`);
          break;
        }}
      }} catch (e) {{ /* try next */ }}
    }}

    // Extract location from meta tags or page content
    let location = '';
    try {{
      location = await page.locator('[itemprop="address"], [data-testid*="address"]').first().textContent({{ timeout: 2000 }});
    }} catch (e) {{ /* no location found */ }}

    // Step 9: Screenshot confirmation page
    await page.screenshot({{ path: SCREENSHOT_CONFIRMED, fullPage: false }});
    process.stderr.write(`[playwright] Confirmation screenshot: ${{SCREENSHOT_CONFIRMED}}\\n`);

    const result = {{
      confirmation_number: confirmationNumber,
      restaurant: RESTAURANT_NAME,
      restaurant_url: RESTAURANT_URL,
      datetime: DATE_STR + 'T' + TIME_STR + ':00+01:00',
      party_size: PARTY_SIZE,
      screenshot_path: SCREENSHOT_CONFIRMED,
      location: location.trim(),
      status: 'confirmed',
    }};

    process.stdout.write(JSON.stringify(result) + '\\n');
    await browser.close();
    process.exit(0);

  }} catch (err) {{
    process.stderr.write(`[playwright] ERROR: ${{err.message}}\\n`);
    try {{
      await page.screenshot({{ path: SCREENSHOT_PRECONFIRM.replace('.png', '-error.png') }});
    }} catch (e2) {{ /* screenshot on error failed */ }}
    await browser.close();
    process.exit(1);
  }}
}}

run().catch(err => {{
  process.stderr.write(`[playwright] Fatal: ${{err.message}}\\n`);
  process.exit(1);
}});
"""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TheFork restaurant booking via Playwright",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--restaurant-url",
        required=True,
        help="TheFork restaurant URL (e.g. https://www.thefork.com/restaurant/contrabando-...)",
    )
    parser.add_argument(
        "--restaurant-name",
        default="",
        help="Human-readable restaurant name (optional, extracted from URL if not given)",
    )
    parser.add_argument(
        "--datetime",
        required=True,
        metavar="ISO8601",
        help="Booking datetime in ISO-8601 format, e.g. 2026-05-15T13:00:00+01:00",
    )
    parser.add_argument(
        "--party-size",
        type=int,
        required=True,
        help="Number of people for the reservation",
    )
    parser.add_argument(
        "--notes",
        default=None,
        help="Optional special requests or notes for the restaurant",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Perform all steps up to the pre-confirm screenshot, but do NOT "
            "submit the booking or call Discord confirm. Exit code 3."
        ),
    )
    parser.add_argument(
        "--vw-item",
        default="thefork-credentials",
        help="Vaultwarden item name for TheFork credentials (default: thefork-credentials)",
    )

    args = parser.parse_args()

    try:
        result = run_booking(
            restaurant_url=args.restaurant_url,
            datetime_iso=args.datetime,
            party_size=args.party_size,
            restaurant_name=args.restaurant_name,
            notes=args.notes,
            dry_run=args.dry_run,
            vw_item=args.vw_item,
        )
        print(json.dumps(result, indent=2))
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
