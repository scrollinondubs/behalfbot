"""Tests for the chassis dashboard: tab loading, failure isolation, the DevOps
config parser, plugin-root resolution and the disk-map tree.

Run from repo root:
  python3 -m pytest chassis/scripts/tests/test_dashboard.py -v

Needs fastapi, jinja2, httpx (TestClient) and pyyaml. Every test builds its
own customer home and plugin root in a temp dir; nothing reads the real
install.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

try:
    import fastapi  # noqa: F401
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except ImportError:  # pragma: no cover
    HAVE_FASTAPI = False

try:
    import yaml  # noqa: F401
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False

CHASSIS = Path(__file__).resolve().parents[2]
DASHBOARD = CHASSIS / "dashboard"
SCRIPTS = CHASSIS / "scripts"

_TMP = tempfile.mkdtemp(prefix="dashboard-test-")
_EMPTY_PLUGINS = Path(_TMP) / "no-plugins"
_EMPTY_PLUGINS.mkdir()
for p in (str(DASHBOARD), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

if HAVE_FASTAPI:
    # app.py builds an app at import; point it at an empty install while it
    # does, then put the environment back for the rest of the suite.
    with mock.patch.dict(os.environ, {"CUSTOMER_HOME": str(Path(_TMP) / "import-home"),
                                      "CHASSIS_PLUGINS_ROOT": str(_EMPTY_PLUGINS)}):
        import _spend  # noqa: E402
        import app as dash_app  # noqa: E402
        import tabs  # noqa: E402
        from core import devops  # noqa: E402


def tearDownModule() -> None:
    shutil.rmtree(_TMP, ignore_errors=True)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


GOOD_TAB = """
    from fastapi import APIRouter, Request
    from helper import WORD
    from tabs import page
    router = APIRouter()
    @router.get("")
    def index(request: Request):
        return page(request, "Good", "<p id='w'>" + WORD + "</p>")
