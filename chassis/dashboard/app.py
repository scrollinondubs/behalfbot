#!/usr/bin/env python3
"""Behalf.bot dashboard: Briefings, DevOps and Projections, plus tabs from
installed plugins and from $CUSTOMER_HOME/dashboard/tabs/.

Run:
    python3 chassis/dashboard/app.py
    python3 -m uvicorn app:app --app-dir chassis/dashboard --port 8765

Env:
    CUSTOMER_HOME     customer state root (falls back like _env.sh)
    DASHBOARD_PORT    default BRIEFING_PORT, else 8765
    DASHBOARD_BIND    default BRIEFING_BIND, else 127.0.0.1
    INSTANCE_NAME     brand shown in the sidebar
    CHASSIS_PLUGINS_ROOT  plugin root override

Host dependencies: fastapi, uvicorn, jinja2 (pyyaml to read the DevOps
config). See chassis/dashboard/requirements.txt and chassis/docs/dashboard.md.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parent
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import tabs  # noqa: E402  (puts chassis/scripts on sys.path)
import _spend  # noqa: E402
from tabs import REGISTRY, Registry, Tab  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s dashboard %(levelname)s %(message)s")
log = logging.getLogger("dashboard")


def load_core(reg: Registry) -> None:
    from core import briefings, devops, projections

    reg.add(Tab("Briefings", "/briefings", briefings.router, "core", order=10))
    reg.add(Tab("DevOps", "/devops", devops.router, "core", order=20))
    if _spend.has_services():
        reg.add(Tab("Projections", "/projections", projections.router, "core", order=30))


def create_app(reg: Registry = REGISTRY, plugins_root: Path | None = None,
               custom_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="Behalf.bot dashboard", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=DASHBOARD_DIR / "static"), name="static")
    # Plugin routers render with request.app.state.templates.TemplateResponse(...).
    # Same Jinja env as page(): chassis templates plus every tab's templates_dir.
    app.state.templates = tabs.templates

    load_core(reg)
    tabs.load_plugins(reg, plugins_root)
    tabs.load_custom(reg, custom_dir)

    for tab in list(reg.tabs):
        try:
            app.include_router(tab.router, prefix=tab.prefix)
        except Exception as e:  # noqa: BLE001
            reg.tabs.remove(tab)
            reg.fail(f"{tab.section} tab", tab.label, e)
    tabs.use_registry(reg)

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse(url="/briefings")

    from core.briefings import legacy_root
    app.include_router(legacy_root)

    log.info("tabs: %s", ", ".join(f"{t.label} ({t.section})" for t in reg.tabs))
    for e in reg.errors:
        log.info("not loaded: %s %s", e.kind, e.ident)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    port = int(os.environ.get("DASHBOARD_PORT") or os.environ.get("BRIEFING_PORT") or 8765)
    bind = os.environ.get("DASHBOARD_BIND") or os.environ.get("BRIEFING_BIND") or "127.0.0.1"
    uvicorn.run(app, host=bind, port=port, log_level="info")


if __name__ == "__main__":
    main()
