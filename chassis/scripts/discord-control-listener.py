#!/usr/bin/env python3
"""discord-control-listener.py - out-of-band remote kill switch for an install.

WHY THIS EXISTS
---------------
Every existing way to stop a runaway install goes through something that can
itself be down. `scripts/conservation-mode.sh` needs a shell. The Discord
plugin path needs a live `claude` process holding a valid OAuth session. The
dispatcher's own throttles need the dispatcher to be healthy.

scrollinondubs/new-jaxity#550: between 2026-09-01 and 2026-09-05 the reference
install's operator was off-grid. A heartbeat fired every 30 minutes on a
condition that could never self-clear - 514 model invocations, $45.79, 50+
near-identical Discord messages over four days. In the same window his
Tailscale node key expired (no ssh, no VNC) and his host Claude session lost
its OAuth token (no Discord replies). He could watch the spam arrive on his
phone and had no way to stop it. He drove home and pulled the power cord.

This listener is the missing path. It is a standalone process that polls the
Discord REST API with the bot token, recognises a small fixed lexicon of
control words from the install's principal, and writes the dispatcher's state
files directly. No Claude, no shell, no ssh. It keeps working when everything
that failed in #550 is still failing.

LEXICON (exact whole-message match, case-insensitive, optional leading bot
mention, optional trailing punctuation)

    halt [<duration>] [<reason>]     aliases: mute, silence
        Full stop. The dispatcher skips EVERY heartbeat at its next tick,
        `criticality: critical` included.

    resume                           aliases: unmute, unhalt
        Clears halt AND conservation. One word gets back to normal.

    conserve [<duration>] [<reason>] aliases: throttle, "conservation on"
        Conservation mode. `critical` heartbeats still run.

    unconserve                       alias: "conservation off"
        Clears conservation only.

    status                           Report both flags. Changes nothing.

`<duration>` is `<n>m` / `<n>h` / `<n>d` and becomes `auto_lift_after`.
Omitted, the flag stays set until explicitly cleared - an auto-resume while the
operator is still off-grid recreates the outage this exists to end.

WHAT HALT DOES NOT STOP
-----------------------
Named explicitly in the ack, because a kill switch that overstates its reach is
worse than none:

  - An in-flight `claude -p`. The gate is evaluated at tick start; a tick
    already running finishes (worst case ~40 minutes with retries).
  - Host-side launchd / systemd jobs outside the dispatcher: the iCloud poller,
    discord-restart, discord-watchdog, plugin-owned agents.
  - A live interactive Claude session. If one is up it can still speak.
  - This listener itself, which must keep listening to hear `resume`.

AUTHORISATION
-------------
Only the install's principal. `CHASSIS_PRINCIPAL_USER_ID` (the id the
principal-policy hook already uses), falling back to
`INSTALLER_DISCORD_USER_ID` (the id `bootstrap-discord-access.sh` already
requires). Unlike that hook, this one fails CLOSED: with no principal id
configured the listener refuses to start and says so. A kill switch that
silently never arms is the exact failure shape of #550.

ENVIRONMENT
-----------
    DISCORD_BOT_TOKEN                required
    CHASSIS_PRINCIPAL_USER_ID        required (or INSTALLER_DISCORD_USER_ID)
    CHASSIS_CONTROL_CHANNEL_IDS      optional - comma/space separated channel
                                     ids to watch. Defaults to
                                     DISCORD_PRIMARY_CHANNEL_ID.
    CUSTOMER_HOME / CHASSIS_HOME     required - where the state files live
    CHASSIS_CONTROL_POLL_SECONDS     optional, default 20
    CHASSIS_CONTROL_LOOKBACK_SECONDS optional, default 3600 - restart replay cap

EXIT CODES
----------
    0   never on the happy path (the loop does not end)
    78  configuration error (EX_CONFIG). The supervisor must NOT restart-loop
        on this - nothing about it is transient.

HTTP
----
urllib with an explicit User-Agent, matching `discord-react.py`. Python's
default urllib User-Agent gets a Cloudflare 403 in front of discord.com, which
has bitten this codebase twice. `requests` is in the image but not in the CI
test job, and this file is the one that must not fail to start.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://discord.com/api/v10"
USER_AGENT = "behalfbot (discord-control-listener)"
EX_CONFIG = 78

# Discord's epoch. Used to synthesise a "now" snowflake so a cold start reads
# forward from this moment instead of replaying four days of backlog.
DISCORD_EPOCH_MS = 1420070400000

DURATION_RE = re.compile(r"^(\d+)([mhd])$", re.IGNORECASE)
MENTION_RE = re.compile(r"^<@!?\d+>\s*")

# Verb -> canonical command. Two-word forms are normalised before lookup.
COMMANDS = {
    "halt": "halt",
    "mute": "halt",
    "silence": "halt",
    "resume": "resume",
    "unmute": "resume",
    "unhalt": "resume",
    "conserve": "conserve",
    "throttle": "conserve",
    "conservation on": "conserve",
    "unconserve": "unconserve",
    "conservation off": "unconserve",
    "status": "status",
}


def log(msg):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("[%s] %s" % (stamp, msg), flush=True)


def utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def snowflake_for(dt):
    ms = int(dt.timestamp() * 1000)
    return str(max(0, ms - DISCORD_EPOCH_MS) << 22)


# --------------------------------------------------------------------------
# Command parsing
# --------------------------------------------------------------------------


def parse_command(content):
    """Return (command, duration_iso, reason) or None when this is not a
    control message.

    Match is on the WHOLE message. A control word buried in a sentence is
    conversation, not an instruction - the operator typing "we should probably
    halt the drift alert" must not silence the install.
    """
    if not content:
        return None

    text = MENTION_RE.sub("", content.strip())
    text = text.strip().strip("!.?,;:").strip()
    if not text:
        return None

    parts = text.split()
    lowered = [p.lower() for p in parts]

    command = None
    rest = []
    if len(lowered) >= 2 and "%s %s" % (lowered[0], lowered[1]) in COMMANDS:
        command = COMMANDS["%s %s" % (lowered[0], lowered[1])]
        rest = parts[2:]
    elif lowered[0] in COMMANDS:
        command = COMMANDS[lowered[0]]
        rest = parts[1:]

    if command is None:
        return None

    # `resume` / `unconserve` / `status` take no arguments. Anything trailing
    # means this was a sentence that happened to start with the word, so treat
    # it as conversation rather than guessing.
    if command in ("resume", "unconserve", "status") and rest:
        return None

    duration = None
    if rest:
        m = DURATION_RE.match(rest[0])
        if m:
            duration = duration_to_iso(int(m.group(1)), m.group(2).lower())
            rest = rest[1:]

    reason = " ".join(rest).strip() or None
    return command, duration, reason


def duration_to_iso(amount, unit):
    delta = {
        "m": timedelta(minutes=amount),
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
    }[unit]
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Flag files
# --------------------------------------------------------------------------


def write_json_atomic(path, payload):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def read_flag(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log("WARN: unreadable flag file %s: %s" % (path, exc))
        return {}


def set_flag(path, enabled, by, auto_lift, reason):
    """Write the five-key flag schema the dispatcher already reads.

    Identical shape to `conservation-mode.json` on purpose - the dispatcher,
    `scripts/conservation-mode.sh`, `backup-to-s3.sh` and
    `gather-quota-check.sh` all speak it already. A parallel schema for the
    halt flag would be a second thing to keep in sync.
    """
    write_json_atomic(
        path,
        {
            "enabled": enabled,
            "enabled_at": utcnow_iso() if enabled else None,
            "enabled_by": by if enabled else None,
            "auto_lift_after": auto_lift if enabled else None,
            "reason": reason if enabled else None,
        },
    )


# --------------------------------------------------------------------------
# Discord REST
# --------------------------------------------------------------------------


class Discord:
    def __init__(self, token):
        self._token = token

    def _request(self, method, path, body=None):
        url = "%s%s" % (API, path)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bot %s" % self._token)
        req.add_header("User-Agent", USER_AGENT)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else None

    def call(self, method, path, body=None):
        """Single retry-aware call. Returns None on any failure - the caller
        keeps looping. This process must never die on an HTTP error; being
        alive to hear `resume` is the whole point of it."""
        for attempt in (1, 2, 3):
            try:
                return self._request(method, path, body)
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", errors="replace")[:300]
                except Exception:
                    pass
                if exc.code == 429:
                    wait = 5.0
                    try:
                        wait = float(json.loads(detail).get("retry_after", 5.0))
                    except Exception:
                        pass
                    log("WARN: rate limited on %s %s, sleeping %ss" % (method, path, wait))
                    time.sleep(min(wait, 60))
                    continue
                if exc.code in (401, 403):
                    log(
                        "ERROR: HTTP %d on %s %s - check the bot token, and that the bot has "
                        "View Channel + Read Message History + Send Messages on the control "
                        "channel. %s" % (exc.code, method, path, detail)
                    )
                    return None
                log("WARN: HTTP %d on %s %s (attempt %d): %s" % (exc.code, method, path, attempt, detail))
            except Exception as exc:
                log("WARN: %s %s failed (attempt %d): %s" % (method, path, attempt, exc))
            time.sleep(2 * attempt)
        return None

    def messages_after(self, channel_id, after):
        query = urllib.parse.urlencode({"after": after, "limit": 50})
        return self.call("GET", "/channels/%s/messages?%s" % (channel_id, query))

    def post(self, channel_id, content):
        return self.call(
            "POST",
            "/channels/%s/messages" % channel_id,
            {"content": content[:1990], "allowed_mentions": {"parse": []}},
        )


# --------------------------------------------------------------------------
# Listener
# --------------------------------------------------------------------------


class Listener:
    def __init__(self, customer_home, principal_id, channels, discord):
        self.customer_home = customer_home
        self.principal_id = principal_id
        self.channels = channels
        self.discord = discord
        self.halt_file = os.path.join(customer_home, "scheduled-tasks", "halt.json")
        self.conservation_file = os.path.join(
            customer_home, "scheduled-tasks", "conservation-mode.json"
        )
        self.cursor_file = os.path.join(customer_home, "state", "control-listener-cursor.json")
        self.cursors = self._load_cursors()

    # -- cursor ------------------------------------------------------------

    def _load_cursors(self):
        """Cold start reads FORWARD from now, never backward.

        A listener that came up and replayed the channel would re-execute a
        four-day-old `halt`, or worse a `resume` the operator sent before the
        thing they wanted stopped. A persisted cursor is honoured but capped at
        CHASSIS_CONTROL_LOOKBACK_SECONDS for the same reason.
        """
        try:
            lookback = int(os.environ.get("CHASSIS_CONTROL_LOOKBACK_SECONDS", "3600"))
        except ValueError:
            lookback = 3600
        floor = snowflake_for(datetime.now(timezone.utc) - timedelta(seconds=lookback))
        stored = read_flag(self.cursor_file)
        cursors = {}
        for channel in self.channels:
            value = str(stored.get(channel, ""))
            if value.isdigit() and int(value) > int(floor):
                cursors[channel] = value
            else:
                cursors[channel] = floor
        return cursors

    def _save_cursors(self):
        try:
            write_json_atomic(self.cursor_file, self.cursors)
        except OSError as exc:
            log("WARN: could not persist cursor: %s" % exc)

    # -- actions -----------------------------------------------------------

    def describe_state(self):
        halt = read_flag(self.halt_file)
        cons = read_flag(self.conservation_file)

        def line(label, flag):
            if not flag.get("enabled"):
                return "%s: off" % label
            bits = ["%s: ON since %s" % (label, flag.get("enabled_at") or "unknown")]
            if flag.get("reason"):
                bits.append("reason: %s" % flag["reason"])
            if flag.get("auto_lift_after"):
                bits.append("auto-lift: %s" % flag["auto_lift_after"])
            else:
                bits.append("auto-lift: none")
            return " - ".join(bits)

        return "\n".join(
            [
                line("halt", halt),
                line("conservation", cons),
                "",
                "halt | resume | conserve | unconserve | status",
            ]
        )

    def apply(self, command, duration, reason, user_id):
        by = "discord-control-listener:%s" % user_id

        if command == "status":
            return self.describe_state()

        if command == "halt":
            set_flag(self.halt_file, True, by, duration, reason)
            lift = (
                "Auto-lift %s." % duration
                if duration
                else "No auto-lift - stays halted until you say `resume`."
            )
            return (
                "HALTED. Every heartbeat is skipped from the next dispatcher tick, "
                "critical ones included.\n"
                "%s\n"
                "Still possible for a short while: a `claude -p` already running finishes "
                "(up to ~40 min with retries).\n"
                "NOT covered by halt: host-side launchd/systemd jobs, and any live "
                "interactive Claude session.\n"
                "Say `resume` to lift, `status` to check." % lift
            )

        if command == "conserve":
            set_flag(self.conservation_file, True, by, duration, reason)
            lift = (
                "Auto-lift %s." % duration
                if duration
                else "No auto-lift - stays on until you say `resume`."
            )
            return (
                "CONSERVATION ON. Heartbeats marked `normal` and `background` are skipped "
                "from the next tick; `critical` ones still run.\n"
                "%s\n"
                "If you want everything to stop, say `halt`." % lift
            )

        if command == "unconserve":
            set_flag(self.conservation_file, False, by, None, None)
            return "Conservation off. Normal and background heartbeats resume at the next tick."

        # resume clears both, so one word is enough under pressure.
        set_flag(self.halt_file, False, by, None, None)
        set_flag(self.conservation_file, False, by, None, None)
        return "RESUMED. Halt and conservation both cleared. Heartbeats resume at the next tick."

    # -- loop --------------------------------------------------------------

    def poll_once(self):
        for channel in self.channels:
            batch = self.discord.messages_after(channel, self.cursors[channel])
            if not batch:
                continue
            # Discord returns newest first. Process oldest first so a `halt`
            # then `resume` in the same batch lands in the order they were sent.
            for message in sorted(batch, key=lambda m: int(m["id"])):
                self.cursors[channel] = message["id"]
                author = (message.get("author") or {}).get("id", "")
                if author != self.principal_id:
                    continue
                parsed = parse_command(message.get("content") or "")
                if parsed is None:
                    continue
                command, duration, reason = parsed
                log("COMMAND %s from %s in %s (message %s)" % (command, author, channel, message["id"]))
                try:
                    ack = self.apply(command, duration, reason, author)
                except OSError as exc:
                    log("ERROR: could not write state for %s: %s" % (command, exc))
                    ack = "Could not apply `%s` - writing the state file failed: %s" % (command, exc)
                self.discord.post(channel, ack)
            self._save_cursors()

    def run(self, interval):
        log(
            "listening on channels %s for principal %s, poll=%ss, state=%s/scheduled-tasks"
            % (",".join(self.channels), self.principal_id, interval, self.customer_home)
        )
        while True:
            try:
                self.poll_once()
            except Exception as exc:
                log("ERROR: poll failed, continuing: %s" % exc)
            time.sleep(interval)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def resolve_channels():
    raw = (
        os.environ.get("CHASSIS_CONTROL_CHANNEL_IDS")
        or os.environ.get("DISCORD_PRIMARY_CHANNEL_ID")
        or ""
    )
    return [c for c in re.split(r"[,\s]+", raw.strip()) if c]


def resolve_config(quiet):
    """Returns (customer_home, principal_id, channels, token) or None.

    Fails CLOSED and loudly. The hook this borrows the principal id from fails
    open on purpose (a misconfigured install should not lose its prompts); a
    kill switch has the opposite requirement.
    """
    problems = []

    customer_home = os.environ.get("CUSTOMER_HOME") or os.environ.get("CHASSIS_HOME") or ""
    if not customer_home:
        problems.append("CUSTOMER_HOME (or CHASSIS_HOME) is unset - nowhere to write the flag files")

    token = os.environ.get("DISCORD_BOT_TOKEN") or ""
    if not token:
        problems.append("DISCORD_BOT_TOKEN is unset")

    principal_id = (
        os.environ.get("CHASSIS_PRINCIPAL_USER_ID")
        or os.environ.get("INSTALLER_DISCORD_USER_ID")
        or ""
    ).strip()
    if not principal_id:
        problems.append(
            "CHASSIS_PRINCIPAL_USER_ID is unset (INSTALLER_DISCORD_USER_ID is also accepted) - "
            "without a principal id nobody is authorised to flip the switch"
        )
    elif not re.match(r"^\d{17,20}$", principal_id):
        problems.append("principal user id does not look like a Discord snowflake")

    channels = resolve_channels()
    if not channels:
        problems.append(
            "no control channel - set CHASSIS_CONTROL_CHANNEL_IDS or DISCORD_PRIMARY_CHANNEL_ID"
        )

    if problems:
        if not quiet:
            log("ERROR: remote kill switch is UNARMED. This install cannot be stopped from Discord.")
            for problem in problems:
                log("ERROR:   %s" % problem)
            log("ERROR: see docs/remote-kill-switch.md. Exiting 78 (config) - not restarting.")
        return None

    return customer_home, principal_id, channels, token


def main(argv):
    quiet = "--check" in argv
    config = resolve_config(quiet=quiet)
    if config is None:
        return EX_CONFIG
    if quiet:
        # Config-only probe for bootstrap-audit and the entrypoint. No network.
        return 0

    customer_home, principal_id, channels, token = config
    try:
        interval = max(5, int(os.environ.get("CHASSIS_CONTROL_POLL_SECONDS", "20")))
    except ValueError:
        interval = 20
    listener = Listener(customer_home, principal_id, channels, Discord(token))
    return listener.run(interval)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