"""


@unittest.skipUnless(HAVE_FASTAPI, "fastapi not installed")
class DashboardCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dash-case-", dir=_TMP))
        self.home = self.tmp / "home"
        self.plugins = self.tmp / "plugins"
        (self.home / "briefings").mkdir(parents=True)
        self.plugins.mkdir()
        self.env = mock.patch.dict(os.environ, {
            "CUSTOMER_HOME": str(self.home),
            "CHASSIS_PLUGINS_ROOT": str(self.plugins),
        })
        self.env.start()
        cfg = self.home / "config" / "spend-services.json"
        self.spend = [mock.patch.object(_spend, "CONFIG_PATH", cfg),
                      mock.patch.object(_spend, "DB_PATH", self.home / "state" / "projections" / "spend.db")]
        for p in self.spend:
            p.start()

    def tearDown(self) -> None:
        for p in self.spend:
            p.stop()
        self.env.stop()

    def plugin(self, name: str, dashboard: dict | None, files: dict[str, str]) -> Path:
        d = self.plugins / name
        manifest = {"id": f"{name}-id"}
        if dashboard is not None:
            manifest["contracts"] = {"dashboard": dashboard}
        write(d / "openclaw.plugin.json", json.dumps(manifest))
        for rel, text in files.items():
            write(d / rel, text)
        return d

    def client(self) -> tuple["TestClient", "tabs.Registry"]:
        reg = tabs.Registry()
        return TestClient(dash_app.create_app(reg, self.plugins, self.home / "dashboard" / "tabs")), reg


class TabLoading(DashboardCase):
    def test_core_tabs_and_no_projections_without_spend_config(self) -> None:
        c, reg = self.client()
        self.assertEqual([t.label for t in reg.tabs], ["Briefings", "DevOps"])
        self.assertEqual(c.get("/briefings").status_code, 200)
        self.assertEqual(c.get("/devops").status_code, 200)
        self.assertEqual(c.get("/projections").status_code, 404)
        self.assertNotIn('href="/projections"', c.get("/devops").text)

    def test_projections_needs_at_least_one_service(self) -> None:
        write(self.home / "config" / "spend-services.json", '{"_settings": {}}')
        _, reg = self.client()
        self.assertNotIn("Projections", [t.label for t in reg.tabs])
        write(self.home / "config" / "spend-services.json",
              '{"claude": {"label": "Claude", "kind": "fixed", "fixed": 100}}')
        c, reg = self.client()
        self.assertIn("Projections", [t.label for t in reg.tabs])
        r = c.get("/projections")
        self.assertEqual(r.status_code, 200)
        self.assertIn("No spend data yet", r.text)

    def test_plugin_tab_mounts_with_sibling_import_and_templates(self) -> None:
        self.plugin("good", {"tab": "Good", "prefix": "/good", "router": "dashboard/tab.py",
                             "templates": "dashboard/templates"},
                    {"dashboard/tab.py": GOOD_TAB, "dashboard/helper.py": "WORD = 'sibling-ok'\n",
                     "dashboard/templates/x.html": "x"})
        c, reg = self.client()
        tab = next(t for t in reg.tabs if t.label == "Good")
        self.assertEqual(tab.section, "plugins")
        self.assertIn(str(self.plugins / "good" / "dashboard" / "templates"), tabs._env.loader.searchpath)
        r = c.get("/good")
        self.assertEqual(r.status_code, 200)
        self.assertIn("sibling-ok", r.text)
        self.assertIn('href="/good" class="nav-item active"', r.text)

    def test_plugin_renders_through_app_state_templates(self) -> None:
        self.plugin("st", {"tab": "State", "prefix": "/st", "router": "dashboard/tab.py",
                           "templates": "dashboard/templates"}, {
            "dashboard/tab.py": """
                from fastapi import APIRouter, Request
                router = APIRouter()
                @router.get("/")
                def index(request: Request):
                    return request.app.state.templates.TemplateResponse(request, "st_page.html", {"word": "via-state"})
            """,
            "dashboard/templates/st_page.html": """
                {% extends "base.html" %}
                {% set active_section = "/st" %}
                {% block content %}<p class="panel">{{ word }}</p>{% endblock %}
            """,
        })
        c, _ = self.client()
        r = c.get("/st", follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (307, "/st/"))
        r = c.get("/st")
        self.assertEqual(r.status_code, 200)
        self.assertIn("via-state", r.text)
        self.assertIn('href="/st" class="nav-item active"', r.text)
        self.assertIn('href="/devops" class="nav-item "', r.text)

    def test_plugin_that_raises_on_import_is_isolated(self) -> None:
        self.plugin("bad", {"tab": "Bad", "prefix": "/bad", "router": "dashboard/tab.py"},
                    {"dashboard/tab.py": "raise RuntimeError('boom on import')\n"})
        self.plugin("exits", {"tab": "Exits", "prefix": "/exits", "router": "dashboard/tab.py"},
                    {"dashboard/tab.py": "import sys\nsys.exit('missing env')\n"})
        self.plugin("good", {"tab": "Good", "prefix": "/good", "router": "dashboard/tab.py"},
                    {"dashboard/tab.py": GOOD_TAB, "dashboard/helper.py": "WORD = 'x'\n"})
        c, reg = self.client()
        self.assertEqual({e.ident for e in reg.errors}, {"bad-id", "exits-id"})
        self.assertEqual(c.get("/good").status_code, 200)
        self.assertEqual(c.get("/bad").status_code, 404)
        page = c.get("/devops")
        self.assertEqual(page.status_code, 200)
        self.assertIn("plugin tab failed to load: bad-id: RuntimeError: boom on import", page.text)
        self.assertIn("plugin tab failed to load: exits-id: SystemExit: missing env", page.text)

    def test_broken_manifest_and_plugins_without_dashboard(self) -> None:
        write(self.plugins / "junk" / "openclaw.plugin.json", "{not json")
        self.plugin("plain", None, {})
        _, reg = self.client()
        self.assertEqual([e.ident for e in reg.errors], ["junk"])
        self.assertEqual([t.label for t in reg.tabs], ["Briefings", "DevOps"])

    def test_duplicate_prefix_and_core_prefix_are_refused(self) -> None:
        for name in ("a", "b"):
            self.plugin(name, {"tab": name, "prefix": "/same", "router": "dashboard/tab.py"},
                        {"dashboard/tab.py": "from fastapi import APIRouter\nrouter = APIRouter()\n"})
        self.plugin("c", {"tab": "c", "prefix": "/devops", "router": "dashboard/tab.py"},
                    {"dashboard/tab.py": "from fastapi import APIRouter\nrouter = APIRouter()\n"})
        _, reg = self.client()
        self.assertEqual([t.prefix for t in reg.tabs].count("/same"), 1)
        self.assertEqual(len(reg.errors), 2)

    def test_devops_panels_run_and_a_raising_panel_becomes_a_fail_row(self) -> None:
        self.plugin("p", {"devops_panels": ["dashboard/ok.py", "dashboard/bad.py", "dashboard/none.py"]}, {
            "dashboard/ok.py": "def panel():\n    return {'title': 'Widgets', 'rows': "
                               "[{'label': 'w', 'value': 'spinning', 'status': 'ok', 'detail': ''}]}\n",
            "dashboard/bad.py": "def panel():\n    raise ValueError('panel exploded')\n",
            "dashboard/none.py": "x = 1\n",
        })
        c, reg = self.client()
        self.assertEqual(len(reg.panels), 2)
        self.assertEqual(len(reg.errors), 1)
        panels = tabs.run_panels(reg)
        self.assertEqual(panels[0]["title"], "Widgets")
        self.assertEqual(panels[1]["rows"][0]["status"], "fail")
        page = c.get("/devops").text
        self.assertIn("spinning", page)
        self.assertIn("ValueError: panel exploded", page)

    def test_customer_tabs(self) -> None:
        tdir = self.home / "dashboard" / "tabs"
        write(tdir / "notes.py", """
            from fastapi import APIRouter, Request
            from tabs import page
            TAB = {"label": "Notes", "prefix": "/notes", "order": 5}
            router = APIRouter()
            @router.get("")
            def index(request: Request):
                return page(request, "Notes", "<p>custom-ok</p>")
        """)
        write(tdir / "helpers.py", "VALUE = 1\n")
        write(tdir / "half.py", "TAB = {'label': 'Half', 'prefix': '/half'}\n")
        write(tdir / "broken.py", "raise ImportError('no such lib')\n")
        write(tdir / "_private.py", "raise RuntimeError('never imported')\n")
        c, reg = self.client()
        self.assertIn(("Notes", "custom"), [(t.label, t.section) for t in reg.tabs])
        self.assertEqual(sorted(e.ident for e in reg.errors), ["broken.py", "half.py"])
        self.assertIn("custom-ok", c.get("/notes").text)


class Briefings(DashboardCase):
    def test_index_raw_and_legacy_urls(self) -> None:
        write(self.home / "briefings" / "2026-01-02-morning-briefing.html", "<p>morning</p>")
        write(self.home / ".env", "SECRET=1")
        c, _ = self.client()
        self.assertIn("/briefings/files/2026-01-02-morning-briefing.html", c.get("/briefings").text)
        for url in ("/briefings/files/2026-01-02-morning-briefing.html",
                    "/briefings/2026-01-02-morning-briefing.html",
                    "/2026-01-02-morning-briefing.html"):
            self.assertEqual(c.get(url).text, "<p>morning</p>", url)
        for url in ("/..%2f.env", "/briefings/..%2f.env", "/briefings/files/..%2f.env", "/missing.html"):
            self.assertEqual(c.get(url).status_code, 404, url)

    def test_empty_briefings_dir_renders(self) -> None:
        c, _ = self.client()
        self.assertIn("No briefings yet", c.get("/briefings").text)


class DevopsConfig(DashboardCase):
    @unittest.skipUnless(HAVE_YAML, "pyyaml not installed")
    def test_no_file_no_key_and_bad_yaml(self) -> None:
        self.assertEqual(devops.load_config(), ({}, None))
        write(self.home / "chassis.config.yaml", "instance: {name: x}\n")
        self.assertEqual(devops.load_config(), ({}, None))
        write(self.home / "chassis.config.yaml", "dashboard: [unclosed\n")
        cfg, note = devops.load_config()
        self.assertEqual(cfg, {})
        self.assertIn("not read", note)
        write(self.home / "chassis.config.yaml", "dashboard:\n  devops: 3\n")
        self.assertEqual(devops.load_config()[1], "dashboard.devops must be a mapping")

    @unittest.skipUnless(HAVE_YAML, "pyyaml not installed")
    def test_full_config(self) -> None:
        write(self.home / "chassis.config.yaml", """
            dashboard:
              devops:
                services:
                  - {label: Heartbeat dispatcher, container: behalfbot}
                  - {label: Ollama, http: "http://127.0.0.1:11434/api/tags"}
                  - {label: Discord bot, tmux: assistant-discord}
                  - {label: Old style, tmux_session: legacy}
                  - {label: Dashboard, launchd: com.behalfbot.dashboard}
                  - {label: Two kinds, tmux: a, http: "http://x"}
                  - {label: No kind}
                  - just a string
                remote_hosts:
                  - {ssh: buildbox, containers: [n8n]}
                backups_dir: backups
                disk_map: false
        """)
        cfg, note = devops.load_config()
        self.assertIsNone(note)
        services, notes = devops.normalize_services(cfg["services"])
        self.assertEqual([(s["kind"], s["target"]) for s in services], [
            ("container", "behalfbot"), ("http", "http://127.0.0.1:11434/api/tags"),
            ("tmux", "assistant-discord"), ("tmux", "legacy"), ("launchd", "com.behalfbot.dashboard")])
        self.assertEqual(len(notes), 3)
        self.assertIs(cfg["disk_map"], False)

    @unittest.skipUnless(HAVE_YAML, "pyyaml not installed")
    def test_page_renders_with_every_probe_failing(self) -> None:
        write(self.home / "chassis.config.yaml", """
            dashboard:
              devops:
                services:
                  - {label: Nothing here, http: "http://127.0.0.1:9/"}
                  - {label: No tmux session, tmux: dashboard-test-no-such-session}
                disk_map: false
        """)
        c, _ = self.client()
        page = c.get("/devops")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Nothing here", page.text)
        self.assertNotIn("Open disk map", page.text)

    def test_without_pyyaml_the_page_still_renders_with_a_note(self) -> None:
        write(self.home / "chassis.config.yaml", "dashboard:\n  devops:\n    disk_map: false\n")
        with mock.patch.dict(sys.modules, {"yaml": None}):
            cfg, note = devops.load_config()
            self.assertEqual(cfg, {})
            self.assertIn("pyyaml is not installed", note)
            c, _ = self.client()
            page = c.get("/devops")
        self.assertEqual(page.status_code, 200)
        self.assertIn("pyyaml is not installed", page.text)
        self.assertIn("Open disk map", page.text)

    def test_backups_static_archived_and_stale(self) -> None:
        b = self.home / "backups"
        write(b / "fresh" / "a.sql", "x")
        old = write(b / "old" / "a.sql", "x")
        write(b / "configs" / "c.tgz", "x")
        write(b / "retired" / "r.sql", "x")
        write(b / "loose-file.txt", "x")
        os.utime(old, (0, 0))
        os.utime(b / "configs" / "c.tgz", (0, 0))
        rows = {r["name"]: r for r in devops.scan_backups(
            {"static_backup_targets": ["configs"], "archived_backup_targets": ["retired"]})}
        self.assertEqual(set(rows), {"fresh", "old", "configs"})
        self.assertTrue(rows["fresh"]["ok"])
        self.assertFalse(rows["old"]["ok"])
        self.assertTrue(rows["configs"]["ok"])
        self.assertFalse(rows["configs"]["is_daily"])


class Paths(unittest.TestCase):
    def setUp(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        import _paths
        self.paths = _paths
        self.tmp = Path(tempfile.mkdtemp(prefix="paths-", dir=_TMP))

    def test_customer_home_order(self) -> None:
        with mock.patch.dict(os.environ, {"CUSTOMER_HOME": "/a", "CHASSIS_HOME": "/b"}):
            self.assertEqual(self.paths.customer_home(), Path("/a"))
        env = {k: v for k, v in os.environ.items() if k not in ("CUSTOMER_HOME", "CHASSIS_HOME")}
        with mock.patch.dict(os.environ, {**env, "CHASSIS_HOME": "/b"}, clear=True):
            self.assertEqual(self.paths.customer_home(), Path("/b"))

    def test_plugins_root_order(self) -> None:
        home, resolved, chassis = self.tmp / "home", self.tmp / "resolved", self.tmp / "chassis"
        for d in (home, resolved, chassis / "plugins"):
            d.mkdir(parents=True)
        env = {k: v for k, v in os.environ.items() if k not in ("CHASSIS_PLUGINS_ROOT", "CHASSIS_HOME")}
        env.update({"CUSTOMER_HOME": str(home)})
        with mock.patch.dict(os.environ, {**env, "CHASSIS_PLUGINS_ROOT": "/explicit"}, clear=True):
            self.assertEqual(self.paths.plugins_root(), Path("/explicit"))
        with mock.patch.dict(os.environ, {**env, "CHASSIS_HOME": str(chassis)}, clear=True):
            self.assertEqual(self.paths.plugins_root(), chassis / "plugins")
            (home / "plugins-root.state.json").write_text(json.dumps({"resolved_root": str(resolved)}))
            self.assertEqual(self.paths.plugins_root(), resolved)
            (home / "plugins-root.state.json").write_text(json.dumps({"resolved_root": str(self.tmp / "gone")}))
            self.assertEqual(self.paths.plugins_root(), chassis / "plugins")


class DiskTree(unittest.TestCase):
    def test_small_dirs_fold_into_other_files(self) -> None:
        spec = importlib.util.spec_from_file_location("disk_map_scan", SCRIPTS / "disk-map-scan.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mb = 1024
        sizes = {"/r": 1000 * mb, "/r/big": 600 * mb, "/r/big/inner": 400 * mb, "/r/tiny": 10 * mb}
        tree = mod.build_tree(sizes, ["/r"], "host disk")
        self.assertEqual(mod._total(tree), 1000 * mb)
        r = tree["children"][0]
        self.assertEqual([c["name"] for c in r["children"]], ["big", "(other files)"])
        with mock.patch.dict(os.environ, {"DISK_MAP_ROOTS": "/x:/y"}):
            self.assertEqual(mod.default_roots(), ["/x", "/y"])


if __name__ == "__main__":
    unittest.main()
