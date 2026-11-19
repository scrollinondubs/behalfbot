#!/usr/bin/env python3
"""matrix-post.py - post a message, plus optional files, to a Matrix room.

The Matrix leg of the opt-in alert transport (CHASSIS_ALERT_TRANSPORT). It is
the counterpart of discord-post.sh and keeps the same calling shape, so the
callers in alert-post.sh and _alert.sh treat the two the same way.

Usage:
    matrix-post.py <room-key|!room:id> <text|@file|-> [attachment ...]

    <room-key>  alerts | ops | dm | <name>. Resolved from the environment:
                  alerts -> CHASSIS_MATRIX_ALERTS_ROOM, then _OPS_, then _DM_
                  ops    -> CHASSIS_MATRIX_OPS_ROOM, then _ALERTS_, then _DM_
                  dm     -> CHASSIS_MATRIX_DM_ROOM
                  <name> -> CHASSIS_MATRIX_<NAME>_ROOM, then _OPS_, then _ALERTS_
                A literal room id (starting with `!`) is used as is.
    <text>      The message. `@/path` reads it from a file, `-` from stdin.
                Stdin is the safe form for text you do not control, because a
                message that happens to start with `@` is otherwise a path.

Environment:
    CHASSIS_MATRIX_HOMESERVER   default http://127.0.0.1:8008. Inside the
                                container a loopback host is rewritten to
                                host.docker.internal, so the one .env shared by
                                host and container works on both sides.
    CHASSIS_MATRIX_TOKEN_FILE   JSON file holding `access_token`. A relative
                                path resolves against CUSTOMER_HOME or
                                CHASSIS_HOME. The token is never printed.

The message is sent as m.text, so a phone notifies, with a formatted_body that
renders **bold**, `code` and [text](https://link). Text goes first and files
after, so the operator is pinged even when an upload fails.

Exit codes: 0 posted, 1 bad arguments, 2 configuration missing, 3 the
homeserver refused or could not be reached.

Standard library only, on purpose: this runs on the host from launchd as well
as in the container, and an alert path must not depend on a pip install.
"""

from __future__ import annotations

import html
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "behalfbot (matrix-post)"
DEFAULT_HOMESERVER = "http://127.0.0.1:8008"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")

FALLBACKS = {
    "alerts": ("ALERTS", "OPS", "DM"),
    "ops": ("OPS", "ALERTS", "DM"),
    "dm": ("DM",),
}


class PostError(Exception):
    def __init__(self, message, code=3):
        super().__init__(message)
        self.code = code


def in_container():
    # Overridable so a test run inside the container can still reach a fake
    # homeserver on loopback.
    marker = os.environ.get("CHASSIS_MATRIX_DOCKERENV_PATH", "/.dockerenv")
    return os.path.exists(marker)


def homeserver_url():
    raw = (os.environ.get("CHASSIS_MATRIX_HOMESERVER") or DEFAULT_HOMESERVER).strip().rstrip("/")
    if "://" not in raw:
        raw = "http://" + raw
    parts = urllib.parse.urlsplit(raw)
    if in_container() and parts.hostname in LOOPBACK_HOSTS:
        netloc = "host.docker.internal" + (":%d" % parts.port if parts.port else "")
        parts = parts._replace(netloc=netloc)
    return urllib.parse.urlunsplit(parts)


def resolve_room(key):
    if key.startswith("!"):
        return key
    upper = re.sub(r"[^A-Za-z0-9]", "_", key).upper()
    chain = FALLBACKS.get(key.lower(), (upper, "OPS", "ALERTS"))
    for name in chain:
        value = (os.environ.get("CHASSIS_MATRIX_%s_ROOM" % name) or "").strip()
        if value:
            return value
    raise PostError(
        "no Matrix room for %r - set one of %s"
        % (key, ", ".join("CHASSIS_MATRIX_%s_ROOM" % n for n in chain)),
        code=2,
    )


