"""Tests for chassis/scripts/matrix-control-listener.py.

A FakeMatrix stands in for the homeserver. The listener is the Matrix twin of
discord-control-listener.py, so several tests pin that it reuses the Discord
listener's parser, flag writer and acks rather than carrying its own copies.
"""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "matrix-control-listener.py"
spec = importlib.util.spec_from_file_location("matrix_control_listener", MODULE_PATH)
ml = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ml)

PRINCIPAL = "@principal:example.org"
OTHER = "@someone:example.org"
BOT = "@bot:example.org"
ROOM = "!control:example.org"


def now_ms():
    return int(time.time() * 1000)


def event(body, sender=PRINCIPAL, age_s=5, msgtype="m.text", etype="m.room.message", **content_extra):
    content = {"msgtype": msgtype, "body": body}
    content.update(content_extra)
    return {
        "type": etype,
        "sender": sender,
        "event_id": "$%d" % abs(hash((body, sender, age_s))),
        "origin_server_ts": now_ms() - age_s * 1000,
        "content": content,
    }


class FakeMatrix:
    """`pages` is a list of chunks, one per forward poll."""

    def __init__(self, pages=None, encrypted=False, fail_send=False):
        self.pages = list(pages or [])
        self.encrypted = encrypted
        self.fail_send = fail_send
        self.sent = []
        self.calls = []
        self._n = 0

    def messages(self, room_id, from_token=None, direction="f", limit=50):
        self.calls.append((room_id, from_token, direction))
        if direction == "b":
            return {"start": "t0", "chunk": [event("halt", age_s=1)]}
        chunk = self.pages.pop(0) if self.pages else []
        self._n += 1
        return {"start": from_token, "end": "t%d" % self._n, "chunk": chunk}

    def room_encrypted(self, room_id):
        return self.encrypted

    def send(self, room_id, content):
        if self.fail_send:
            raise ml.MatrixError("HTTP 500", 500)
        self.sent.append((room_id, content))
        return {"event_id": "$ack"}


def make(tmp_path, pages=None, primed=True, **kw):
    home = tmp_path / "customer"
    (home / "scheduled-tasks").mkdir(parents=True)
    (home / "state").mkdir(parents=True)
    if primed:
        (home / "state" / "matrix-control-listener-cursor.json").write_text(json.dumps({ROOM: "t0"}))
    fake = FakeMatrix(pages, **kw)
    return ml.MatrixListener(str(home), PRINCIPAL, [ROOM], fake), fake, home


def read(path):
    return json.loads(Path(path).read_text())


def halt_flag(home):
    path = home / "scheduled-tasks" / "halt.json"
    return read(path) if path.exists() else None


# ---------------------------------------------------------------------------
# Reuse of the Discord listener
# ---------------------------------------------------------------------------


def test_parser_and_flag_writer_are_the_discord_listeners():
    assert ml.parse_command is ml.control.parse_command
    assert ml.set_flag is ml.control.set_flag
    assert issubclass(ml.MatrixListener, ml.control.Listener)
    assert ml.MatrixListener.apply is ml.control.Listener.apply


def test_discord_listener_keeps_its_own_enabled_by():
    assert ml.control.Listener.SOURCE == "discord-control-listener"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def test_cold_start_takes_position_and_acts_on_nothing(tmp_path):
    inst, fake, home = make(tmp_path, primed=False)
    inst.poll_once()
    assert halt_flag(home) is None
    assert fake.sent == []
    assert fake.calls == [(ROOM, None, "b")]
    assert read(home / "state" / "matrix-control-listener-cursor.json") == {ROOM: "t0"}


def test_principal_halt_writes_the_shared_flag_and_acks(tmp_path):
    inst, fake, home = make(tmp_path, [[event("halt too noisy")]])
    inst.poll_once()
    flag = halt_flag(home)
    assert flag["enabled"] is True
    assert flag["reason"] == "too noisy"
    assert flag["enabled_by"] == "matrix-control-listener:%s" % PRINCIPAL
    assert set(flag) == {"enabled", "enabled_at", "enabled_by", "auto_lift_after", "reason"}
    (room, content), = fake.sent
    assert room == ROOM
    assert content["msgtype"] == "m.text"
    assert "HALTED" in content["body"]
    assert "<code>resume</code>" in content["formatted_body"]


