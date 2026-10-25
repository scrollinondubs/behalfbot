#!/usr/bin/env python3
"""test_daily_log_surfaces.py - per-surface status in the daily-log gather.

The bug this locks down (new-jaxity#307)
========================================
The gather returned `warnings: []` and nothing else about surface health. A
flat list of strings with no surface attribution cannot answer the one
question the daily-log prompt has to ask about an empty bucket: did nothing
happen, or could we not look? So the prompt collapsed both into "quiet day".
On a day 8 PRs merged across three repos, the GitHub query failed,
`prs_by_repo` came back `{}`, and the log reported nothing shipped.

The distinction cannot live in the buckets. A failed GitHub scan and a
genuinely quiet day produce byte-identical `prs_by_repo` values. It has to
live in a status field, which is what `surfaces` is.

The property under test throughout: a surface that was NOT successfully read
must never report `ok`. `skipped` and `error` are both acceptable answers for
an unread surface - conflating those two is a cosmetic bug, whereas reporting
either as `ok` is the incident.

Run:
    python3 -m pytest chassis/scripts/tests/test_daily_log_surfaces.py -v
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

GATHER_PATH = REPO_ROOT / "chassis" / "scripts" / "daily-log-gather.py"
_spec = importlib.util.spec_from_file_location("daily_log_gather_surfaces", GATHER_PATH)
gather = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gather)

UNTIL = datetime(2026, 7, 19, tzinfo=timezone.utc)

SURFACES = ("github", "gmail", "second_brain", "discord")

# Every surface configured, so build_output attempts all four and the status
# is decided by what the gather functions return rather than by a missing var.
FULL_ENV = {
    "CHASSIS_HOME": "/nonexistent-chassis-home",
    "DAILY_LOG_GH_USER": "testuser",
    "DAILY_LOG_GMAIL_IDENTITY": "jax@example.com",
    "DAILY_LOG_SIYUAN_URL": "http://127.0.0.1:1",
    "DAILY_LOG_DISCORD_CHANNEL_ID": "1234567890123456789",
    "DISCORD_TOKEN": "test-token",
}


def build(env: dict, **patches):
    """Run build_output with a controlled env and stubbed surface gathers.

    Defaults every gather to a clean success so each test can make exactly one
    of them fail and attribute the resulting status unambiguously.
    """
    defaults = {
        "gather_github": ({}, [], []),
        "gather_gmail": ([], False, []),
        "gather_siyuan": ([], []),
        "gather_discord_postmortems": ([], []),
    }
    defaults.update(patches)
    with mock.patch.dict("os.environ", env, clear=True), \
         mock.patch.object(gather, "resolve_second_brain_backend", return_value="siyuan"), \
         mock.patch.object(gather, "gather_metrics", return_value={}), \
         mock.patch.object(gather, "gather_github", return_value=defaults["gather_github"]), \
         mock.patch.object(gather, "gather_gmail", return_value=defaults["gather_gmail"]), \
         mock.patch.object(gather, "gather_siyuan", return_value=defaults["gather_siyuan"]), \
         mock.patch.object(gather, "gather_discord_postmortems",
                           return_value=defaults["gather_discord_postmortems"]):
        return gather.build_output(now=UNTIL, verbose=False)


class SurfacesBlockShapeTest(unittest.TestCase):
    def test_every_surface_is_present(self) -> None:
        surfaces = build(FULL_ENV)["surfaces"]
        self.assertEqual(set(surfaces), set(SURFACES))

    def test_every_surface_has_status_and_error_keys(self) -> None:
        surfaces = build(FULL_ENV)["surfaces"]
        for name in SURFACES:
            with self.subTest(surface=name):
                self.assertIn("status", surfaces[name])
                self.assertIn("error", surfaces[name])

    def test_status_is_always_one_of_the_three_values(self) -> None:
        surfaces = build({"CHASSIS_HOME": "/nonexistent-chassis-home"})["surfaces"]
        for name, surface in surfaces.items():
            with self.subTest(surface=name):
                self.assertIn(surface["status"], ("ok", "error", "skipped"))

    def test_warnings_is_retained_alongside_surfaces(self) -> None:
        """Bootstrap copies the prompt to disk once. Installs still read this."""
        payload = build({"CHASSIS_HOME": "/nonexistent-chassis-home"})
        self.assertIn("warnings", payload)
        self.assertTrue(payload["warnings"])


class UnconfiguredSurfacesAreSkippedTest(unittest.TestCase):
    """No credentials means nothing was attempted, which is not `ok`."""

    def test_bare_env_skips_every_surface(self) -> None:
        surfaces = build({"CHASSIS_HOME": "/nonexistent-chassis-home"})["surfaces"]
        for name in SURFACES:
            with self.subTest(surface=name):
                self.assertEqual(surfaces[name]["status"], "skipped")

    def test_skip_reason_is_reported(self) -> None:
        surfaces = build({"CHASSIS_HOME": "/nonexistent-chassis-home"})["surfaces"]
        for name in SURFACES:
            with self.subTest(surface=name):
                self.assertTrue(surfaces[name]["error"],
                                f"{name} skipped without saying why")

    def test_discord_channel_set_but_token_missing_is_skipped(self) -> None:
        env = dict(FULL_ENV)
        del env["DISCORD_TOKEN"]
        surfaces = build(env)["surfaces"]
        self.assertEqual(surfaces["discord"]["status"], "skipped")
        self.assertIn("DISCORD_TOKEN", surfaces["discord"]["error"])


class AttemptedAndFailedIsErrorTest(unittest.TestCase):
    """The #307 shape: configured, attempted, failed, empty bucket."""

    def test_failed_github_scan_reports_error_not_ok(self) -> None:
        payload = build(
            FULL_ENV,
            gather_github=({}, [], ["gh graphql viewer query failed - skipped GitHub scan"]),
        )
        github = payload["surfaces"]["github"]
        self.assertEqual(github["status"], "error")
        self.assertIn("graphql", github["error"])

    def test_failed_github_scan_bucket_is_indistinguishable_from_quiet_day(self) -> None:
        """The reason status exists: the buckets alone cannot carry this.

        A failed scan and a genuinely quiet day are byte-identical in
        prs_by_repo. If this assertion ever fails, the bucket started carrying
        the signal and the status field could in principle be retired - but
        until then, status is the only place the distinction lives.
        """
        failed = build(
            FULL_ENV,
            gather_github=({}, [], ["gh graphql viewer query failed"]),
        )
        quiet = build(FULL_ENV, gather_github=({}, [], []))
        self.assertEqual(failed["prs_by_repo"], quiet["prs_by_repo"])
        self.assertNotEqual(
            failed["surfaces"]["github"]["status"],
            quiet["surfaces"]["github"]["status"],
        )

    def test_failed_second_brain_scan_reports_error(self) -> None:
        payload = build(
            FULL_ENV,
            gather_siyuan=([], ["siyuan SQL query failed - skipped SiYuan scan"]),
        )
        self.assertEqual(payload["surfaces"]["second_brain"]["status"], "error")

    def test_failed_discord_scan_reports_error(self) -> None:
        payload = build(
            FULL_ENV,
            gather_discord_postmortems=([], ["discord fetch failed: HTTP 401"]),
        )
        discord = payload["surfaces"]["discord"]
        self.assertEqual(discord["status"], "error")
        self.assertIn("401", discord["error"])

    def test_multiple_warnings_are_all_reported(self) -> None:
        payload = build(
            FULL_ENV,
            gather_github=({}, [], ["first failure", "second failure"]),
        )
        detail = payload["surfaces"]["github"]["error"]
        self.assertIn("first failure", detail)
        self.assertIn("second failure", detail)

    def test_one_failed_surface_does_not_demote_the_others(self) -> None:
        payload = build(FULL_ENV, gather_github=({}, [], ["boom"]))
        self.assertEqual(payload["surfaces"]["github"]["status"], "error")
        self.assertEqual(payload["surfaces"]["second_brain"]["status"], "ok")
        self.assertEqual(payload["surfaces"]["discord"]["status"], "ok")


