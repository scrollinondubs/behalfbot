#!/usr/bin/env python3
"""create-calendar-event.py - Create a Google Calendar event for a confirmed restaurant booking.

Uses the Google Calendar REST API via an OAuth2 access token. The token
is managed via the Google OAuth2 flow - first run will need authorization.
Token is cached in ~/.config/<assistant>/google-calendar-token.json.

Usage:
    python3 create-calendar-event.py \
        --restaurant "Contrabando Saldanha" \
        --datetime "2026-05-15T13:00:00+01:00" \
        --party-size 4 \
        --confirmation "ABC123" \
        [--location "Av. Duque de Avila, Lisbon"] \
        [--calendar-id "primary"]

Output (stdout):
    JSON: {"event_id": "...", "html_link": "...", "title": "..."}

Exit codes:
    0 - event created
    1 - error

Setup notes:
- Requires GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in $CHASSIS_HOME/.env
  (or in Vaultwarden as 'google-calendar-oauth')
- On first run, a browser URL will be printed for OAuth authorization
- Subsequent runs reuse the cached token (auto-refreshes via refresh_token)
- If Google Calendar OAuth is not yet configured, this script exits gracefully
  with a message directing Sean to create the event manually
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent.parent
SCRIPTS = REPO / "scripts"

TOKEN_CACHE = Path.home() / ".config" / "<v1-reference-install>" / "google-calendar-token.json"
GCAL_API_BASE = "https://www.googleapis.com/calendar/v3"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPE = "https://www.googleapis.com/auth/calendar.events"


def _load_env() -> dict[str, str]:
    try:
        sys.path.insert(0, str(SCRIPTS))
        from _loadenv import load_env as _unified  # type: ignore
        return dict(_unified())
    except ImportError:
        pass
    env: dict[str, str] = {}
    env_file = REPO / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _get_oauth_creds() -> tuple[str, str]:
    """Get Google OAuth client credentials from env/VW."""
    env = _load_env()
    client_id = env.get("GOOGLE_CLIENT_ID", "") or os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = env.get("GOOGLE_CLIENT_SECRET", "") or os.environ.get("GOOGLE_CLIENT_SECRET", "")
    return client_id, client_secret


def _load_token_cache() -> dict | None:
    """Load cached token from disk."""
    if TOKEN_CACHE.exists():
        try:
            return json.loads(TOKEN_CACHE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _save_token_cache(token_data: dict) -> None:
    """Save token to disk cache."""
    TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_CACHE.write_text(json.dumps(token_data, indent=2))


def _refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict:
    """Exchange refresh_token for a new access_token."""
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }).encode("utf-8")
    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Token refresh failed: {e.read().decode()}") from e


def _get_access_token() -> str:
    """Get a valid access token, refreshing if needed."""
    client_id, client_secret = _get_oauth_creds()

    if not client_id or not client_secret:
        raise RuntimeError(
            "Google OAuth credentials not configured. "
            "Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to $CHASSIS_HOME/.env "
            "or set up a Vaultwarden item 'google-calendar-oauth'. "
            "See plugins/restaurant-booking/README.md for setup instructions."
        )

    cached = _load_token_cache()

    if cached:
        # Check if access token is still valid (with 5 min buffer)
        expires_at = cached.get("expires_at", 0)
        if time.time() < expires_at - 300:
            return cached["access_token"]

        # Refresh the token
        if cached.get("refresh_token"):
            try:
                new_tokens = _refresh_access_token(
                    client_id, client_secret, cached["refresh_token"]
                )
                if "access_token" in new_tokens:
                    new_tokens["refresh_token"] = new_tokens.get("refresh_token", cached["refresh_token"])
                    new_tokens["expires_at"] = time.time() + new_tokens.get("expires_in", 3600)
                    _save_token_cache(new_tokens)
                    return new_tokens["access_token"]
            except RuntimeError as e:
                print(f"[create-calendar-event] Token refresh failed: {e}", file=sys.stderr)

    # No valid token - need to do the OAuth flow
    print(
        "[create-calendar-event] No valid Google Calendar token found.\n"
        "To authorize, run the one-time OAuth setup:\n\n"
        f"  python3 {__file__} --setup\n\n"
        "This will open a browser for you to authorize Google Calendar access.",
        file=sys.stderr,
    )
    raise RuntimeError(
        "Google Calendar not authorized. Run: python3 create-calendar-event.py --setup"
    )


def _do_oauth_setup(client_id: str, client_secret: str) -> None:
    """Interactive one-time OAuth setup to get initial tokens."""
    import secrets

    state = secrets.token_urlsafe(16)
    # Use OOB flow (copy-paste the code) since we may not have a local server
    redirect_uri = "urn:ietf:wg:oauth:2.0:oob"

    auth_url = (
        f"{OAUTH_AUTH_URL}?"
        + urllib.parse.urlencode({
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "state": state,
        })
    )

    print("\nTo authorize Google Calendar access, open this URL in your browser:\n")
    print(auth_url)
    print("\nAfter authorizing, paste the code shown here:")

    code = input("Authorization code: ").strip()
    if not code:
        raise RuntimeError("No authorization code provided")

    # Exchange code for tokens
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "code": code,
    }).encode("utf-8")
    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            tokens = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Token exchange failed: {e.read().decode()}") from e

    if "access_token" not in tokens:
        raise RuntimeError(f"No access_token in response: {tokens}")

    tokens["expires_at"] = time.time() + tokens.get("expires_in", 3600)
    _save_token_cache(tokens)
    print(f"\nAuthorization successful. Token cached at: {TOKEN_CACHE}")


def _create_event(
    access_token: str,
    calendar_id: str,
    title: str,
    start_datetime: str,
    end_datetime: str,
    location: str,
    description: str,
) -> dict:
    """Call the Google Calendar API to create an event."""
    event = {
        "summary": title,
        "location": location,
        "description": description,
        "start": {
            "dateTime": start_datetime,
            "timeZone": "Europe/Lisbon",
        },
        "end": {
            "dateTime": end_datetime,
            "timeZone": "Europe/Lisbon",
        },
    }

    body = json.dumps(event).encode("utf-8")
    url = f"{GCAL_API_BASE}/calendars/{urllib.parse.quote(calendar_id)}/events"
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Calendar API error: {e.read().decode()}") from e


def create_booking_event(
    restaurant_name: str,
    datetime_iso: str,
    party_size: int,
    confirmation_number: str,
    location: str = "",
    calendar_id: str = "primary",
) -> dict:
    """Create a Google Calendar event for a confirmed booking."""
    dt = datetime.fromisoformat(datetime_iso)
    hour = dt.hour

    # Dinner if 17:00 or later, lunch otherwise
    meal_type = "Dinner" if hour >= 17 else "Lunch"
    title = f"{meal_type} at {restaurant_name} (party of {party_size})"

    # 90-minute default duration
    end_dt = dt + timedelta(minutes=90)
    end_iso = end_dt.isoformat()

    description = (
        f"Party of {party_size}.\n"
        f"Booked via Behalf.bot restaurant-booking plugin.\n"
        f"Confirmation: {confirmation_number}."
    )

    access_token = _get_access_token()

    result = _create_event(
        access_token=access_token,
        calendar_id=calendar_id,
        title=title,
        start_datetime=datetime_iso,
        end_datetime=end_iso,
        location=location,
        description=description,
    )

    return {
        "event_id": result.get("id", ""),
        "html_link": result.get("htmlLink", ""),
        "title": result.get("summary", title),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create Google Calendar event for a confirmed restaurant booking",
    )
    parser.add_argument("--restaurant", required=False, help="Restaurant name")
    parser.add_argument("--datetime", required=False, metavar="ISO8601", help="Booking datetime")
    parser.add_argument("--party-size", type=int, required=False, help="Number of guests")
    parser.add_argument("--confirmation", required=False, default="", help="TheFork confirmation number")
    parser.add_argument("--location", default="", help="Restaurant address/location")
    parser.add_argument("--calendar-id", default="primary", help="Google Calendar ID (default: primary)")
    parser.add_argument("--setup", action="store_true", help="Run one-time OAuth authorization setup")

    args = parser.parse_args()

    if args.setup:
        client_id, client_secret = _get_oauth_creds()
        if not client_id or not client_secret:
            print(
                "Error: GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set in $CHASSIS_HOME/.env\n"
                "Create a Google Cloud project, enable Calendar API, and create OAuth2 credentials.\n"
                "See plugins/restaurant-booking/README.md for full setup instructions.",
                file=sys.stderr,
            )
            sys.exit(1)
        _do_oauth_setup(client_id, client_secret)
        return

    if not args.restaurant or not args.datetime or not args.party_size:
        parser.print_help()
        sys.exit(1)

    try:
        result = create_booking_event(
            restaurant_name=args.restaurant,
            datetime_iso=args.datetime,
            party_size=args.party_size,
            confirmation_number=args.confirmation,
            location=args.location,
            calendar_id=args.calendar_id,
        )
        print(json.dumps(result, indent=2))
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        print(
            "\nNOTE: Calendar event creation failed. Please create the event manually:\n"
            f"  Title: Lunch/Dinner at {args.restaurant} (party of {args.party_size})\n"
            f"  Time: {args.datetime}\n"
            f"  Confirmation: {args.confirmation}",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