def load_token():
    path = (os.environ.get("CHASSIS_MATRIX_TOKEN_FILE") or "").strip()
    if not path:
        raise PostError("CHASSIS_MATRIX_TOKEN_FILE is unset", code=2)
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        base = os.environ.get("CUSTOMER_HOME") or os.environ.get("CHASSIS_HOME") or os.getcwd()
        path = os.path.join(base, path)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        # Never echo the file: on a parse error some decoders quote the input.
        raise PostError("cannot read a JSON token file at CHASSIS_MATRIX_TOKEN_FILE", code=2)
    token = data.get("access_token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        raise PostError("token file has no access_token", code=2)
    return token


_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")


def text_content(text):
    """m.text with formatted_body. Escape first, then add markup, so nothing
    in the message can inject HTML. Code spans are held out so markup inside
    them stays literal. Links are http(s) only."""
    held = []

    def hold(match):
        held.append("<code>%s</code>" % match.group(1))
        return "\x00%d\x00" % (len(held) - 1)

    formatted = _CODE.sub(hold, html.escape(text))
    formatted = _LINK.sub(r'<a href="\2">\1</a>', formatted)
    formatted = _BOLD.sub(r"<strong>\1</strong>", formatted)
    formatted = re.sub("\x00(\\d+)\x00", lambda m: held[int(m.group(1))], formatted)
    formatted = formatted.replace("\n", "<br>")
    return {
        "msgtype": "m.text",
        "body": text,
        "format": "org.matrix.custom.html",
        "formatted_body": formatted,
    }


class Matrix:
    def __init__(self, base, token, timeout=30):
        self.base = base
        self._token = token
        self.timeout = timeout
        self._txn = 0

    def request(self, method, path, body=None, raw=None, content_type=None):
        data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", "Bearer %s" % self._token)
        req.add_header("User-Agent", USER_AGENT)
        if data is not None:
            req.add_header("Content-Type", content_type or "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            errcode = ""
            try:
                errcode = json.loads(exc.read().decode("utf-8", errors="replace")).get("errcode", "")
            except Exception:
                pass
            raise PostError("HTTP %d %s on %s" % (exc.code, errcode, path.split("?")[0]))
        except (urllib.error.URLError, OSError) as exc:
            raise PostError("cannot reach the homeserver: %s" % getattr(exc, "reason", exc))
        try:
            return json.loads(payload) if payload else {}
        except ValueError:
            raise PostError("homeserver returned non-JSON on %s" % path.split("?")[0])

    def send(self, room_id, content):
        # A repeated txn id is silently deduplicated by the homeserver, so a
        # second send in the same millisecond would vanish without the counter.
        self._txn += 1
        txn = "chassis-%d-%d-%d" % (int(time.time() * 1000), os.getpid(), self._txn)
        path = "/_matrix/client/v3/rooms/%s/send/m.room.message/%s" % (
            urllib.parse.quote(room_id, safe=""),
            txn,
        )
        event = self.request("PUT", path, body=content).get("event_id")
        if not event:
            raise PostError("send returned no event_id")
        return event

    def upload(self, path):
        name = os.path.basename(path)
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        with open(path, "rb") as fh:
            data = fh.read()
        query = urllib.parse.urlencode({"filename": name})
        mxc = self.request("POST", "/_matrix/media/v3/upload?%s" % query, raw=data, content_type=mime).get(
            "content_uri"
        )
        if not mxc:
            raise PostError("upload returned no content_uri")
        msgtype = {"image": "m.image", "video": "m.video", "audio": "m.audio"}.get(mime.split("/")[0], "m.file")
        return {"msgtype": msgtype, "body": name, "url": mxc, "info": {"mimetype": mime, "size": len(data)}}


def read_body(arg):
    if arg == "-":
        return sys.stdin.read()
    if arg.startswith("@"):
        path = arg[1:]
        if not os.path.isfile(path):
            raise PostError("body file not found: %s" % path, code=1)
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    return arg


def main(argv):
    if len(argv) < 2:
        print("usage: matrix-post.py <room-key|!room:id> <text|@file|-> [attachment ...]", file=sys.stderr)
        return 1
    key, body_arg, files = argv[0], argv[1], argv[2:]
    try:
        for path in files:
            if not os.path.isfile(path):
                raise PostError("attachment not found: %s" % path, code=1)
        text = read_body(body_arg)
        if not text.strip():
            raise PostError("empty message", code=1)
        room = resolve_room(key)
        client = Matrix(homeserver_url(), load_token())
        event = client.send(room, text_content(text))
        failed = 0
        for path in files:
            try:
                client.send(room, client.upload(path))
            except (PostError, OSError) as exc:
                failed += 1
                print("matrix-post.py: attachment %s failed: %s" % (os.path.basename(path), exc), file=sys.stderr)
    except PostError as exc:
        print("matrix-post.py: %s" % exc, file=sys.stderr)
        return exc.code
    print("matrix-post.py: posted to %s, event id=%s" % (room, event))
    return 3 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