def test_resume_and_conserve_round_trip(tmp_path):
    inst, _, home = make(tmp_path, [[event("halt")], [event("conserve 2h quota")], [event("resume")]])
    inst.poll_once()
    inst.poll_once()
    cons = read(home / "scheduled-tasks" / "conservation-mode.json")
    assert cons["enabled"] is True and cons["auto_lift_after"]
    inst.poll_once()
    assert halt_flag(home)["enabled"] is False
    assert read(home / "scheduled-tasks" / "conservation-mode.json")["enabled"] is False


def test_unconserve_and_status(tmp_path):
    inst, fake, home = make(tmp_path, [[event("conserve")], [event("unconserve")], [event("status")]])
    inst.poll_once()
    inst.poll_once()
    inst.poll_once()
    assert read(home / "scheduled-tasks" / "conservation-mode.json")["enabled"] is False
    assert "halt: off" in fake.sent[-1][1]["body"]


def test_conversation_is_not_a_command(tmp_path):
    inst, fake, home = make(tmp_path, [[event("we should probably halt the drift alert")]])
    inst.poll_once()
    assert halt_flag(home) is None
    assert fake.sent == []


# ---------------------------------------------------------------------------
# Event rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ev,reason",
    [
        (event("halt", sender=OTHER), "sender"),
        (event("halt", sender="@principal:example.org.evil"), "sender"),
        (event("halt", sender="principal"), "sender"),
        (event("halt", age_s=601), "stale"),
        (event("halt", msgtype="m.notice"), "msgtype"),
        (event("halt", msgtype="m.emote"), "msgtype"),
        (event("halt", etype="m.room.encrypted"), "encrypted"),
        (event("halt", etype="m.reaction"), "type"),
        (event("* halt", **{"m.new_content": {"msgtype": "m.text", "body": "halt"},
                            "m.relates_to": {"rel_type": "m.replace", "event_id": "$old"}}), "edit"),
    ],
)
def test_events_that_must_never_act(tmp_path, ev, reason):
    assert ml.parse_event(ev, PRINCIPAL) == (None, reason)
    inst, fake, home = make(tmp_path, [[ev]])
    inst.poll_once()
    assert halt_flag(home) is None
    assert fake.sent == []


def test_redacted_and_untimestamped_events_are_dropped():
    ev = event("halt")
    ev["content"] = {}
    assert ml.parse_event(ev, PRINCIPAL) == (None, "redacted")
    ev = event("halt")
    del ev["origin_server_ts"]
    assert ml.parse_event(ev, PRINCIPAL) == (None, "no-timestamp")


def test_ten_minute_boundary():
    assert ml.parse_event(event("halt", age_s=590), PRINCIPAL)[0] == "halt"
    assert ml.parse_event(event("halt", age_s=610), PRINCIPAL) == (None, "stale")


def test_reply_fallback_is_stripped(tmp_path):
    ev = event("> <@bot:example.org> HALTED. Every heartbeat...\n\nresume",
               **{"m.relates_to": {"m.in_reply_to": {"event_id": "$x"}}})
    assert ml.parse_event(ev, PRINCIPAL) == ("resume", "")


def test_quoted_text_without_reply_relation_is_not_stripped():
    ev = event("> halt")
    body, _ = ml.parse_event(ev, PRINCIPAL)
    assert ml.parse_command(body) is None


@pytest.mark.parametrize("text", ["@bot:example.org: halt", "@bot:example.org halt", "@bot:example.org:8448: halt"])
def test_leading_user_id_mention_is_allowed(text):
    body, _ = ml.parse_event(event(text), PRINCIPAL)
    assert ml.parse_command(body)[0] == "halt"


def test_bot_acks_are_never_read_as_commands(tmp_path):
    inst, fake, home = make(tmp_path, [[event("halt", sender=BOT)]])
    inst.poll_once()
    assert halt_flag(home) is None


