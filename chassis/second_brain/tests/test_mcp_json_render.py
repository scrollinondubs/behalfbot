#!/usr/bin/env python3
"""test_mcp_json_render.py - .mcp.json renders correctly for every (backend, mode) combination.

Drives the REAL template (chassis/.mcp.json.template) through the REAL
hydrator (chassis/scripts/hydrate-mcp-json.py) with fixture configs, and
asserts which second-brain servers land in the output:

    (siyuan,   direct)  -> siyuan only
    (siyuan,   adapter) -> secondbrain only
    (notion,   direct)  -> notion only
    (notion,   adapter) -> secondbrain only
    (obsidian, direct)  -> NO second-brain server at all. This combination is
                           effectively invalid: there is no Obsidian MCP server
                           in the template because none exists that fits the
                           chassis (the community options need the Obsidian
                           desktop app + Local REST API plugin, which a
                           headless container install does not run). Obsidian
                           installs are adapter-mode-only.
    (obsidian, adapter) -> secondbrain only

Plus the compatibility case that protects existing installs: a config with NO
mode key renders exactly like mode: direct.

Run:
    python3 -m pytest chassis/second_brain/tests/test_mcp_json_render.py -v
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

TEMPLATE_PATH = REPO_ROOT / "chassis" / ".mcp.json.template"
HYDRATOR_PATH = REPO_ROOT / "chassis" / "scripts" / "hydrate-mcp-json.py"

_spec = importlib.util.spec_from_file_location("hydrate_mcp_json", HYDRATOR_PATH)
hydrator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hydrator)

SECOND_BRAIN_SERVERS = {"siyuan", "notion", "secondbrain"}


def _render(backend: str, mode: str | None) -> dict:
    sb: dict = {"backend": backend}
    if mode is not None:
        sb["mode"] = mode
    config = {"second_brain": sb}
    template = hydrator.load_json(str(TEMPLATE_PATH))
    hydrated, _unresolved = hydrator.hydrate(config, template, env={})
    return hydrated["mcpServers"]


class McpJsonRenderMatrixTest(unittest.TestCase):
    def _assert_second_brain_servers(self, servers: dict, expected: set[str]) -> None:
        present = SECOND_BRAIN_SERVERS & set(servers)
        self.assertEqual(present, expected)

    def test_siyuan_direct(self) -> None:
        self._assert_second_brain_servers(_render("siyuan", "direct"), {"siyuan"})

    def test_siyuan_adapter(self) -> None:
        self._assert_second_brain_servers(_render("siyuan", "adapter"), {"secondbrain"})

    def test_notion_direct(self) -> None:
        self._assert_second_brain_servers(_render("notion", "direct"), {"notion"})

    def test_notion_adapter(self) -> None:
        self._assert_second_brain_servers(_render("notion", "adapter"), {"secondbrain"})

    def test_obsidian_direct_has_no_second_brain_server(self) -> None:
        # Invalid-but-tolerated combination: no native Obsidian MCP server
        # exists, so direct mode leaves the install with no second-brain MCP.
        self._assert_second_brain_servers(_render("obsidian", "direct"), set())

    def test_obsidian_adapter(self) -> None:
        self._assert_second_brain_servers(
            _render("obsidian", "adapter"), {"secondbrain"}
        )

    def test_missing_mode_means_direct(self) -> None:
        # Installs that predate the mode key must see zero change.
        self._assert_second_brain_servers(_render("siyuan", None), {"siyuan"})
        self._assert_second_brain_servers(_render("notion", None), {"notion"})
        self._assert_second_brain_servers(_render("obsidian", None), set())

    def test_default_on_servers_survive_in_both_modes(self) -> None:
        for mode in ("direct", "adapter"):
            servers = _render("siyuan", mode)
            for name in ("memory", "playwright", "context7", "github"):
                self.assertIn(name, servers, f"{name} missing in mode={mode}")

    def test_adapter_entry_shape(self) -> None:
        servers = _render("notion", "adapter")
        entry = servers["secondbrain"]
        self.assertEqual(entry["command"], "python3")
        self.assertIn("second_brain/mcp_server.py", entry["args"][0])
        # Meta keys must be stripped from the hydrated output.
        self.assertFalse([k for k in entry if k.startswith("_")])


CALENDAR_WRITE_TOOLS = {"create-event", "update-event", "delete-event", "respond-to-event"}


def _render_config(config: dict) -> dict:
    template = hydrator.load_json(str(TEMPLATE_PATH))
    hydrated, _unresolved = hydrator.hydrate(config, template, env={})
    return hydrated["mcpServers"]


def _calendar_tools(servers: dict) -> set[str]:
    return set(servers["google-calendar"]["env"]["ENABLED_TOOLS"].split(","))


class GoogleMcpRenderTest(unittest.TestCase):
    """Gmail + Calendar MCP servers (issue #57).

    The load-bearing case is the FIRST one: an existing install whose config
    predates modules.google must render byte-identically to before. Everything
    else here is downstream of that.
    """

    def test_config_without_google_keys_renders_no_google_servers(self) -> None:
        for config in (
            {},
            {"second_brain": {"backend": "siyuan"}},
            {"modules": {"admin": True}},  # modules exists, google does not
        ):
            servers = _render_config(config)
            self.assertNotIn("gmail", servers)
            self.assertNotIn("google-calendar", servers)
            self.assertNotIn("google-sheets", servers)

    def test_existing_install_server_set_is_unchanged(self) -> None:
        # The exact server set a pre-#57 config rendered. If adding a Google
        # entry ever leaks into an install that did not ask for it, this fails.
        servers = _render_config({"second_brain": {"backend": "siyuan"}})
        self.assertEqual(
            set(servers), {"memory", "playwright", "context7", "github", "siyuan"}
        )

    def test_gmail_registers_when_flag_set(self) -> None:
        servers = _render_config({"modules": {"google": {"gmail": True}}})
        entry = servers["gmail"]
        self.assertEqual(entry["command"], "npx")
        self.assertIn("@gongrzhe/server-gmail-autoauth-mcp", entry["args"])
        self.assertEqual(
            set(entry["env"]), {"GMAIL_OAUTH_PATH", "GMAIL_CREDENTIALS_PATH"}
        )
        self.assertFalse([k for k in entry if k.startswith("_")])
        # Enabling Gmail must not drag Calendar in with it.
        self.assertNotIn("google-calendar", servers)

    def test_calendar_defaults_to_read_only_when_trust_line_absent(self) -> None:
        # A config that enables Calendar but never mentions trust_line lands on
        # the read floor. `==` on a missing path is False, so the override skips.
        servers = _render_config({"modules": {"google": {"calendar": True}}})
        tools = _calendar_tools(servers)
        self.assertIn("list-events", tools)
        self.assertEqual(tools & CALENDAR_WRITE_TOOLS, set())

    def test_calendar_read_only_trust_line_withholds_write_tools(self) -> None:
        servers = _render_config(
            {
                "modules": {"google": {"calendar": True}},
                "trust_line": {"calendar": "read_only"},
            }
        )
        self.assertEqual(_calendar_tools(servers) & CALENDAR_WRITE_TOOLS, set())

    def test_calendar_read_write_trust_line_grants_write_tools(self) -> None:
        servers = _render_config(
            {
                "modules": {"google": {"calendar": True}},
                "trust_line": {"calendar": "read_write"},
            }
        )
        tools = _calendar_tools(servers)
        self.assertEqual(tools & CALENDAR_WRITE_TOOLS, CALENDAR_WRITE_TOOLS)
        self.assertIn("list-events", tools)  # widened, not swapped

    def test_calendar_entry_shape(self) -> None:
        entry = _render_config({"modules": {"google": {"calendar": True}}})[
            "google-calendar"
        ]
        self.assertIn("@cocal/google-calendar-mcp", entry["args"])
        # _override_when and friends are template-only and must not ship.
        self.assertFalse([k for k in entry if k.startswith("_")])

    def test_shipped_config_defaults_calendar_to_read_only(self) -> None:
        # Guards the chassis.config.yaml default itself: if someone raises
        # trust_line.calendar back to read_write, every install that turns
        # Calendar on silently gets delete-event. Make that a failing test,
        # not a surprise.
        import yaml  # noqa: PLC0415 - test-only dependency

        with open(REPO_ROOT / "chassis.config.yaml") as f:
            shipped = yaml.safe_load(f)
        self.assertEqual(shipped["trust_line"]["calendar"], "read_only")
        self.assertIs(shipped["modules"]["google"]["gmail"], False)
        self.assertIs(shipped["modules"]["google"]["calendar"], False)


class GoogleSheetsMcpRenderTest(unittest.TestCase):
    """Google Sheets MCP server (issue #63).

    Same shape as the Gmail/Calendar tests above, with one difference worth
    stating: `trust_line.sheets` does not gate a TOOL LIST, because
    @shivaduke28/google-sheets-mcp has no tool filter. It gates whether the
    server is handed a write allowlist at all. With no allowlist the server
    refuses every write itself, so the absence of GOOGLE_MCP_CONFIG *is* the
    read-only floor - which is why these tests assert on that key's presence
    rather than on a set of tool names.
    """

    def test_sheets_absent_unless_flag_set(self) -> None:
        # Gmail/Calendar on must not drag Sheets in with them.
        servers = _render_config(
            {"modules": {"google": {"gmail": True, "calendar": True}}}
        )
        self.assertNotIn("google-sheets", servers)

    def test_sheets_registers_when_flag_set(self) -> None:
        servers = _render_config({"modules": {"google": {"sheets": True}}})
        entry = servers["google-sheets"]
        self.assertEqual(entry["command"], "npx")
        self.assertIn("@shivaduke28/google-sheets-mcp", entry["args"])
        self.assertFalse([k for k in entry if k.startswith("_")])
        # Enabling Sheets must not drag Gmail or Calendar in with it.
        self.assertNotIn("gmail", servers)
        self.assertNotIn("google-calendar", servers)

    def test_sheets_reuses_the_shared_oauth_client(self) -> None:
        # The whole point of #63: one OAuth client, not a second credential
        # mechanism. The Sheets server must read the SAME placeholder the
        # calendar entry reads.
        sheets = _render_config({"modules": {"google": {"sheets": True}}})[
            "google-sheets"
        ]
        calendar = _render_config({"modules": {"google": {"calendar": True}}})[
            "google-calendar"
        ]
        self.assertEqual(
            sheets["env"]["GOOGLE_OAUTH_CREDENTIALS"],
            calendar["env"]["GOOGLE_OAUTH_CREDENTIALS"],
        )

    def test_sheets_defaults_to_read_floor_when_trust_line_absent(self) -> None:
        # No trust_line at all: no allowlist, so the server denies every write.
        entry = _render_config({"modules": {"google": {"sheets": True}}})[
            "google-sheets"
        ]
        self.assertNotIn("GOOGLE_MCP_CONFIG", entry["env"])
        self.assertEqual(
            set(entry["env"]), {"GOOGLE_OAUTH_CREDENTIALS", "GOOGLE_OAUTH_TOKENS"}
        )

    def test_sheets_read_only_trust_line_withholds_the_allowlist(self) -> None:
        entry = _render_config(
            {
                "modules": {"google": {"sheets": True}},
                "trust_line": {"sheets": "read_only"},
            }
        )["google-sheets"]
        self.assertNotIn("GOOGLE_MCP_CONFIG", entry["env"])

    def test_sheets_read_write_trust_line_grants_the_allowlist(self) -> None:
        entry = _render_config(
            {
                "modules": {"google": {"sheets": True}},
                "trust_line": {"sheets": "read_write"},
            }
        )["google-sheets"]
        self.assertEqual(entry["env"]["GOOGLE_MCP_CONFIG"], "<GOOGLE_SHEETS_ALLOWLIST>")
        # Widened, not swapped - the credentials must survive the override.
        self.assertIn("GOOGLE_OAUTH_CREDENTIALS", entry["env"])
        self.assertIn("GOOGLE_OAUTH_TOKENS", entry["env"])

    def test_shipped_config_defaults_sheets_off_and_read_only(self) -> None:
        import yaml  # noqa: PLC0415 - test-only dependency

        with open(REPO_ROOT / "chassis.config.yaml") as f:
            shipped = yaml.safe_load(f)
        self.assertIs(shipped["modules"]["google"]["sheets"], False)
        self.assertEqual(shipped["trust_line"]["sheets"], "read_only")


class OverrideWhenTest(unittest.TestCase):
    ENTRY = {
        "_enable_when": "chassis.config.yaml.modules.x == true",
        "env": {"TOOLS": "read"},
        "_override_when": [
            {
                "predicate": "chassis.config.yaml.trust_line.x == 'read_write'",
                "set": {"env.TOOLS": "read,write"},
            }
        ],
    }

    def test_override_applies_when_predicate_holds(self) -> None:
        out = hydrator.apply_overrides(self.ENTRY, {"trust_line": {"x": "read_write"}})
        self.assertEqual(out["env"]["TOOLS"], "read,write")

    def test_override_skipped_when_predicate_fails(self) -> None:
        out = hydrator.apply_overrides(self.ENTRY, {"trust_line": {"x": "read_only"}})
        self.assertEqual(out["env"]["TOOLS"], "read")

    def test_override_skipped_on_missing_config_path(self) -> None:
        out = hydrator.apply_overrides(self.ENTRY, {})
        self.assertEqual(out["env"]["TOOLS"], "read")

    def test_template_entry_is_not_mutated(self) -> None:
        # apply_overrides deep-copies. A shared template dict must survive being
        # rendered against a permissive config and then a restrictive one.
        hydrator.apply_overrides(self.ENTRY, {"trust_line": {"x": "read_write"}})
        self.assertEqual(self.ENTRY["env"]["TOOLS"], "read")

    def test_entry_without_override_when_is_returned_unchanged(self) -> None:
        entry = {"command": "npx"}
        self.assertEqual(hydrator.apply_overrides(entry, {}), entry)

    def test_malformed_clauses_are_ignored(self) -> None:
        for clauses in ("not-a-list", ["not-a-dict"], [{"predicate": "x == 'y'"}], [{}]):
            entry = {"env": {"TOOLS": "read"}, "_override_when": clauses}
            out = hydrator.apply_overrides(entry, {"trust_line": {"x": "read_write"}})
            self.assertEqual(out["env"]["TOOLS"], "read")


class EnableWhenEvaluatorTest(unittest.TestCase):
    CONFIG = {"second_brain": {"backend": "siyuan", "mode": "adapter"}}

    def _eval(self, predicate: str, config=None) -> bool:
        return hydrator.evaluate_enable_when(
            predicate, self.CONFIG if config is None else config
        )

    def test_equality(self) -> None:
        self.assertTrue(self._eval("chassis.config.yaml.second_brain.backend == 'siyuan'"))
        self.assertFalse(self._eval("chassis.config.yaml.second_brain.backend == 'notion'"))

    def test_inequality(self) -> None:
        self.assertFalse(self._eval("chassis.config.yaml.second_brain.mode != 'adapter'"))
        self.assertTrue(self._eval("chassis.config.yaml.second_brain.mode != 'direct'"))

    def test_inequality_on_missing_path_is_true(self) -> None:
        config = {"second_brain": {"backend": "siyuan"}}
        self.assertTrue(
            self._eval("chassis.config.yaml.second_brain.mode != 'adapter'", config)
        )

    def test_equality_on_missing_path_is_false(self) -> None:
        config = {"second_brain": {"backend": "siyuan"}}
        self.assertFalse(
            self._eval("chassis.config.yaml.second_brain.mode == 'adapter'", config)
        )

    def test_conjunction(self) -> None:
        self.assertTrue(
            self._eval(
                "chassis.config.yaml.second_brain.backend == 'siyuan' "
                "&& chassis.config.yaml.second_brain.mode == 'adapter'"
            )
        )
        self.assertFalse(
            self._eval(
                "chassis.config.yaml.second_brain.backend == 'siyuan' "
                "&& chassis.config.yaml.second_brain.mode != 'adapter'"
            )
        )

    def test_unparsable_predicates_are_false(self) -> None:
        self.assertFalse(self._eval("garbage"))
        self.assertFalse(self._eval(""))
        self.assertFalse(self._eval("a == 'x' && "))


if __name__ == "__main__":
    unittest.main(verbosity=2)
