#!/usr/bin/env python3
"""test_factory_credentials.py - get_adapter() resolves REAL credentials, or fails loudly.

The gap this closes
===================
Adapter mode and direct mode used to read SiYuan credentials from two disjoint
sources. Direct mode got `SIYUAN_URL` / `SIYUAN_TOKEN` from `.env` via the
`siyuan` entry in .mcp.json. Adapter mode read `second_brain.siyuan.{base_url,
token,notebook_id}` from chassis.config.yaml - a sub-block that existed in NO
shipped template and in NO real install. `get_adapter()` therefore built a
SiYuanAdapter with token='' and notebook_id='', and a live kernel answers that
with `{"code":-1,"msg":"Auth failed [session]"}`. Flipping `mode: adapter` made
the second brain go dark.

The pre-existing suite missed it because test_mcp_json_render.py only exercises
the hydrator (never the factory) and the mcp_server e2e test only runs Obsidian
against a hand-written fixture config. This test drives the factory against the
REAL shipped chassis.config.yaml, which is where the missing sub-block lived.

Run:
    python3 -m pytest chassis/second_brain/tests/test_factory_credentials.py -v
"""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from chassis.second_brain.factory import _load_config, get_adapter  # noqa: E402

SHIPPED_CONFIG = REPO_ROOT / "chassis.config.yaml"

FAKE_URL = "http://host.docker.internal:6806"
FAKE_TOKEN = "test-token-not-a-real-secret"
FAKE_ENV = {"SIYUAN_URL": FAKE_URL, "SIYUAN_TOKEN": FAKE_TOKEN}
# Fake host. The real deeplink host is customer-specific, it moves, and it never
# belongs in this repo - that is the whole reason it is a parameter.
FAKE_DEEPLINK_BASE = "https://siyuan.invalid:6806/stage/build/desktop/?id="


def _shipped_as_siyuan(raw: str, siyuan_block: str = "") -> str:
    """The REAL shipped config text, backend flipped to siyuan.

    Text substitution rather than a YAML round-trip so the file stays
    byte-identical to what installers get, comments included. The
    substitution-count assertion means a restructured template fails this test
    loudly instead of silently testing a fixture that no longer resembles the
    shipped one.
    """
    patched, count = re.subn(
        r"^(\s*)backend:\s*\w+.*$",
        lambda m: f"{m.group(1)}backend: siyuan" + siyuan_block,
        raw,
        count=1,
        flags=re.MULTILINE,
    )
    assert count == 1, "expected exactly one second_brain.backend key in chassis.config.yaml"
    return patched


def _shipped_as_notion(raw: str, notion_block: str = "") -> str:
    """The REAL shipped config text, backend pinned to notion.

    Same substitution contract as _shipped_as_siyuan. The shipped config already
    ships `backend: notion`, but pinning it explicitly means these tests keep
    testing Notion if the shipped default ever changes.
    """
    patched, count = re.subn(
        r"^(\s*)backend:\s*\w+.*$",
        lambda m: f"{m.group(1)}backend: notion" + notion_block,
        raw,
        count=1,
        flags=re.MULTILINE,
    )
    assert count == 1, "expected exactly one second_brain.backend key in chassis.config.yaml"
    return patched


class SiYuanCredentialResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sb-factory-creds-")
        self.tmp_dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.shipped_raw = SHIPPED_CONFIG.read_text(encoding="utf-8")
        self.config_path = self._write("shipped-siyuan.yaml", _shipped_as_siyuan(self.shipped_raw))

    def _write(self, name: str, text: str) -> Path:
        path = self.tmp_dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_env_credentials_reach_the_adapter(self) -> None:
        # The shipped config carries no second_brain.siyuan block, so .env is
        # the only credential source an install actually has. Adapter mode must
        # read it. This is the bug: it used to yield token=''.
        with mock.patch.dict("os.environ", FAKE_ENV):
            adapter = get_adapter(self.config_path)
        notes = adapter.notes
        self.assertEqual(adapter.backend, "siyuan")
        self.assertTrue(notes._token, "token must not be empty - the live kernel rejects it")
        self.assertEqual(notes._token, FAKE_TOKEN)
        self.assertEqual(notes._base_url, FAKE_URL)

    def test_notebook_id_falls_back_to_databases_notes_root(self) -> None:
        # notes_root is the canonical "page id for free-form prose writes" key
        # that every install already fills in. No installer should have to
        # duplicate it under second_brain.siyuan.notebook_id.
        expected = _load_config(self.config_path)["second_brain"]["databases"]["notes_root"]
        self.assertTrue(expected, "shipped config lost second_brain.databases.notes_root")
        with mock.patch.dict("os.environ", FAKE_ENV):
            adapter = get_adapter(self.config_path)
        self.assertTrue(adapter.notes._notebook_id, "notebook_id must not be empty")
        self.assertEqual(adapter.notes._notebook_id, expected)

    def test_missing_token_raises_instead_of_building_a_dead_adapter(self) -> None:
        # No token in config, no token in env. The old code silently returned an
        # adapter that fails auth on every call. It must fail at construction.
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                get_adapter(self.config_path)
        message = str(ctx.exception)
        self.assertIn("SIYUAN_TOKEN", message)
        self.assertIn("second_brain.siyuan.token", message)

    def test_missing_notebook_id_raises(self) -> None:
        # Same contract for the write target: an empty notebook_id sends
        # createDocWithMd into the void.
        blanked = self.shipped_raw.replace("notes_root: TBD_at_install", "notes_root:")
        self.assertNotIn("notes_root: TBD_at_install", blanked)
        path = self._write("no-notes-root.yaml", _shipped_as_siyuan(blanked))
        with mock.patch.dict("os.environ", FAKE_ENV):
            with self.assertRaises(ValueError) as ctx:
                get_adapter(path)
        self.assertIn("notebook_id", str(ctx.exception))

    def test_yaml_block_overrides_env(self) -> None:
        # The YAML keys stay available as an explicit override for installs that
        # want a non-default kernel, notebook, or deeplink host.
        override = (
            "\n  siyuan:"
            "\n    base_url: http://kernel.internal:6806"
            "\n    token: yaml-wins"
            "\n    notebook_id: 20231101120000-abc123"
            "\n    deeplink_template: https://s.example.com/?id="
        )
        path = self._write(
            "siyuan-override.yaml", _shipped_as_siyuan(self.shipped_raw, override)
        )
        with mock.patch.dict("os.environ", FAKE_ENV):
            adapter = get_adapter(path)
        notes = adapter.notes
        self.assertEqual(notes._token, "yaml-wins")
        self.assertEqual(notes._base_url, "http://kernel.internal:6806")
        self.assertEqual(notes._notebook_id, "20231101120000-abc123")
        self.assertEqual(notes._deeplink_template, "https://s.example.com/?id=")

    def test_deeplink_falls_back_to_the_desktop_uri_default(self) -> None:
        # Neither YAML nor env: the chassis default. Note this URI does NOT open
        # on a phone - that is why the env var below exists.
        with mock.patch.dict("os.environ", FAKE_ENV, clear=True):
            adapter = get_adapter(self.config_path)
        self.assertEqual(adapter.notes._deeplink_template, "siyuan://blocks/")
        self.assertEqual(
            adapter.notes.get_deeplink("20231101120000-abc123"),
            "siyuan://blocks/20231101120000-abc123",
        )

    def test_deeplink_comes_from_env_when_yaml_is_absent(self) -> None:
        # The real-use path: an install sets SIYUAN_DEEPLINK_BASE in .env to its
        # web-UI prefix so links are clickable on a phone. Fake host on purpose -
        # the real one is customer-specific and never lives in this repo.
        env = dict(FAKE_ENV, SIYUAN_DEEPLINK_BASE=FAKE_DEEPLINK_BASE)
        with mock.patch.dict("os.environ", env, clear=True):
            adapter = get_adapter(self.config_path)
        self.assertEqual(adapter.notes._deeplink_template, FAKE_DEEPLINK_BASE)
        # The id is appended verbatim, so the prefix keeps its trailing separator.
        self.assertEqual(
            adapter.notes.get_deeplink("20231101120000-abc123"),
            FAKE_DEEPLINK_BASE + "20231101120000-abc123",
        )

    def test_deeplink_yaml_overrides_env(self) -> None:
        override = "\n  siyuan:\n    deeplink_template: https://yaml.invalid/?id="
        path = self._write(
            "siyuan-deeplink.yaml", _shipped_as_siyuan(self.shipped_raw, override)
        )
        env = dict(FAKE_ENV, SIYUAN_DEEPLINK_BASE=FAKE_DEEPLINK_BASE)
        with mock.patch.dict("os.environ", env, clear=True):
            adapter = get_adapter(path)
        self.assertEqual(adapter.notes._deeplink_template, "https://yaml.invalid/?id=")

    def test_env_ref_in_yaml_that_expands_to_nothing_falls_through(self) -> None:
        # A config shipping `token: ${SIYUAN_TOKEN}` must not shadow the env
        # fallback when the var is unset - it resolves to '' and has to fall
        # through to the same ValueError, not build a dead adapter.
        override = "\n  siyuan:\n    token: ${SIYUAN_TOKEN}"
        path = self._write(
            "siyuan-envref.yaml", _shipped_as_siyuan(self.shipped_raw, override)
        )
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                get_adapter(path)
        self.assertIn("SIYUAN_TOKEN", str(ctx.exception))