# ---------------------------------------------------------------------------
# Cursor and failure handling
# ---------------------------------------------------------------------------


def test_cursor_advances_and_persists(tmp_path):
    inst, _, home = make(tmp_path, [[event("hello")]])
    inst.poll_once()
    assert read(home / "state" / "matrix-control-listener-cursor.json") == {ROOM: "t1"}


def test_a_command_is_not_reapplied_on_the_next_poll(tmp_path):
    inst, fake, _ = make(tmp_path, [[event("halt")], []])
    inst.poll_once()
    inst.poll_once()
    assert len(fake.sent) == 1


def test_state_is_written_even_when_the_ack_fails(tmp_path):
    inst, _, home = make(tmp_path, [[event("halt")]], fail_send=True)
    inst.poll_once()
    assert halt_flag(home)["enabled"] is True


def test_http_errors_do_not_kill_the_loop(tmp_path):
    inst, fake, _ = make(tmp_path)

    def boom(*a, **k):
        raise ml.MatrixError("HTTP 502", 502)

    fake.messages = boom
    inst.poll_once()


def test_encrypted_control_room_refuses_to_arm(tmp_path):
    inst, _, _ = make(tmp_path, encrypted=True)
    assert inst.refuse_encrypted_rooms() is False
    inst, _, _ = make(tmp_path / "b", encrypted=False)
    assert inst.refuse_encrypted_rooms() is True


# ---------------------------------------------------------------------------
# Configuration (no network)
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"access_token": "secret-token"}))
    monkeypatch.setenv("CUSTOMER_HOME", str(tmp_path))
    monkeypatch.setenv("CHASSIS_MATRIX_TOKEN_FILE", str(token))
    monkeypatch.setenv("CHASSIS_MATRIX_PRINCIPAL", PRINCIPAL)
    monkeypatch.setenv("CHASSIS_MATRIX_DM_ROOM", ROOM)
    monkeypatch.delenv("CHASSIS_MATRIX_CONTROL_ROOMS", raising=False)
    return monkeypatch


def test_config_ok(cfg):
    config = ml.resolve_config(quiet=True)
    assert config is not None
    assert config[1:3] == (PRINCIPAL, [ROOM])
    assert ml.main(["--check"]) == 0


def test_control_rooms_override_the_dm_room(cfg):
    cfg.setenv("CHASSIS_MATRIX_CONTROL_ROOMS", "!a:x, !b:x")
    assert ml.resolve_config(quiet=True)[2] == ["!a:x", "!b:x"]


@pytest.mark.parametrize(
    "var,value",
    [
        ("CHASSIS_MATRIX_PRINCIPAL", ""),
        ("CHASSIS_MATRIX_PRINCIPAL", "Principal"),
        ("CHASSIS_MATRIX_PRINCIPAL", "100000000000000001"),
        ("CHASSIS_MATRIX_DM_ROOM", ""),
        ("CHASSIS_MATRIX_DM_ROOM", "#alias:example.org"),
        ("CHASSIS_MATRIX_TOKEN_FILE", "/nonexistent/token.json"),
        ("CUSTOMER_HOME", ""),
    ],
)
def test_config_fails_closed(cfg, var, value, capsys):
    cfg.setenv(var, value)
    if var == "CUSTOMER_HOME":
        cfg.delenv("CHASSIS_HOME", raising=False)
    assert ml.resolve_config(quiet=True) is None
    assert ml.main(["--check"]) == ml.EX_CONFIG
    ml.resolve_config(quiet=False)
    out = capsys.readouterr()
    assert "UNARMED" in out.out
    assert "secret-token" not in out.out + out.err


def test_missing_pagination_token_is_logged_not_silent(tmp_path, capsys):
    inst, fake, _ = make(tmp_path, primed=False)
    fake.messages = lambda *a, **k: {"chunk": []}
    inst.poll_once()
    assert "no pagination token" in capsys.readouterr().out
    assert ROOM not in inst.cursors
