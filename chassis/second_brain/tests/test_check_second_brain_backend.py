#!/usr/bin/env python3
"""test_check_second_brain_backend.py - per-backend smoke reachability check.

The check this replaces knew only Notion and recorded itself as `notion_read`,
while chassis.config.yaml lists `second_brain_read` under
`success_criteria.smoke_tests`. The criterion named a check that never appeared
in the output, and Obsidian installs got a SKIP for the one thing most likely
to be wrong with them.

Obsidian gets the most coverage here because it is the only backend whose check
is pure filesystem, so every branch is reachable in a test without a network
fake - and because the read_only interaction is the subtle one: config states
intent, the filesystem states truth, and the check has to react to the
DISAGREEMENT rather than to either side alone.

Run:
    python3 -m pytest chassis/second_brain/tests/test_check_second_brain_backend.py -v
"""
from __future__ import annotations

import importlib.util
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

CHECK_PATH = REPO_ROOT / "chassis" / "scripts" / "check-second-brain-backend.py"
_spec = importlib.util.spec_from_file_location("check_second_brain_backend", CHECK_PATH)
checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checker)


class ObsidianCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name) / "vault"
        self.vault.mkdir()
        (self.vault / "note.md").write_text("# note", encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)

    def test_healthy_vault_passes_and_counts_notes(self) -> None:
        status, msg = checker.check_obsidian({"vault_path": str(self.vault)})
        self.assertEqual(status, "PASS")
        self.assertIn("readable + writable", msg)
        self.assertIn("1 notes", msg)

    def test_unset_vault_path_fails(self) -> None:
        status, msg = checker.check_obsidian({})
        self.assertEqual(status, "FAIL")
        self.assertIn("vault_path is not set", msg)

    def test_missing_vault_fails(self) -> None:
        status, msg = checker.check_obsidian(
            {"vault_path": str(self.vault / "nope")}
        )
        self.assertEqual(status, "FAIL")
        self.assertIn("does not exist", msg)

    def test_vault_path_pointing_at_a_file_fails(self) -> None:
        status, msg = checker.check_obsidian(
            {"vault_path": str(self.vault / "note.md")}
        )
        self.assertEqual(status, "FAIL")
        self.assertIn("not a directory", msg)

    def test_unreadable_vault_fails(self) -> None:
        with mock.patch.object(checker.os, "access",
                               lambda p, m: m not in (os.R_OK, os.X_OK)):
            status, msg = checker.check_obsidian({"vault_path": str(self.vault)})
        self.assertEqual(status, "FAIL")
        self.assertIn("not readable", msg)

    def test_unwritable_vault_without_read_only_flag_fails(self) -> None:
        """The runtime failure this catches: every write refused, no config says so."""
        with mock.patch.object(checker.os, "access", lambda p, m: m != os.W_OK):
            status, msg = checker.check_obsidian({"vault_path": str(self.vault)})
        self.assertEqual(status, "FAIL")
        self.assertIn("read_only is not set", msg)

    def test_unwritable_vault_with_read_only_flag_passes(self) -> None:
        with mock.patch.object(checker.os, "access", lambda p, m: m != os.W_OK):
            status, msg = checker.check_obsidian(
                {"vault_path": str(self.vault), "read_only": True}
            )
        self.assertEqual(status, "PASS")
        self.assertIn("read-only as declared", msg)

    def test_writable_vault_declared_read_only_passes_but_says_so(self) -> None:
        # Nothing is broken, but the flag disagrees with the disk - say it out
        # loud rather than let a stale read_only flag look intentional.
        status, msg = checker.check_obsidian(
            {"vault_path": str(self.vault), "read_only": True}
        )
        self.assertEqual(status, "PASS")
        self.assertIn("despite read_only: true", msg)

    def test_vault_path_expands_user_and_env(self) -> None:
        with mock.patch.dict(os.environ, {"MY_VAULT": str(self.vault)}):
            status, _ = checker.check_obsidian({"vault_path": "${MY_VAULT}"})
        self.assertEqual(status, "PASS")

    def test_real_chmod_zero_directory_is_caught(self) -> None:
        """Belt and braces: the mocked os.access tests above, done for real.

        Skipped for root, which bypasses permission bits entirely.
        """
        if os.geteuid() == 0:
            self.skipTest("running as root - permission bits do not apply")
        locked = Path(self._tmp.name) / "locked"
        locked.mkdir()
        os.chmod(locked, 0o000)
        self.addCleanup(os.chmod, locked, stat.S_IRWXU)
        status, msg = checker.check_obsidian({"vault_path": str(locked)})
        self.assertEqual(status, "FAIL")
        self.assertIn("not readable", msg)


