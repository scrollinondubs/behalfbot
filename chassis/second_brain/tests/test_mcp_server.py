#!/usr/bin/env python3
"""test_mcp_server.py - End-to-end test of the `secondbrain` MCP server over stdio.

Spawns chassis/second_brain/mcp_server.py as a subprocess - exactly how Claude
Code launches it - against a throwaway Obsidian vault, speaks newline-delimited
JSON-RPC on its stdin/stdout, and exercises every tool: initialize handshake,
tools/list, create_doc, read_doc, append_to_doc, search, list_recent,
get_deeplink. No network, no live backend.

Requires the `mcp` package (in requirements.txt); the whole module is skipped
when it is not importable so the rest of the suite stays runnable on a bare
interpreter.

Run:
    python3 -m pytest chassis/second_brain/tests/test_mcp_server.py -v
"""
from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVER_PATH = REPO_ROOT / "chassis" / "second_brain" / "mcp_server.py"

try:
    import mcp  # noqa: F401

    HAVE_MCP = True
except ImportError:
    HAVE_MCP = False

READ_TIMEOUT_SECONDS = 30


@unittest.skipUnless(HAVE_MCP, "mcp SDK not installed (pip install mcp)")
class SecondBrainMcpServerTest(unittest.TestCase):
    """One server process for the whole class - the handshake is stateful."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = Path(tempfile.mkdtemp(prefix="secondbrain-mcp-e2e-"))
        cls.vault = cls._tmp / "vault"
        (cls.vault / "Briefings").mkdir(parents=True)
        (cls.vault / "Inbox.md").write_text(
            "# Inbox\n\nCall the notary about the apartment paperwork.\n",
            encoding="utf-8",
        )
        customer_home = cls._tmp / "customer"
        customer_home.mkdir()
        (customer_home / "chassis.config.yaml").write_text(
            "second_brain:\n"
            "  backend: obsidian\n"
            "  mode: adapter\n"
            "  obsidian:\n"
            f"    vault_path: {cls.vault}\n"
            "    vault_name: e2e-vault\n",
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["CHASSIS_HOME"] = str(customer_home)
        cls.proc = subprocess.Popen(
            [sys.executable, str(SERVER_PATH)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,
        )
        cls._next_id = 0
        cls._initialize()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.wait(timeout=5)
        shutil.rmtree(cls._tmp, ignore_errors=True)

    # -- JSON-RPC plumbing ----------------------------------------------------

    @classmethod
    def _send(cls, message: dict) -> None:
        cls.proc.stdin.write(json.dumps(message) + "\n")
        cls.proc.stdin.flush()

    @classmethod
    def _read_response(cls, expect_id: int) -> dict:
        deadline_fd = cls.proc.stdout
        while True:
            ready, _, _ = select.select([deadline_fd], [], [], READ_TIMEOUT_SECONDS)
            if not ready:
                stderr = cls.proc.stderr.read() if cls.proc.poll() is not None else ""
                raise AssertionError(
                    f"no response within {READ_TIMEOUT_SECONDS}s waiting for id={expect_id}; "
                    f"server rc={cls.proc.poll()}, stderr tail: {stderr[-2000:]}"
                )
            line = deadline_fd.readline()
            if not line:
                raise AssertionError(
                    f"server closed stdout waiting for id={expect_id} "
                    f"(rc={cls.proc.poll()})"
                )
            message = json.loads(line)
            if message.get("id") == expect_id:
                return message
            # Skip server-initiated notifications/logs.

    @classmethod
    def _request(cls, method: str, params: dict | None = None) -> dict:
        cls._next_id += 1
        request_id = cls._next_id
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        cls._send(payload)
        return cls._read_response(request_id)

    @classmethod
    def _initialize(cls) -> None:
        response = cls._request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "chassis-e2e-test", "version": "0"},
            },
        )
        assert "result" in response, f"initialize failed: {response}"
        cls.server_info = response["result"]
        cls._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _call_tool(self, name: str, arguments: dict) -> dict:
        response = self._request(
            "tools/call", {"name": name, "arguments": arguments}
        )
        self.assertIn("result", response, f"{name} errored: {response}")
        result = response["result"]
        self.assertFalse(
            result.get("isError"),
            f"{name} returned isError: {json.dumps(result)[:800]}",
        )
        return result

    @staticmethod
    def _text_of(result: dict) -> str:
        return "\n".join(
            item.get("text", "")
            for item in result.get("content", [])
            if item.get("type") == "text"
        )

    # -- the actual drill (numbered: later steps read earlier steps' writes) --

    def test_01_server_identifies_as_secondbrain(self) -> None:
        self.assertEqual(self.server_info["serverInfo"]["name"], "secondbrain")

    def test_02_tools_list_exposes_the_protocol_surface(self) -> None:
        response = self._request("tools/list")
        tools = {tool["name"] for tool in response["result"]["tools"]}
        self.assertEqual(
            tools,
            {
                "create_doc",
                "append_to_doc",
                "read_doc",
                "search",
                "list_recent",
                "get_deeplink",
            },
        )

    def test_03_create_doc_returns_id_and_deeplink(self) -> None:
        result = self._call_tool(
            "create_doc",
            {
                "parent": "Briefings/",
                "title": "2026-07-09-e2e",
                "body": "# E2E\n\nqueued from the drill\n",
            },
        )
        text = self._text_of(result)
        self.assertIn("Briefings/2026-07-09-e2e.md", text)
        self.assertIn("obsidian://open?vault=e2e-vault", text)
        self.assertTrue((self.vault / "Briefings" / "2026-07-09-e2e.md").is_file())

    def test_04_read_doc_round_trips(self) -> None:
        result = self._call_tool(
            "read_doc", {"doc_id": "Briefings/2026-07-09-e2e.md"}
        )
        self.assertIn("queued from the drill", self._text_of(result))

    def test_05_append_is_visible_in_next_read(self) -> None:
        self._call_tool(
            "append_to_doc",
            {
                "doc_id": "Briefings/2026-07-09-e2e.md",
                "content": "appended-by-e2e-marker",
            },
        )
        result = self._call_tool(
            "read_doc", {"doc_id": "Briefings/2026-07-09-e2e.md"}
        )
        self.assertIn("appended-by-e2e-marker", self._text_of(result))

    def test_06_search_finds_seeded_content(self) -> None:
        result = self._call_tool("search", {"query": "notary", "limit": 5})
        self.assertIn("Inbox", self._text_of(result))

    def test_07_list_recent_sees_the_created_doc(self) -> None:
        since = (datetime.now() - timedelta(hours=1)).isoformat()
        until = (datetime.now() + timedelta(hours=1)).isoformat()
        result = self._call_tool(
            "list_recent", {"since": since, "until": until, "limit": 10}
        )
        self.assertIn("Briefings/2026-07-09-e2e.md", self._text_of(result))

    def test_08_list_recent_empty_future_window(self) -> None:
        since = (datetime.now() + timedelta(days=10)).isoformat()
        until = (datetime.now() + timedelta(days=11)).isoformat()
        result = self._call_tool(
            "list_recent", {"since": since, "until": until}
        )
        self.assertNotIn("2026-07-09-e2e", self._text_of(result))

    def test_09_get_deeplink(self) -> None:
        result = self._call_tool("get_deeplink", {"doc_id": "Inbox.md"})
        self.assertIn("obsidian://open?vault=e2e-vault&file=Inbox.md", self._text_of(result))

    def test_10_bad_datetime_surfaces_as_tool_error_not_crash(self) -> None:
        response = self._request(
            "tools/call",
            {
                "name": "list_recent",
                "arguments": {"since": "not-a-date", "until": "also-not"},
            },
        )
        result = response.get("result", {})
        self.assertTrue(result.get("isError"), f"expected isError, got {response}")
        self.assertIn("ISO-8601", json.dumps(result))
        # Server must still answer after the failed call.
        alive = self._call_tool("get_deeplink", {"doc_id": "Inbox.md"})
        self.assertIn("Inbox.md", self._text_of(alive))


if __name__ == "__main__":
    unittest.main(verbosity=2)
