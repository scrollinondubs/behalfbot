#!/usr/bin/env python3
"""matrix-control-listener.py - the remote kill switch, listening on Matrix.

Opt-in sibling of discord-control-listener.py for installs that moved the
operator's chat to Matrix. Same lexicon, same flag files, same acks: the
command parser, `set_flag()` and the ack text are imported from the Discord
listener rather than copied, so the two cannot drift. Started by the container
entrypoint only when CHASSIS_MATRIX_CONTROL=1. See docs/remote-kill-switch.md.

EVENT RULES
-----------
    - m.room.message with msgtype m.text only.
    - Sender must equal CHASSIS_MATRIX_PRINCIPAL exactly (a full user id such
      as @principal:example.org). Display names are never compared: anyone can
      set one.
    - Events older than 10 minutes are dropped. After an outage or a restart
      the listener must not replay a `halt` or `resume` the operator has since
      countermanded.
    - Edits (m.replace) are ignored. Editing "status" into "halt" must not
      halt, and editing an old message must not re-run it.
    - Encrypted events are never acted on. The listener holds no keys, so an
      encrypted control room makes it deaf. That is logged loudly, and an
      encrypted room found at start refuses to arm (exit 78), because a kill
      switch that silently never fires is the failure this exists to end.
    - A reply's quoted fallback is stripped before matching, and a leading
      full user-id mention (`@bot:server: halt`) is allowed.

POLLING
-------
GET /rooms/{room}/messages with a per-room token, not /sync. /sync on the
bot's access token would consume to-device messages meant for whatever else
uses that token. The first poll of a room only takes the current position and
acts on nothing.

ENVIRONMENT
-----------
    CHASSIS_MATRIX_TOKEN_FILE         required - JSON with access_token
    CHASSIS_MATRIX_PRINCIPAL          required - the operator's Matrix user id
    CHASSIS_MATRIX_CONTROL_ROOMS      optional - comma/space separated room
                                      ids. Defaults to CHASSIS_MATRIX_DM_ROOM.
    CHASSIS_MATRIX_HOMESERVER         default http://127.0.0.1:8008 (rewritten
                                      to host.docker.internal in the container)
    CUSTOMER_HOME / CHASSIS_HOME      required - where the state files live
    CHASSIS_MATRIX_CONTROL_POLL_SECONDS  optional, default 15

EXIT CODES
----------
    0   never on the happy path
    78  configuration error (EX_CONFIG); the supervisor does not restart it
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
EX_CONFIG = 78
MAX_EVENT_AGE_S = 600
USER_AGENT = "behalfbot (matrix-control-listener)"
MXID_RE = re.compile(r"^@[^:\s]+:\S+$")
LEADING_MENTION_RE = re.compile(r"^@[^:\s]+:[A-Za-z0-9.\-\[\]]+(?::\d+)?:?\s*")


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


control = _load("discord_control_listener", "discord-control-listener.py")
matrix_post = _load("matrix_post", "matrix-post.py")

log = control.log
parse_command = control.parse_command
set_flag = control.set_flag


# --------------------------------------------------------------------------
# Event rules (adapted from new-jaxity scripts/_angel_matrix.py parse_event)
# --------------------------------------------------------------------------


def strip_reply_fallback(body):
    lines = body.split("\n")
    if not lines or not lines[0].startswith(">"):
        return body
    i = 0
    while i < len(lines) and lines[i].startswith(">"):
        i += 1
    if i < len(lines) and lines[i].strip() == "":
        i += 1
    return "\n".join(lines[i:])


def parse_event(ev, principal, now_ms=None, max_age_s=MAX_EVENT_AGE_S):
    """(body, "") when the event should be matched against the lexicon, else
    (None, reason). Reasons go to the log and never carry a body."""
    etype = ev.get("type")
    sender = str(ev.get("sender") or "")
    if etype == "m.room.encrypted":
        return None, "encrypted"
    if etype != "m.room.message":
        return None, "type"
    if sender != principal:
        return None, "sender"
    content = ev.get("content")
    if not isinstance(content, dict) or not content:
        return None, "redacted"
    rel = content.get("m.relates_to") if isinstance(content.get("m.relates_to"), dict) else {}
    if rel.get("rel_type") == "m.replace" or "m.new_content" in content:
        return None, "edit"
    ts = ev.get("origin_server_ts")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return None, "no-timestamp"
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if (now_ms - ts) / 1000 > max_age_s:
        return None, "stale"
    if content.get("msgtype") != "m.text":
        return None, "msgtype"
    body = content.get("body") if isinstance(content.get("body"), str) else ""
    if isinstance(rel.get("m.in_reply_to"), dict):
        body = strip_reply_fallback(body)
    body = LEADING_MENTION_RE.sub("", body.strip())
    return body, ""


# --------------------------------------------------------------------------
# Matrix REST
# --------------------------------------------------------------------------


class MatrixError(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


class Matrix:
    def __init__(self, base, token):
        self.base = base
        self._token = token

    def _request(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", "Bearer %s" % self._token)
        req.add_header("User-Agent", USER_AGENT)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = {}
            try:
                detail = json.loads(exc.read().decode("utf-8", errors="replace"))
            except Exception:
                pass
            err = MatrixError("HTTP %d %s" % (exc.code, detail.get("errcode", "")), exc.code)
            err.retry_after_ms = detail.get("retry_after_ms")
            raise err
        except (urllib.error.URLError, OSError) as exc:
            raise MatrixError("unreachable: %s" % getattr(exc, "reason", exc))
        return json.loads(raw) if raw else {}

    @staticmethod
    def q(value):
        return urllib.parse.quote(value, safe="")

    def messages(self, room_id, from_token=None, direction="f", limit=50):
        params = {"dir": direction, "limit": limit}
        if from_token:
            params["from"] = from_token
        path = "/_matrix/client/v3/rooms/%s/messages?%s" % (self.q(room_id), urllib.parse.urlencode(params))
        return self._request("GET", path)

    def room_encrypted(self, room_id):
        try:
            self._request("GET", "/_matrix/client/v3/rooms/%s/state/m.room.encryption" % self.q(room_id))
        except MatrixError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    def send(self, room_id, content):
        txn = "control-%d-%d" % (int(time.time() * 1000), os.getpid())
        path = "/_matrix/client/v3/rooms/%s/send/m.room.message/%s" % (self.q(room_id), txn)
        return self._request("PUT", path, content)


# --------------------------------------------------------------------------
# Listener
# --------------------------------------------------------------------------


class MatrixListener(control.Listener):
    """Reuses the Discord listener's apply() and describe_state(), so the
    flag writes and every ack are byte-identical across transports."""

    SOURCE = "matrix-control-listener"

    def __init__(self, customer_home, principal_id, rooms, matrix):
        super().__init__(customer_home, principal_id, [], None)
        self.rooms = rooms
        self.matrix = matrix
        self.cursor_file = os.path.join(customer_home, "state", "matrix-control-listener-cursor.json")
        stored = control.read_flag(self.cursor_file)
        self.cursors = {r: stored[r] for r in rooms if isinstance(stored.get(r), str) and stored[r]}

    def _save_cursors(self):
        try:
            control.write_json_atomic(self.cursor_file, self.cursors)
        except OSError as exc:
            log("WARN: could not persist cursor: %s" % exc)

    def refuse_encrypted_rooms(self):
        """True when every room is readable. An encrypted control room cannot
        be read without keys, so arming on one would be a switch that never
        fires."""
        ok = True
        for room in self.rooms:
            try:
                if self.matrix.room_encrypted(room):
                    log("ERROR: control room %s is end-to-end encrypted. This listener holds no keys and "
                        "cannot read it, so `halt` sent there would do NOTHING. Use an unencrypted room "
                        "for CHASSIS_MATRIX_CONTROL_ROOMS." % room)
                    ok = False
            except MatrixError as exc:
                log("WARN: could not check encryption on %s (%s); continuing" % (room, exc))
        return ok

    def ack(self, room, text):
        try:
            self.matrix.send(room, matrix_post.text_content(text))
        except MatrixError as exc:
            log("WARN: ack to %s failed: %s" % (room, exc))

    def poll_room(self, room):
        token = self.cursors.get(room)
        if not token:
            # First sight of this room: take the position, act on nothing.
            data = self.matrix.messages(room, direction="b", limit=1)
            start = data.get("start")
            if start:
                self.cursors[room] = start
                self._save_cursors()
            return
        data = self.matrix.messages(room, from_token=token)
        for ev in data.get("chunk") or []:
            body, reason = parse_event(ev, self.principal_id)
            if body is None:
                if reason == "encrypted":
                    log("WARN: encrypted event from %s in %s ignored - the kill switch cannot read "
                        "encrypted rooms" % (ev.get("sender", "?"), room))
                continue
            parsed = parse_command(body)
            if parsed is None:
                continue
            command, duration, reason_text = parsed
            log("COMMAND %s from %s in %s (event %s)" % (command, self.principal_id, room, ev.get("event_id")))
            try:
                text = self.apply(command, duration, reason_text, self.principal_id)
            except OSError as exc:
                log("ERROR: could not write state for %s: %s" % (command, exc))
                text = "Could not apply `%s` - writing the state file failed: %s" % (command, exc)
            self.ack(room, text)
        end = data.get("end")
        if end and end != token:
            self.cursors[room] = end
            self._save_cursors()

    def poll_once(self):
        for room in self.rooms:
            try:
                self.poll_room(room)
            except MatrixError as exc:
                wait = getattr(exc, "retry_after_ms", None)
                if exc.status == 429 and isinstance(wait, (int, float)):
                    time.sleep(min(wait / 1000, 60))
                if exc.status in (401, 403):
                    log("ERROR: %s on %s - check the token and that the bot has joined the room" % (exc, room))
                else:
                    log("WARN: poll of %s failed: %s" % (room, exc))

    def run(self, interval):
        log("listening on Matrix rooms %s for principal %s, poll=%ss, state=%s/scheduled-tasks"
            % (",".join(self.rooms), self.principal_id, interval, self.customer_home))
        while True:
            try:
                self.poll_once()
            except Exception as exc:
                log("ERROR: poll failed, continuing: %s" % exc)
            time.sleep(interval)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def resolve_rooms():
    raw = os.environ.get("CHASSIS_MATRIX_CONTROL_ROOMS") or os.environ.get("CHASSIS_MATRIX_DM_ROOM") or ""
    return [r for r in re.split(r"[,\s]+", raw.strip()) if r]


def resolve_config(quiet):
    """(customer_home, principal, rooms, token) or None. Fails closed. Reads
    the token file but makes no network call."""
    problems = []
    customer_home = os.environ.get("CUSTOMER_HOME") or os.environ.get("CHASSIS_HOME") or ""
    if not customer_home:
        problems.append("CUSTOMER_HOME (or CHASSIS_HOME) is unset - nowhere to write the flag files")

    token = None
    try:
        token = matrix_post.load_token()
    except matrix_post.PostError as exc:
        problems.append(str(exc))

    principal = (os.environ.get("CHASSIS_MATRIX_PRINCIPAL") or "").strip()
    if not principal:
        problems.append("CHASSIS_MATRIX_PRINCIPAL is unset - without it nobody is authorised to flip the switch")
    elif not MXID_RE.match(principal):
        problems.append("CHASSIS_MATRIX_PRINCIPAL is not a full Matrix user id (@name:server)")

    rooms = resolve_rooms()
    if not rooms:
        problems.append("no control room - set CHASSIS_MATRIX_CONTROL_ROOMS or CHASSIS_MATRIX_DM_ROOM")
    elif any(not r.startswith("!") for r in rooms):
        problems.append("control rooms must be room ids (!id:server), not aliases")

    if problems:
        if not quiet:
            log("ERROR: Matrix kill switch is UNARMED. This install cannot be stopped from Matrix.")
            for problem in problems:
                log("ERROR:   %s" % problem)
            log("ERROR: see docs/remote-kill-switch.md. Exiting 78 (config) - not restarting.")
        return None
    return customer_home, principal, rooms, token


def main(argv):
    quiet = "--check" in argv
    config = resolve_config(quiet=quiet)
    if config is None:
        return EX_CONFIG
    if quiet:
        return 0
    customer_home, principal, rooms, token = config
    try:
        interval = max(5, int(os.environ.get("CHASSIS_MATRIX_CONTROL_POLL_SECONDS", "15")))
    except ValueError:
        interval = 15
    listener = MatrixListener(customer_home, principal, rooms, Matrix(matrix_post.homeserver_url(), token))
    if not listener.refuse_encrypted_rooms():
        return EX_CONFIG
    return listener.run(interval)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
