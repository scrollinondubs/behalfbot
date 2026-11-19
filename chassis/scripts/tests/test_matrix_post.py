"""Tests for chassis/scripts/matrix-post.py against a fake homeserver.

The fake is a local http.server on an ephemeral port. Nothing here contacts a
real homeserver. The script is run as a subprocess where the exit code and the
printed output are the contract (token never printed, status lines), and
imported where the formatting rules are the contract.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "matrix-post.py"
TOKEN = "syt_fake_token_do_not_print_42"

spec = importlib.util.spec_from_file_location("matrix_post", SCRIPT)
mp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mp)


class FakeHomeserver:
    def __init__(self):
        self.requests = []
        self.fail_send = False
        self.fail_upload = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, code, payload):
                raw = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "auth": self.headers.get("Authorization"),
                        "ctype": self.headers.get("Content-Type"),
                        "body": body,
                    }
                )
                if self.headers.get("Authorization") != "Bearer %s" % TOKEN:
                    return self._reply(401, {"errcode": "M_UNKNOWN_TOKEN"})
                if "/send/m.room.message/" in self.path:
                    if outer.fail_send:
                        return self._reply(403, {"errcode": "M_FORBIDDEN"})
                    return self._reply(200, {"event_id": "$ev%d" % len(outer.requests)})
                if self.path.startswith("/_matrix/media/v3/upload"):
                    if outer.fail_upload:
                        return self._reply(500, {"errcode": "M_UNKNOWN"})
                    return self._reply(200, {"content_uri": "mxc://example.org/abc"})
                return self._reply(404, {"errcode": "M_UNRECOGNIZED"})

            do_PUT = _handle
            do_POST = _handle
            do_GET = _handle

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def sends(self):
        return [json.loads(r["body"]) for r in self.requests if "/send/m.room.message/" in r["path"]]


@pytest.fixture
def hs():
    server = FakeHomeserver()
    yield server
    server.close()


@pytest.fixture
def env(tmp_path, hs):
    token_file = tmp_path / "matrix-token.json"
    token_file.write_text(json.dumps({"access_token": TOKEN, "user_id": "@bot:example.org"}))
    e = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "CUSTOMER_HOME": str(tmp_path),
        "CHASSIS_MATRIX_HOMESERVER": hs.url,
        "CHASSIS_MATRIX_TOKEN_FILE": "matrix-token.json",
        "CHASSIS_MATRIX_ALERTS_ROOM": "!alerts:example.org",
        "CHASSIS_MATRIX_OPS_ROOM": "!ops:example.org",
        "CHASSIS_MATRIX_DM_ROOM": "!dm:example.org",
        "CHASSIS_MATRIX_DOCKERENV_PATH": str(tmp_path / "no-dockerenv"),
    }
    return e


def run(env, *args, stdin=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], env=env, input=stdin, capture_output=True, text=True, timeout=30
    )


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


def test_posts_m_text_with_formatted_body(env, hs):
    r = run(env, "alerts", "**Disk** at `91%` see [dash](https://d.example/x)")
    assert r.returncode == 0, r.stderr
    (content,) = hs.sends()
    assert content["msgtype"] == "m.text"
    assert content["body"] == "**Disk** at `91%` see [dash](https://d.example/x)"
    assert content["format"] == "org.matrix.custom.html"
    assert content["formatted_body"] == (
        '<strong>Disk</strong> at <code>91%</code> see <a href="https://d.example/x">dash</a>'
    )
    put = [q for q in hs.requests if q["method"] == "PUT"][0]
    assert "/rooms/%21alerts%3Aexample.org/send/m.room.message/" in put["path"]


def test_token_goes_in_the_header_and_is_never_printed(env, hs):
    r = run(env, "ops", "hello")
    assert r.returncode == 0
    assert all(q["auth"] == "Bearer %s" % TOKEN for q in hs.requests)
    assert all(TOKEN not in q["path"] for q in hs.requests)
    assert TOKEN not in r.stdout + r.stderr
    hs.fail_send = True
    r = run(env, "ops", "hello")
    assert r.returncode == 3
    assert "M_FORBIDDEN" in r.stderr
    assert TOKEN not in r.stdout + r.stderr


def test_body_from_stdin_and_from_file(env, hs, tmp_path):
    assert run(env, "alerts", "-", stdin="@not/a/path\nline two").returncode == 0
    body_file = tmp_path / "body.txt"
    body_file.write_text("from a file")
    assert run(env, "alerts", "@%s" % body_file).returncode == 0
    first, second = hs.sends()
    assert first["body"] == "@not/a/path\nline two"
    assert first["formatted_body"] == "@not/a/path<br>line two"
    assert second["body"] == "from a file"


def test_attachments_upload_after_the_text(env, hs, tmp_path):
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG fake")
    log = tmp_path / "run.log"
    log.write_text("log")
    r = run(env, "alerts", "see attached", str(png), str(log))
    assert r.returncode == 0, r.stderr
    sends = hs.sends()
    assert [s["msgtype"] for s in sends] == ["m.text", "m.image", "m.file"]
    assert sends[1]["url"] == "mxc://example.org/abc"
    assert sends[1]["info"]["mimetype"] == "image/png"
    uploads = [q for q in hs.requests if q["path"].startswith("/_matrix/media/v3/upload")]
    assert uploads[0]["ctype"] == "image/png"
    assert uploads[0]["body"] == b"\x89PNG fake"


def test_failed_upload_still_sends_the_text_and_exits_nonzero(env, hs, tmp_path):
    png = tmp_path / "shot.png"
    png.write_bytes(b"x")
    hs.fail_upload = True
    r = run(env, "alerts", "text first", str(png))
    assert r.returncode == 3
    assert [s["msgtype"] for s in hs.sends()] == ["m.text"]


def test_missing_attachment_is_refused_before_anything_is_sent(env, hs):
    r = run(env, "alerts", "x", "/nonexistent/file.png")
    assert r.returncode == 1
    assert hs.requests == []


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_missing_token_file_exits_2_without_contacting_the_server(env, hs):
    env["CHASSIS_MATRIX_TOKEN_FILE"] = "nope.json"
    r = run(env, "alerts", "x")
    assert r.returncode == 2
    assert hs.requests == []


def test_token_file_without_access_token(env, hs, tmp_path):
    (tmp_path / "bad.json").write_text(json.dumps({"user_id": "@bot:example.org"}))
    env["CHASSIS_MATRIX_TOKEN_FILE"] = str(tmp_path / "bad.json")
    assert run(env, "alerts", "x").returncode == 2


def test_unreachable_homeserver_exits_3(env):
    env["CHASSIS_MATRIX_HOMESERVER"] = "http://127.0.0.1:9"
    r = run(env, "alerts", "x")
    assert r.returncode == 3
    assert TOKEN not in r.stdout + r.stderr


def test_no_room_exits_2(env, hs):
    for key in ("CHASSIS_MATRIX_ALERTS_ROOM", "CHASSIS_MATRIX_OPS_ROOM", "CHASSIS_MATRIX_DM_ROOM"):
        env.pop(key)
    assert run(env, "alerts", "x").returncode == 2
    assert hs.requests == []


@pytest.mark.parametrize(
    "key,setvars,expected",
    [
        ("alerts", {"CHASSIS_MATRIX_ALERTS_ROOM": "!a:x"}, "!a:x"),
        ("alerts", {"CHASSIS_MATRIX_OPS_ROOM": "!o:x"}, "!o:x"),
        ("alerts", {"CHASSIS_MATRIX_DM_ROOM": "!d:x"}, "!d:x"),
        ("ops", {"CHASSIS_MATRIX_ALERTS_ROOM": "!a:x", "CHASSIS_MATRIX_OPS_ROOM": "!o:x"}, "!o:x"),
        ("dm", {"CHASSIS_MATRIX_DM_ROOM": "!d:x", "CHASSIS_MATRIX_OPS_ROOM": "!o:x"}, "!d:x"),
        ("pacman", {"CHASSIS_MATRIX_PACMAN_ROOM": "!p:x", "CHASSIS_MATRIX_OPS_ROOM": "!o:x"}, "!p:x"),
        ("pacman", {"CHASSIS_MATRIX_OPS_ROOM": "!o:x"}, "!o:x"),
        ("!literal:x", {}, "!literal:x"),
    ],
)
def test_room_resolution(monkeypatch, key, setvars, expected):
    for var in list(os.environ):
        if var.startswith("CHASSIS_MATRIX_") and var.endswith("_ROOM"):
            monkeypatch.delenv(var)
    for k, v in setvars.items():
        monkeypatch.setenv(k, v)
    assert mp.resolve_room(key) == expected


def test_dm_never_falls_back_to_a_shared_room(monkeypatch):
    monkeypatch.delenv("CHASSIS_MATRIX_DM_ROOM", raising=False)
    monkeypatch.setenv("CHASSIS_MATRIX_OPS_ROOM", "!o:x")
    with pytest.raises(mp.PostError):
        mp.resolve_room("dm")


def test_loopback_is_rewritten_inside_the_container(monkeypatch, tmp_path):
    marker = tmp_path / "dockerenv"
    marker.write_text("")
    monkeypatch.setenv("CHASSIS_MATRIX_DOCKERENV_PATH", str(marker))
    monkeypatch.setenv("CHASSIS_MATRIX_HOMESERVER", "http://127.0.0.1:8008")
    assert mp.homeserver_url() == "http://host.docker.internal:8008"
    monkeypatch.setenv("CHASSIS_MATRIX_HOMESERVER", "https://matrix.example.org")
    assert mp.homeserver_url() == "https://matrix.example.org"
    monkeypatch.setenv("CHASSIS_MATRIX_DOCKERENV_PATH", str(tmp_path / "absent"))
    monkeypatch.setenv("CHASSIS_MATRIX_HOMESERVER", "http://127.0.0.1:8008")
    assert mp.homeserver_url() == "http://127.0.0.1:8008"


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def test_html_in_the_message_is_escaped():
    content = mp.text_content("<script>alert(1)</script> & **ok**")
    assert content["formatted_body"] == "&lt;script&gt;alert(1)&lt;/script&gt; &amp; <strong>ok</strong>"


def test_only_http_links_become_anchors():
    content = mp.text_content("[x](javascript:alert(1)) [y](https://ok.example)")
    assert "javascript" not in content["formatted_body"].split("<a ")[-1].split(">")[0]
    assert content["formatted_body"].startswith("[x](javascript:alert(1))")
    assert '<a href="https://ok.example">y</a>' in content["formatted_body"]


def test_markup_inside_code_stays_literal():
    content = mp.text_content("`**not bold**` and **bold**")
    assert content["formatted_body"] == "<code>**not bold**</code> and <strong>bold</strong>"
