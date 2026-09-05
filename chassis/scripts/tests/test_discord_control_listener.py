"""Behavioural coverage for the remote kill switch (behalfbot#550).

The property under test is not "the parser works". It is that the operator's
one remaining channel does the right thing under the conditions of the outage:
somebody who is not the principal cannot flip it, a restart does not replay a
four-day-old command, and a `halt` reaches disk before anything is said in
Discord (an ack that arrives without the state write is the lie that made #550
expensive - four days of alerts claiming a webhook was unset while it was set).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "discord-control-listener.py"

spec = importlib.util.spec_from_file_location("discord_control_listener", MODULE_PATH)
listener_mod = importlib.util.module_from_spec(spec)
sys.modules["discord_control_listener"] = listener_mod
spec.loader.exec_module(listener_mod)

PRINCIPAL = "100000000000000001"
OTHER = "100000000000000002"
CHANNEL = "222222222222222222"


class FakeDiscord:
    """Stands in for the REST client. Records what would have been posted."""

    def __init__(self, batches=None):
        self.batches = list(batches or [])
        self.posted = []

    def messages_after(self, channel_id, after):
        if not self.batches:
            return []
        return self.batches.pop(0)

    def post(self, channel_id, content):
        self.posted.append((channel_id, content))
        return {"id": "1"}


def message(msg_id, author, content):
    return {"id": str(msg_id), "author": {"id": author}, "content": content}


def make_listener(tmp_path, batches=None, discord=None):
    home = tmp_path / "customer"
    (home / "scheduled-tasks").mkdir(parents=True)
    (home / "state").mkdir(parents=True)
    discord = discord or FakeDiscord(batches)
    inst = listener_mod.Listener(str(home), PRINCIPAL, [CHANNEL], discord)
    return inst, discord, home


def read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Lexicon
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("halt", "halt"),
        ("HALT", "halt"),
        ("halt!", "halt"),
        ("mute", "halt"),
        ("silence", "halt"),
        ("<@100000000000000003> halt", "halt"),
        ("<@!100000000000000003>halt", "halt"),
        ("resume", "resume"),
        ("unmute", "resume"),
        ("unhalt", "resume"),
        ("conserve", "conserve"),
        ("throttle", "conserve"),
        ("conservation on", "conserve"),
        ("Conservation Off", "unconserve"),
        ("unconserve", "unconserve"),
        ("status", "status"),
        ("  status  ", "status"),
    ],
)
def test_lexicon_recognised(text, expected):
    parsed = listener_mod.parse_command(text)
    assert parsed is not None, text
    assert parsed[0] == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "we should probably halt the drift alert",
        "can you resume the briefing please",
        "status of the deploy?",
        "hello",
        "stop",
        "halting",
        "resumed",
    ],
)
def test_conversation_is_not_a_command(text):
    """A control word inside a sentence is conversation.

    The listener has no model and no context. Matching a substring would mean
    the operator cannot discuss the kill switch in the channel the kill switch
    listens on.
    """
    assert listener_mod.parse_command(text) is None


def test_halt_takes_duration_and_reason():
    command, duration, reason = listener_mod.parse_command("halt 2h runaway repo-drift")
    assert command == "halt"
    assert reason == "runaway repo-drift"
    parsed = datetime.strptime(duration, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    delta = parsed - datetime.now(timezone.utc)
    assert timedelta(hours=1, minutes=55) < delta < timedelta(hours=2, minutes=5)


def test_halt_without_duration_has_no_auto_lift():
    """The default must be indefinite.

    An auto-resume that fires while the operator is still off-grid restarts the
    outage, and they are by definition not there to notice.
    """
    command, duration, reason = listener_mod.parse_command("halt spamming me")
    assert (command, duration, reason) == ("halt", None, "spamming me")


def test_reason_that_looks_like_a_duration_is_still_a_duration():
    command, duration, reason = listener_mod.parse_command("halt 30m")
    assert command == "halt"
    assert duration is not None
    assert reason is None


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------


def test_non_principal_halt_is_ignored(tmp_path):
    inst, discord, home = make_listener(tmp_path, [[message(10, OTHER, "halt")]])
    inst.poll_once()
    assert not (home / "scheduled-tasks" / "halt.json").exists()
    assert discord.posted == []


def test_principal_halt_writes_the_flag_and_acks(tmp_path):
    inst, discord, home = make_listener(tmp_path, [[message(10, PRINCIPAL, "halt too noisy")]])
    inst.poll_once()
    flag = read(home / "scheduled-tasks" / "halt.json")
    assert flag["enabled"] is True
    assert flag["reason"] == "too noisy"
    assert flag["auto_lift_after"] is None
    assert flag["enabled_by"] == "discord-control-listener:%s" % PRINCIPAL
    assert set(flag) == {"enabled", "enabled_at", "enabled_by", "auto_lift_after", "reason"}
    assert len(discord.posted) == 1
    assert "HALTED" in discord.posted[0][1]


def test_ack_states_what_halt_does_not_cover(tmp_path):
    """The ack is the only documentation the operator has in the moment."""
    inst, discord, _ = make_listener(tmp_path, [[message(10, PRINCIPAL, "halt")]])
    inst.poll_once()
    ack = discord.posted[0][1]
    assert "critical" in ack
    assert "launchd" in ack
    assert "resume" in ack
    assert len(ack) < 2000


def test_config_refuses_to_arm_without_a_principal(monkeypatch):
    monkeypatch.setenv("CUSTOMER_HOME", "/tmp/x")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    monkeypatch.setenv("DISCORD_PRIMARY_CHANNEL_ID", CHANNEL)
    monkeypatch.delenv("CHASSIS_PRINCIPAL_USER_ID", raising=False)
    monkeypatch.delenv("INSTALLER_DISCORD_USER_ID", raising=False)
    assert listener_mod.resolve_config(quiet=True) is None
    assert listener_mod.main(["--check"]) == listener_mod.EX_CONFIG


def test_config_accepts_the_bootstrap_variable_name(monkeypatch):
    monkeypatch.setenv("CUSTOMER_HOME", "/tmp/x")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    monkeypatch.setenv("DISCORD_PRIMARY_CHANNEL_ID", CHANNEL)
    monkeypatch.delenv("CHASSIS_PRINCIPAL_USER_ID", raising=False)
    monkeypatch.setenv("INSTALLER_DISCORD_USER_ID", PRINCIPAL)
    config = listener_mod.resolve_config(quiet=True)
    assert config is not None
    assert config[1] == PRINCIPAL


def test_config_rejects_a_non_snowflake_principal(monkeypatch):
    monkeypatch.setenv("CUSTOMER_HOME", "/tmp/x")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")
    monkeypatch.setenv("DISCORD_PRIMARY_CHANNEL_ID", CHANNEL)
    monkeypatch.setenv("CHASSIS_PRINCIPAL_USER_ID", "sean")
    assert listener_mod.resolve_config(quiet=True) is None


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


def test_resume_clears_both_flags(tmp_path):
    inst, _, home = make_listener(
        tmp_path,
        [
            [message(10, PRINCIPAL, "halt")],
            [message(11, PRINCIPAL, "conserve")],
            [message(12, PRINCIPAL, "resume")],
        ],
    )
    inst.poll_once()
    inst.poll_once()
    inst.poll_once()
    assert read(home / "scheduled-tasks" / "halt.json")["enabled"] is False
    assert read(home / "scheduled-tasks" / "conservation-mode.json")["enabled"] is False


def test_conserve_does_not_touch_the_halt_flag(tmp_path):
    inst, _, home = make_listener(tmp_path, [[message(10, PRINCIPAL, "conserve 1d quota")]])
    inst.poll_once()
    cons = read(home / "scheduled-tasks" / "conservation-mode.json")
    assert cons["enabled"] is True
    assert cons["reason"] == "quota"
    assert cons["auto_lift_after"] is not None
    assert not (home / "scheduled-tasks" / "halt.json").exists()


def test_status_changes_nothing(tmp_path):
    inst, discord, home = make_listener(tmp_path, [[message(10, PRINCIPAL, "status")]])
    inst.poll_once()
    assert not (home / "scheduled-tasks" / "halt.json").exists()
    assert "halt: off" in discord.posted[0][1]


def test_status_reports_an_active_halt(tmp_path):
    inst, discord, _ = make_listener(
        tmp_path,
        [[message(10, PRINCIPAL, "halt noisy")], [message(11, PRINCIPAL, "status")]],
    )
    inst.poll_once()
    inst.poll_once()
    assert "halt: ON" in discord.posted[1][1]
    assert "noisy" in discord.posted[1][1]


def test_batch_is_applied_oldest_first(tmp_path):
    """Discord returns newest first. A halt and a resume in one batch must land
    in the order they were sent, not reversed."""
    inst, _, home = make_listener(
        tmp_path,
        [[message(12, PRINCIPAL, "resume"), message(11, PRINCIPAL, "halt")]],
    )
    inst.poll_once()
    assert read(home / "scheduled-tasks" / "halt.json")["enabled"] is False


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------


def test_cold_start_reads_forward_from_now(tmp_path):
    """With no stored cursor the listener starts at the lookback floor, never
    at the beginning of the channel."""
    inst, _, _ = make_listener(tmp_path)
    floor = int(inst.cursors[CHANNEL])
    now = int(listener_mod.snowflake_for(datetime.now(timezone.utc)))
    hour_ago = int(listener_mod.snowflake_for(datetime.now(timezone.utc) - timedelta(hours=1)))
    one_minute = 60_000 << 22
    assert abs(floor - hour_ago) < one_minute
    assert floor < now


def test_stale_persisted_cursor_is_capped(tmp_path):
    """A cursor from four days ago would replay four days of backlog on the
    first poll after a restart - including commands the operator has since
    countermanded."""
    home = tmp_path / "customer"
    (home / "scheduled-tasks").mkdir(parents=True)
    (home / "state").mkdir(parents=True)
    old = listener_mod.snowflake_for(datetime.now(timezone.utc) - timedelta(days=4))
    with open(home / "state" / "control-listener-cursor.json", "w", encoding="utf-8") as fh:
        json.dump({CHANNEL: old}, fh)
    inst = listener_mod.Listener(str(home), PRINCIPAL, [CHANNEL], FakeDiscord())
    assert int(inst.cursors[CHANNEL]) > int(old)


def test_recent_persisted_cursor_is_honoured(tmp_path):
    home = tmp_path / "customer"
    (home / "scheduled-tasks").mkdir(parents=True)
    (home / "state").mkdir(parents=True)
    recent = listener_mod.snowflake_for(datetime.now(timezone.utc) - timedelta(minutes=5))
    with open(home / "state" / "control-listener-cursor.json", "w", encoding="utf-8") as fh:
        json.dump({CHANNEL: recent}, fh)
    inst = listener_mod.Listener(str(home), PRINCIPAL, [CHANNEL], FakeDiscord())
    assert inst.cursors[CHANNEL] == recent


def test_cursor_advances_past_non_command_messages(tmp_path):
    inst, _, home = make_listener(tmp_path, [[message(99, OTHER, "hello there")]])
    inst.poll_once()
    assert inst.cursors[CHANNEL] == "99"
    assert read(home / "state" / "control-listener-cursor.json")[CHANNEL] == "99"


def test_a_command_is_not_reapplied_on_the_next_poll(tmp_path):
    inst, discord, _ = make_listener(tmp_path, [[message(10, PRINCIPAL, "halt")], []])
    inst.poll_once()
    inst.poll_once()
    assert len(discord.posted) == 1


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_state_is_written_before_the_ack(tmp_path):
    """If Discord is unreachable the halt must still take effect. The ack is
    the operator's confirmation, not the mechanism."""
    home = tmp_path / "customer"
    (home / "scheduled-tasks").mkdir(parents=True)
    (home / "state").mkdir(parents=True)

    class DeadDiscord(FakeDiscord):
        def post(self, channel_id, content):
            return None

    inst = listener_mod.Listener(
        str(home), PRINCIPAL, [CHANNEL], DeadDiscord([[message(10, PRINCIPAL, "halt")]])
    )
    inst.poll_once()
    assert read(home / "scheduled-tasks" / "halt.json")["enabled"] is True


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits")
def test_an_unwritable_state_dir_is_reported_not_silent(tmp_path):
    inst, discord, home = make_listener(tmp_path, [[message(10, PRINCIPAL, "halt")]])
    os.chmod(home / "scheduled-tasks", 0o500)
    try:
        inst.poll_once()
    finally:
        os.chmod(home / "scheduled-tasks", 0o700)
    assert discord.posted, "a failed write must still tell the operator"
    assert "Could not apply" in discord.posted[0][1]


def test_http_failures_return_none_rather_than_raising(monkeypatch):
    """The REST client must never take the process down.

    Being alive to hear `resume` is the entire value of this listener. A raised
    exception on a 500 or a DNS blip would end it.
    """
    calls = []

    def boom(req, timeout=None):
        calls.append(req.full_url)
        raise urllib.error.URLError("dns is down")

    monkeypatch.setattr(listener_mod.urllib.request, "urlopen", boom)
    monkeypatch.setattr(listener_mod.time, "sleep", lambda _s: None)
    client = listener_mod.Discord("token")
    assert client.messages_after(CHANNEL, "1") is None
    assert len(calls) == 3, "three attempts, then give up until the next poll"


def test_forbidden_names_the_permission_and_does_not_retry(monkeypatch):
    attempts = []

    def forbidden(req, timeout=None):
        attempts.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(listener_mod.urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(listener_mod.time, "sleep", lambda _s: None)
    client = listener_mod.Discord("token")
    assert client.post(CHANNEL, "hello") is None
    assert len(attempts) == 1, "a permission problem is not transient"