class SuccessfulSurfacesAreOkTest(unittest.TestCase):
    """`ok` has to remain reachable, or the prompt can never say "quiet day"."""

    def test_clean_scan_reports_ok_with_null_error(self) -> None:
        surfaces = build(FULL_ENV)["surfaces"]
        for name in ("github", "second_brain", "discord"):
            with self.subTest(surface=name):
                self.assertEqual(surfaces[name]["status"], "ok")
                self.assertIsNone(surfaces[name]["error"])

    def test_empty_bucket_under_ok_is_a_real_zero(self) -> None:
        payload = build(FULL_ENV)
        self.assertEqual(payload["prs_by_repo"], {})
        self.assertEqual(payload["surfaces"]["github"]["status"], "ok")


class GmailDeferralTest(unittest.TestCase):
    """Deferral is not a result - this script never opened the mailbox."""

    def test_deferred_gmail_is_not_ok(self) -> None:
        payload = build(FULL_ENV, gather_gmail=([], True, []))
        self.assertTrue(payload["gmail_scan_deferred"])
        self.assertEqual(payload["surfaces"]["gmail"]["status"], "skipped")

    def test_non_deferred_clean_gmail_is_ok(self) -> None:
        payload = build(FULL_ENV, gather_gmail=([], False, []))
        self.assertEqual(payload["surfaces"]["gmail"]["status"], "ok")


class CrashPathTest(unittest.TestCase):
    """A crashed gather read nothing, so it may not look like a quiet day."""

    def test_crash_marks_every_surface_error(self) -> None:
        argv = sys.argv
        sys.argv = ["daily-log-gather.py"]
        try:
            with mock.patch.object(gather, "build_output",
                                   side_effect=RuntimeError("kaboom")), \
                 mock.patch("builtins.print") as printed:
                rc = gather.main()
        finally:
            sys.argv = argv
        self.assertEqual(rc, 0)
        import json
        payload = json.loads(printed.call_args[0][0])
        for name in SURFACES:
            with self.subTest(surface=name):
                self.assertEqual(payload["surfaces"][name]["status"], "error")
                self.assertIn("kaboom", payload["surfaces"][name]["error"])


if __name__ == "__main__":
    unittest.main()