class NotionCredentialResolutionTest(unittest.TestCase):
    """Notion's half of the same contract SiYuanCredentialResolutionTest covers.

    Notion had two gaps SiYuan did not:

    1. Two env var names for one secret. The factory read NOTION_API_TOKEN and
       .env.example documented it, while .mcp.json.template, hydrate-env-from-vw.sh
       and smoke-test.sh used NOTION_INTEGRATION_TOKEN. The Vaultwarden path
       worked by accident; an installer who followed .env.example got the literal
       string "<NOTION_INTEGRATION_TOKEN>" shipped as their bearer token.
    2. No guards. get_adapter() built a NotionAdapter unconditionally, so an
       empty token or empty notes_root produced a happily-constructed adapter
       that 401'd on every call. SiYuan only got its guards after that exact
       failure burned an install (928c657).
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sb-factory-notion-")
        self.tmp_dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.shipped_raw = SHIPPED_CONFIG.read_text(encoding="utf-8")
        self.config_path = self._write("shipped-notion.yaml", _shipped_as_notion(self.shipped_raw))

    def _write(self, name: str, text: str) -> Path:
        path = self.tmp_dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_env_token_reaches_the_adapter(self) -> None:
        # The shipped config carries no second_brain.notion block, so .env is the
        # only credential source an install actually has.
        with mock.patch.dict("os.environ", {"NOTION_API_TOKEN": FAKE_TOKEN}, clear=True):
            adapter = get_adapter(self.config_path)
        self.assertEqual(adapter.backend, "notion")
        self.assertEqual(adapter.notes._token, FAKE_TOKEN)
        # The structured surface shares the one credential - no second copy.
        self.assertEqual(adapter.database._token, FAKE_TOKEN)

    def test_the_only_token_var_is_notion_api_token(self) -> None:
        # The rename's regression guard. NOTION_INTEGRATION_TOKEN is dead: setting
        # it alone must NOT satisfy the factory, or the two-name split is back and
        # installers get a silent 401 depending on which doc they followed.
        with mock.patch.dict(
            "os.environ", {"NOTION_INTEGRATION_TOKEN": FAKE_TOKEN}, clear=True
        ):
            with self.assertRaises(ValueError) as ctx:
                get_adapter(self.config_path)
        self.assertIn("NOTION_API_TOKEN", str(ctx.exception))

    def test_notes_root_falls_back_to_databases_notes_root(self) -> None:
        expected = _load_config(self.config_path)["second_brain"]["databases"]["notes_root"]
        self.assertTrue(expected, "shipped config lost second_brain.databases.notes_root")
        with mock.patch.dict("os.environ", {"NOTION_API_TOKEN": FAKE_TOKEN}, clear=True):
            adapter = get_adapter(self.config_path)
        self.assertEqual(adapter.notes._notes_root, expected)

    def test_missing_token_raises_instead_of_building_a_dead_adapter(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                get_adapter(self.config_path)
        message = str(ctx.exception)
        self.assertIn("NOTION_API_TOKEN", message)
        self.assertIn("second_brain.notion.token", message)

    def test_missing_notes_root_raises(self) -> None:
        blanked = self.shipped_raw.replace("notes_root: TBD_at_install", "notes_root:")
        self.assertNotIn("notes_root: TBD_at_install", blanked)
        path = self._write("no-notes-root.yaml", _shipped_as_notion(blanked))
        with mock.patch.dict("os.environ", {"NOTION_API_TOKEN": FAKE_TOKEN}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                get_adapter(path)
        self.assertIn("notes_root", str(ctx.exception))

    def test_yaml_block_overrides_env(self) -> None:
        override = (
            "\n  notion:"
            "\n    token: yaml-wins"
            "\n    notes_root: 1234abcd-5678-90ef-1234-567890abcdef"
            "\n    active_database: lp_crm"
            "\n    databases:"
            "\n      lp_crm: aaaa1111-bbbb-2222-cccc-333333333333"
            "\n    natural_keys:"
            "\n      lp_crm: email"
        )
        path = self._write("notion-override.yaml", _shipped_as_notion(self.shipped_raw, override))
        with mock.patch.dict("os.environ", {"NOTION_API_TOKEN": FAKE_TOKEN}, clear=True):
            adapter = get_adapter(path)
        self.assertEqual(adapter.notes._token, "yaml-wins")
        self.assertEqual(adapter.notes._notes_root, "1234abcd-5678-90ef-1234-567890abcdef")
        self.assertEqual(adapter.database._active, "lp_crm")
        self.assertEqual(
            adapter.database._databases, {"lp_crm": "aaaa1111-bbbb-2222-cccc-333333333333"}
        )
        self.assertEqual(adapter.database._natural_keys, {"lp_crm": "email"})

    def test_env_ref_in_yaml_that_expands_to_nothing_falls_through(self) -> None:
        # `token: ${NOTION_API_TOKEN}` is exactly what docs/second-brain-adapters.md
        # shows. With the var unset it resolves to '' and must reach the same
        # ValueError rather than shadowing the env fallback with an empty string.
        override = "\n  notion:\n    token: ${NOTION_API_TOKEN}"
        path = self._write("notion-envref.yaml", _shipped_as_notion(self.shipped_raw, override))
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                get_adapter(path)
        self.assertIn("NOTION_API_TOKEN", str(ctx.exception))

    def test_env_ref_in_yaml_expands_from_env(self) -> None:
        override = "\n  notion:\n    token: ${NOTION_API_TOKEN}"
        path = self._write("notion-envref-set.yaml", _shipped_as_notion(self.shipped_raw, override))
        with mock.patch.dict("os.environ", {"NOTION_API_TOKEN": FAKE_TOKEN}, clear=True):
            adapter = get_adapter(path)
        self.assertEqual(adapter.notes._token, FAKE_TOKEN)

    def test_unresolved_placeholder_is_not_accepted_as_a_token(self) -> None:
        # The concrete pre-existing failure: hydrate-mcp-json.py left
        # "<NOTION_INTEGRATION_TOKEN>" in .mcp.json when the var was unset, the
        # MCP server got that literal string in its environment, and it went out
        # as `Authorization: Bearer <NOTION_INTEGRATION_TOKEN>`. The hydrator now
        # drops such keys, but the factory must not accept the shape either.
        for placeholder in ("<NOTION_API_TOKEN>", "<NOTION_INTEGRATION_TOKEN>"):
            with self.subTest(placeholder=placeholder):
                with mock.patch.dict(
                    "os.environ", {"NOTION_API_TOKEN": placeholder}, clear=True
                ):
                    with self.assertRaises(ValueError) as ctx:
                        get_adapter(self.config_path)
                self.assertIn("NOTION_API_TOKEN", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
