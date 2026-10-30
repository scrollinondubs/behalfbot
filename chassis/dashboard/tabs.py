"""Tab contract, loaders and the shared page renderer.

Three kinds of tab share one contract:

  core      shipped here (Briefings, DevOps, Projections)
  plugins   declared in a plugin's openclaw.plugin.json under
            contracts.dashboard, imported from the resolved plugin root
  custom    any *.py in $CUSTOMER_HOME/dashboard/tabs/ defining TAB + router

A tab that fails to import is skipped with one logged line and a row on
/devops. It never takes the app down.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup

DASHBOARD_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = DASHBOARD_DIR.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _paths import customer_home, plugins_root  # noqa: E402

log = logging.getLogger("dashboard")

SECTIONS = ("core", "plugins", "custom")


@dataclass
class Tab:
    label: str
    prefix: str
    router: APIRouter
    section: str
    order: int = 100
    templates_dir: Path | None = None


@dataclass
class LoadError:
    kind: str      # "plugin tab" | "plugin panel" | "custom tab"
    ident: str
    error: str


@dataclass
class Registry:
    tabs: list[Tab] = field(default_factory=list)
    panels: list[tuple[str, Callable[[], dict]]] = field(default_factory=list)
    errors: list[LoadError] = field(default_factory=list)

    def add(self, tab: Tab) -> None:
        if not tab.prefix or not tab.prefix.startswith("/") or tab.prefix == "/":
            raise ValueError(f"tab prefix must start with / and not be empty: {tab.prefix!r}")
        if tab.section not in SECTIONS:
            raise ValueError(f"unknown section {tab.section!r}")
        if any(t.prefix == tab.prefix for t in self.tabs):
            raise ValueError(f"prefix {tab.prefix} is already taken")
        self.tabs.append(tab)
        if tab.templates_dir:
            add_templates_dir(tab.templates_dir)

    def fail(self, kind: str, ident: str, err: BaseException) -> None:
        msg = f"{type(err).__name__}: {err}"
        self.errors.append(LoadError(kind, ident, msg))
        log.error("%s failed to load: %s: %s", kind, ident, msg)
        log.debug("".join(traceback.format_exception(type(err), err, err.__traceback__)))

    def nav(self) -> list[dict]:
        out = []
        for section in SECTIONS:
            items = sorted((t for t in self.tabs if t.section == section), key=lambda t: (t.order, t.label))
            if items:
                out.append({"section": section, "tabs": items})
        return out


REGISTRY = Registry()


# ---- templates ---------------------------------------------------------------

_env = Environment(
    loader=FileSystemLoader([str(DASHBOARD_DIR / "templates")]),
    autoescape=True, cache_size=0, auto_reload=True,
)
_env.globals["css_version"] = int(time.time())
_env.globals["brand"] = os.environ.get("INSTANCE_NAME", "").strip() or "Behalf.bot"
_env.globals["nav"] = REGISTRY.nav


def _is_active(tab: Tab, active_section: str | None, path: str = "") -> bool:
    """A template's active_section wins; without one, the URL path decides."""
    if active_section:
        return active_section.strip("/") == tab.prefix.strip("/")
    return path == tab.prefix or path.startswith(tab.prefix + "/")


_env.globals["is_active"] = _is_active
templates = Jinja2Templates(env=_env)


def use_registry(reg: Registry) -> None:
    """Point the nav and the /devops failure rows at `reg`."""
    global REGISTRY
    REGISTRY = reg
    _env.globals["nav"] = reg.nav


def current_registry() -> Registry:
    return REGISTRY


def add_templates_dir(path: Path) -> None:
    p = str(path)
    if p not in _env.loader.searchpath:
        _env.loader.searchpath.append(p)


def page(request: Request, template_or_title: str, context_or_html: dict | str | None = None,
         status_code: int = 200) -> HTMLResponse:
    """Render inside base.html with the nav.

    page(request, "thing.html", {...})   renders a template that extends base.html
    page(request, "Title", "<p>html</p>") wraps raw HTML in the shared shell
    """
    if template_or_title.endswith(".html") and not isinstance(context_or_html, str):
        return templates.TemplateResponse(request, template_or_title, dict(context_or_html or {}),
                                          status_code=status_code)
    return templates.TemplateResponse(request, "page.html", {
        "title": template_or_title,
        "body": Markup(context_or_html or ""),
    }, status_code=status_code)