class SiyuanCheckTest(unittest.TestCase):
    def test_no_token_skips(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            status, msg = checker.check_siyuan({})
        self.assertEqual(status, "SKIP")
        self.assertIn("SIYUAN_TOKEN not set", msg)

    def test_reachable_kernel_passes(self) -> None:
        with mock.patch.object(checker, "urllib") as urllib_mod:
            resp = mock.MagicMock()
            resp.read.return_value = b'{"code":0,"msg":"","data":"3.1.10"}'
            urllib_mod.request.urlopen.return_value.__enter__.return_value = resp
            status, msg = checker.check_siyuan({"token": "t", "base_url": "http://k:6806"})
        self.assertEqual(status, "PASS")
        self.assertIn("3.1.10", msg)

    def test_bad_token_fails_rather_than_passing_on_a_200(self) -> None:
        """SiYuan answers a wrong token with HTTP 200 and code -1.

        A TCP-connect or status-code check would call this healthy. It is not:
        every real call comes back "Auth failed [session]".
        """
        with mock.patch.object(checker, "urllib") as urllib_mod:
            resp = mock.MagicMock()
            resp.read.return_value = b'{"code":-1,"msg":"Auth failed [session]"}'
            urllib_mod.request.urlopen.return_value.__enter__.return_value = resp
            status, msg = checker.check_siyuan({"token": "wrong"})
        self.assertEqual(status, "FAIL")
        self.assertIn("Auth failed", msg)

    def test_unreachable_kernel_fails(self) -> None:
        with mock.patch.object(checker.urllib.request, "urlopen",
                               side_effect=OSError("connection refused")):
            status, msg = checker.check_siyuan({"token": "t"})
        self.assertEqual(status, "FAIL")
        self.assertIn("unreachable", msg)

    def test_token_falls_back_to_env(self) -> None:
        with mock.patch.dict(os.environ, {"SIYUAN_TOKEN": "from-env"}, clear=True), \
             mock.patch.object(checker.urllib.request, "urlopen",
                               side_effect=OSError("refused")):
            status, _ = checker.check_siyuan({})
        self.assertEqual(status, "FAIL")  # reached the call, so the token resolved


class NotionCheckTest(unittest.TestCase):
    def test_no_token_skips(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            status, msg = checker.check_notion({})
        self.assertEqual(status, "SKIP")
        self.assertIn("NOTION_API_TOKEN not set", msg)

    def test_reachable_api_passes(self) -> None:
        with mock.patch.object(checker, "urllib") as urllib_mod:
            resp = mock.MagicMock()
            resp.read.return_value = b'{"id":"bot-123","type":"bot"}'
            urllib_mod.request.urlopen.return_value.__enter__.return_value = resp
            status, msg = checker.check_notion({"token": "secret_x"})
        self.assertEqual(status, "PASS")
        self.assertIn("bot-123", msg)

    def test_failed_call_fails(self) -> None:
        with mock.patch.object(checker.urllib.request, "urlopen",
                               side_effect=OSError("401")):
            status, msg = checker.check_notion({"token": "bad"})
        self.assertEqual(status, "FAIL")


class DispatchTest(unittest.TestCase):
    def _run_main(self, sb_config: dict) -> tuple[str, str]:
        with mock.patch.object(checker, "load_second_brain_config", return_value=sb_config), \
             mock.patch("builtins.print") as printer:
            checker.main()
        line = printer.call_args[0][0]
        status, _, msg = line.partition("|")
        return status, msg

    def test_each_backend_routes_to_its_own_check(self) -> None:
        for backend in ("siyuan", "notion", "obsidian"):
            with self.subTest(backend=backend), \
                 mock.patch.dict(checker.CHECKS,
                                 {backend: lambda _c: ("PASS", f"ran {backend}")}):
                status, msg = self._run_main({"backend": backend})
            self.assertEqual(status, "PASS")
            self.assertEqual(msg, f"ran {backend}")

    def test_missing_backend_skips(self) -> None:
        status, msg = self._run_main({})
        self.assertEqual(status, "SKIP")
        self.assertIn("second_brain.backend not set", msg)

    def test_unknown_backend_skips_rather_than_failing(self) -> None:
        # A chassis install is allowed to run without a second brain.
        status, msg = self._run_main({"backend": "roam"})
        self.assertEqual(status, "SKIP")
        self.assertIn("roam", msg)

    def test_output_is_one_parseable_line(self) -> None:
        with mock.patch.object(checker, "load_second_brain_config", return_value={}), \
             mock.patch("builtins.print") as printer:
            rc = checker.main()
        self.assertEqual(rc, 0)  # always exit 0 - the caller grades the status
        printer.assert_called_once()
        self.assertEqual(printer.call_args[0][0].count("|"), 1)

    def test_unreadable_config_skips(self) -> None:
        with mock.patch("chassis.second_brain.factory._load_config",
                        side_effect=FileNotFoundError("gone")):
            self.assertEqual(checker.load_second_brain_config(), {})


class SmokeTestWiringTest(unittest.TestCase):
    """The check name has to match what chassis.config.yaml grades on."""

    def test_smoke_test_records_second_brain_read_not_notion_read(self) -> None:
        smoke = (REPO_ROOT / "chassis" / "scripts" / "smoke-test.sh").read_text()
        self.assertIn("record SKIP second_brain_read", smoke)
        # Match `record` CALLS, not raw substrings - the comment above the check
        # names the old `notion_read` on purpose, to explain what changed.
        recorded = {
            line.split()[2]
            for line in smoke.splitlines()
            if line.strip().startswith("record ") and len(line.split()) > 2
        }
        self.assertIn("second_brain_read", recorded)
        self.assertNotIn("notion_read", recorded)

    def test_config_success_criterion_names_a_check_that_is_recorded(self) -> None:
        config = (REPO_ROOT / "chassis.config.yaml").read_text()
        self.assertIn("second_brain_read", config)
        smoke = (REPO_ROOT / "chassis" / "scripts" / "smoke-test.sh").read_text()
        self.assertIn("second_brain_read", smoke)


if __name__ == "__main__":
    unittest.main()
