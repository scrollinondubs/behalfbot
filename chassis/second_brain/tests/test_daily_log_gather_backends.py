#!/usr/bin/env python3
"""test_daily_log_gather_backends.py - daily-log-gather.py routes per backend.

The bug this locks down: daily-log-gather.py talked to SiYuan and only SiYuan,
so on an Obsidian or Notion install the second-brain section of the daily log
was silently empty. It now dispatches on `second_brain.backend`.

Two properties matter and they pull in opposite directions, so both are tested
explicitly rather than one being assumed from the other:

  1. SiYuan installs must be UNCHANGED. The legacy direct-HTTP `gather_siyuan`
     still runs, and `gather_via_adapter` is never called. Every live install
     runs SiYuan; a "refactor" that quietly moved them onto the adapter's
     slightly different query would be a regression dressed as a cleanup.
  2. Obsidian and Notion installs must actually reach their backend, through
     `get_adapter().notes.list_recent()`.

Plus the failure modes, because a gather script that raises takes the whole
heartbeat down: an unresolvable config falls back to SiYuan (what every install
predating the adapter runs), and an adapter that cannot be built or whose call
raises degrades to a warning.

Run:
    python3 -m pytest chassis/second_brain/tests/test_daily_log_gather_backends.py -v
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

from chassis.second_brain.base import SearchHit  # noqa: E402

GATHER_PATH = REPO_ROOT / "chassis" / "scripts" / "daily-log-gather.py"
_spec = importlib.util.spec_from_file_location("daily_log_gather", GATHER_PATH)
gather = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gather)

SINCE = datetime(2026, 7, 18, tzinfo=timezone.utc)
UNTIL = datetime(2026, 7, 19, tzinfo=timezone.utc)


def _config(backend: str | None) -> dict:
    return {"second_brain": {"backend": backend}} if backend else {}


class ResolveBackendTest(unittest.TestCase):
    def test_reads_backend_from_config(self) -> None:
        for backend in ("siyuan", "notion", "obsidian"):
            with self.subTest(backend=backend), \
                 mock.patch("chassis.second_brain.factory._load_config",
                            return_value=_config(backend)):
                self.assertEqual(gather.resolve_second_brain_backend(), backend)

    def test_backend_is_normalized(self) -> None:
        with mock.patch("chassis.second_brain.factory._load_config",
                        return_value={"second_brain": {"backend": "  Obsidian "}}):
            self.assertEqual(gather.resolve_second_brain_backend(), "obsidian")

    def test_missing_config_falls_back_to_siyuan(self) -> None:
        # The compatibility guarantee: no config must not mean "no scan".
        with mock.patch("chassis.second_brain.factory._load_config",
                        side_effect=FileNotFoundError("no chassis.config.yaml")):
            self.assertEqual(gather.resolve_second_brain_backend(), "siyuan")

    def test_malformed_config_falls_back_to_siyuan(self) -> None:
        with mock.patch("chassis.second_brain.factory._load_config",
                        side_effect=ValueError("garbage")):
            self.assertEqual(gather.resolve_second_brain_backend(), "siyuan")

    def test_config_without_second_brain_block_falls_back_to_siyuan(self) -> None:
        with mock.patch("chassis.second_brain.factory._load_config", return_value={}):
            self.assertEqual(gather.resolve_second_brain_backend(), "siyuan")


class HitMappingTest(unittest.TestCase):
    """Every backend fills the same five keys the SiYuan path publishes."""

    def test_siyuan_shaped_raw(self) -> None:
        hit = SearchHit(
            id="20260718120000-abc1234",
            title="Daily Log",
            snippet="body text",
            deeplink="siyuan://blocks/20260718120000-abc1234",
            raw={"hpath": "/Daily Logs/2026-07-18", "body_len": 412,
                 "updated": "20260718120000"},
        )
        item = gather.hit_to_activity(hit, "siyuan")
        self.assertEqual(item["hpath"], "/Daily Logs/2026-07-18")
        self.assertEqual(item["len"], 412)
        self.assertEqual(item["updated_at"], "20260718120000")
        self.assertEqual(item["backend"], "siyuan")

    def test_obsidian_uses_id_as_path_and_size_as_len(self) -> None:
        hit = SearchHit(
            id="Daily Logs/2026-07-18.md",
            title="2026-07-18",
            snippet="# Daily Log",
            deeplink="obsidian://open?vault=v&file=Daily%20Logs/2026-07-18.md",
            raw={"path": "/vault/Daily Logs/2026-07-18.md", "mtime": 1752800000.0,
                 "size_bytes": 981},
        )
        item = gather.hit_to_activity(hit, "obsidian")
        self.assertEqual(item["hpath"], "Daily Logs/2026-07-18.md")
        self.assertEqual(item["len"], 981)
        self.assertEqual(item["updated_at"], "1752800000.0")

    def test_notion_falls_back_to_title_and_snippet_length(self) -> None:
        # Notion's search API returns neither a path nor a size.
        hit = SearchHit(
            id="1234abcd-5678-90ef-1234-567890abcdef",
            title="Daily Log 2026-07-18",
            snippet="",
            deeplink="https://www.notion.so/1234abcd567890ef1234567890abcdef",
            raw={"last_edited_time": "2026-07-18T12:00:00.000Z"},
        )
        item = gather.hit_to_activity(hit, "notion")
        self.assertEqual(item["hpath"], "Daily Log 2026-07-18")
        self.assertEqual(item["len"], 0)
        self.assertEqual(item["updated_at"], "2026-07-18T12:00:00.000Z")

    def test_unrecognized_raw_shape_still_produces_a_row(self) -> None:
        hit = SearchHit(id="x", title="T", snippet="s", deeplink="d", raw=None)
        item = gather.hit_to_activity(hit, "notion")
        self.assertEqual(item["id"], "x")
        self.assertEqual(item["updated_at"], "")


class AdapterGatherTest(unittest.TestCase):
    def _adapter_returning(self, hits: list[SearchHit]) -> mock.Mock:
        adapter = mock.Mock()
        adapter.notes.list_recent.return_value = hits
        return adapter

    def test_obsidian_hits_are_mapped(self) -> None:
        hit = SearchHit(id="a.md", title="a", snippet="body", deeplink="obsidian://a",
                        raw={"size_bytes": 300, "mtime": 1.0})
        adapter = self._adapter_returning([hit])
        with mock.patch("chassis.second_brain.factory.get_adapter", return_value=adapter):
            activity, warnings = gather.gather_via_adapter(
                "obsidian", SINCE, UNTIL, verbose=False
            )
        self.assertEqual(warnings, [])
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0]["backend"], "obsidian")

    def test_list_recent_gets_naive_local_datetimes(self) -> None:
        """Aware UTC datetimes would shift the window by the host's offset.

        Obsidian compares against st_mtime and SiYuan against a kernel-local
        clock string; both document naive input as local time.
        """
        adapter = self._adapter_returning([])
        with mock.patch("chassis.second_brain.factory.get_adapter", return_value=adapter):
            gather.gather_via_adapter("obsidian", SINCE, UNTIL, verbose=False)
        kwargs = adapter.notes.list_recent.call_args.kwargs
        self.assertIsNone(kwargs["since"].tzinfo)
        self.assertIsNone(kwargs["until"].tzinfo)
        self.assertEqual(kwargs["min_content_len"], gather.DEFAULT_SIYUAN_MIN_CONTENT_LEN)
        self.assertEqual(kwargs["limit"], gather.DEFAULT_SIYUAN_LIMIT)

    def test_unbuildable_adapter_warns_instead_of_raising(self) -> None:
        # get_adapter raises ValueError on an empty/placeholder credential.
        with mock.patch("chassis.second_brain.factory.get_adapter",
                        side_effect=ValueError("Notion adapter has no API token")):
            activity, warnings = gather.gather_via_adapter(
                "notion", SINCE, UNTIL, verbose=False
            )
        self.assertEqual(activity, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("could not be built", warnings[0])

    def test_failing_list_recent_warns_instead_of_raising(self) -> None:
        adapter = mock.Mock()
        adapter.notes.list_recent.side_effect = RuntimeError("vault vanished")
        with mock.patch("chassis.second_brain.factory.get_adapter", return_value=adapter):
            activity, warnings = gather.gather_via_adapter(
                "obsidian", SINCE, UNTIL, verbose=False
            )
        self.assertEqual(activity, [])
        self.assertIn("list_recent failed", warnings[0])


class BuildOutputDispatchTest(unittest.TestCase):
    """The whole point: which code path runs for which backend."""

    def _build(self, backend: str, env: dict[str, str]) -> dict:
        with mock.patch.object(gather, "resolve_second_brain_backend", return_value=backend), \
             mock.patch.object(gather, "gather_github", return_value=({}, [], [])), \
             mock.patch.object(gather, "gather_gmail", return_value=([], False, [])), \
             mock.patch.object(gather, "gather_discord_postmortems", return_value=([], [])), \
             mock.patch.object(gather, "gather_metrics", return_value={}), \
             mock.patch.dict("os.environ", env, clear=True):
            return gather.build_output(now=UNTIL, verbose=False)

    def test_siyuan_uses_the_legacy_direct_http_path(self) -> None:
        legacy_rows = [{"id": "20260718-a", "hpath": "/x", "len": 400,
                        "updated_at": "20260718120000", "excerpt": ""}]
        with mock.patch.object(gather, "gather_siyuan",
                               return_value=(legacy_rows, [])) as legacy, \
             mock.patch.object(gather, "gather_via_adapter") as adapter_path:
            payload = self._build("siyuan", {"SIYUAN_URL": "http://127.0.0.1:6806",
                                             "SIYUAN_TOKEN": "t"})
        legacy.assert_called_once()
        adapter_path.assert_not_called()
        self.assertEqual(payload["second_brain_backend"], "siyuan")
        self.assertEqual(payload["siyuan_activity"], legacy_rows)

    def test_siyuan_without_url_still_warns_exactly_as_before(self) -> None:
        with mock.patch.object(gather, "gather_siyuan") as legacy, \
             mock.patch.object(gather, "gather_via_adapter") as adapter_path:
            payload = self._build("siyuan", {})
        legacy.assert_not_called()
        adapter_path.assert_not_called()
        self.assertIn(
            "DAILY_LOG_SIYUAN_URL / SIYUAN_URL unset - skipped SiYuan scan",
            payload["warnings"],
        )

    def test_obsidian_uses_the_adapter_path(self) -> None:
        rows = [{"id": "a.md", "hpath": "a.md", "len": 300,
                 "updated_at": "1.0", "excerpt": "", "backend": "obsidian"}]
        with mock.patch.object(gather, "gather_siyuan") as legacy, \
             mock.patch.object(gather, "gather_via_adapter",
                               return_value=(rows, [])) as adapter_path:
            payload = self._build("obsidian", {})
        legacy.assert_not_called()
        adapter_path.assert_called_once()
        self.assertEqual(payload["second_brain_backend"], "obsidian")
        self.assertEqual(payload["second_brain_activity"], rows)

    def test_notion_uses_the_adapter_path(self) -> None:
        with mock.patch.object(gather, "gather_siyuan") as legacy, \
             mock.patch.object(gather, "gather_via_adapter",
                               return_value=([], [])) as adapter_path:
            payload = self._build("notion", {})
        legacy.assert_not_called()
        adapter_path.assert_called_once()
        self.assertEqual(payload["second_brain_backend"], "notion")

    def test_siyuan_activity_alias_mirrors_the_canonical_key(self) -> None:
        """Existing installs read `siyuan_activity` from a prompt on disk."""
        rows = [{"id": "a.md", "hpath": "a.md", "len": 300,
                 "updated_at": "1.0", "excerpt": "", "backend": "obsidian"}]
        with mock.patch.object(gather, "gather_via_adapter", return_value=(rows, [])):
            payload = self._build("obsidian", {})
        self.assertEqual(payload["siyuan_activity"], payload["second_brain_activity"])


if __name__ == "__main__":
    unittest.main()