# ---- module import -------------------------------------------------------------

def _module_name(kind: str, ident: str, path: Path) -> str:
    safe = re.sub(r"\W", "_", f"{ident}_{path.stem}")
    return f"behalfbot_dash_{kind}_{safe}"


def import_file(path: Path, name: str):
    """Import one file as a module. Its own directory sits on sys.path while
    it executes, so sibling imports work. SystemExit is caught by callers."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    parent = str(path.parent)
    added = parent not in sys.path
    if added:
        sys.path.insert(0, parent)
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    finally:
        if added and parent in sys.path:
            sys.path.remove(parent)
    return mod


# ---- plugins -------------------------------------------------------------------

def _plugin_dirs(root: Path) -> list[Path]:
    try:
        return sorted(d for d in root.iterdir() if (d / "openclaw.plugin.json").is_file())
    except OSError:
        return []


def load_plugins(reg: Registry, root: Path | None = None) -> None:
    root = root if root is not None else plugins_root()
    if root is None:
        return
    for pdir in _plugin_dirs(root):
        ident = pdir.name
        try:
            manifest = json.loads((pdir / "openclaw.plugin.json").read_text())
            ident = manifest.get("id") or pdir.name
            spec = (manifest.get("contracts") or {}).get("dashboard")
        except (Exception, SystemExit) as e:  # noqa: BLE001
            reg.fail("plugin tab", ident, e)
            continue
        if not spec:
            continue
        if spec.get("router"):
            try:
                path = pdir / spec["router"]
                mod = import_file(path, _module_name("plugin", ident, path))
                tdir = pdir / spec["templates"] if spec.get("templates") else None
                reg.add(Tab(
                    label=spec.get("tab") or ident,
                    prefix=spec["prefix"],
                    router=mod.router,
                    section="plugins",
                    order=int(spec.get("order", 100)),
                    templates_dir=tdir if tdir and tdir.is_dir() else None,
                ))
            except (Exception, SystemExit) as e:  # noqa: BLE001
                reg.fail("plugin tab", ident, e)
        for rel in spec.get("devops_panels") or []:
            try:
                path = pdir / rel
                mod = import_file(path, _module_name("panel", ident, path))
                if not callable(getattr(mod, "panel", None)):
                    raise AttributeError(f"{rel} defines no panel()")
                reg.panels.append((f"{ident}:{rel}", mod.panel))
            except (Exception, SystemExit) as e:  # noqa: BLE001
                reg.fail("plugin panel", f"{ident}: {rel}", e)


# ---- customer tabs -------------------------------------------------------------

def load_custom(reg: Registry, tabs_dir: Path | None = None) -> None:
    tabs_dir = tabs_dir if tabs_dir is not None else customer_home() / "dashboard" / "tabs"
    if not tabs_dir.is_dir():
        return
    for path in sorted(tabs_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            mod = import_file(path, _module_name("custom", "tab", path))
            meta, router = getattr(mod, "TAB", None), getattr(mod, "router", None)
            if meta is None and router is None:
                continue
            if not isinstance(meta, dict) or router is None:
                raise AttributeError("needs both TAB = {'label', 'prefix'} and router")
            tdir = meta.get("templates")
            reg.add(Tab(
                label=meta["label"],
                prefix=meta["prefix"],
                router=router,
                section="custom",
                order=int(meta.get("order", 100)),
                templates_dir=(path.parent / tdir) if tdir else None,
            ))
        except (Exception, SystemExit) as e:  # noqa: BLE001
            reg.fail("custom tab", path.name, e)


# ---- devops panels -------------------------------------------------------------

def run_panels(reg: Registry) -> list[dict]:
    """Call each registered panel(). A panel that raises or returns junk
    becomes a fail row instead of a 500."""
    out = []
    for ident, fn in reg.panels:
        try:
            p = fn()
            if not isinstance(p, dict) or not isinstance(p.get("rows"), list):
                raise TypeError("panel() must return {'title': str, 'rows': [...]}")
            out.append({"title": str(p.get("title") or ident), "rows": p["rows"]})
        except Exception as e:  # noqa: BLE001
            out.append({"title": ident, "rows": [
                {"label": "panel", "value": "error", "status": "fail", "detail": f"{type(e).__name__}: {e}"},
            ]})
    return out
