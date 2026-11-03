"""TripWires tab: what is being watched, with cancel, archive and a small add
form. The logic lives in chassis/scripts/_tripwires.py.

build_router takes the renderer and the store so an install with its own
dashboard app can mount the same tab; only the default renderer uses tabs.py.
"""
from __future__ import annotations

import sys
import urllib.parse
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.append(str(SCRIPTS_DIR))
import _tripwires as tw  # noqa: E402

MAX_FORM_BYTES = 64 * 1024


def _default_render(request: Request, context: dict, status_code: int = 200):
    from tabs import page
    return page(request, "tripwires.html", context, status_code=status_code)


async def _form(request: Request) -> dict[str, str]:
    # By hand so the dashboard does not need python-multipart for one form.
    body = await request.body()
    if len(body) > MAX_FORM_BYTES:
        raise HTTPException(413, "form too large")
    return {k: v[-1] for k, v in urllib.parse.parse_qs(body.decode("utf8", "replace")).items()}


def build_router(render: Callable = _default_render, store_dir: Path | None = None,
                 prefix: str = "/tripwires") -> APIRouter:
    router = APIRouter()

    def store() -> tw.Store:
        return tw.Store(store_dir)

    def refuse_foreign(request: Request) -> None:
        if not tw.origin_allowed(request.headers):
            raise HTTPException(403, "cross-origin request refused")

    def show(request: Request, status_code: int = 200, error: str | None = None, form: dict | None = None):
        show_archived = request.query_params.get("archived") == "1"
        context = {**tw.view(store(), show_archived=show_archived), "active_section": "tripwires",
                   "prefix": prefix, "error": error, "form": form or {}}
        return render(request, context, status_code)

    def back() -> RedirectResponse:
        return RedirectResponse(prefix, status_code=303)

    @router.get("")
    def index(request: Request):
        return show(request)

    @router.post("/add")
    async def add(request: Request):
        refuse_foreign(request)
        form = await _form(request)
        try:
            tw.add(store(), name=form.get("name"), why=form.get("why"), check=form.get("check"),
                   url=form.get("url"), text=form.get("text"), status=form.get("status"),
                   every=form.get("every"), expires=form.get("expires"), message=form.get("message"),
                   keep_watching=form.get("keep_watching") == "1")
        except (tw.TripwireError, tw.StoreBusy, tw.StoreUnavailable) as e:
            return show(request, 400, str(e), form)
        return back()

    @router.post("/{tripwire_id}/cancel")
    def cancel(request: Request, tripwire_id: str):
        return transition(request, tw.cancel, tripwire_id)

    @router.post("/{tripwire_id}/archive")
    def archive(request: Request, tripwire_id: str):
        return transition(request, tw.archive, tripwire_id)

    def transition(request: Request, action: Callable, tripwire_id: str):
        refuse_foreign(request)
        try:
            action(store(), tripwire_id)
        except (tw.TripwireError, tw.StoreBusy, tw.StoreUnavailable) as e:
            return show(request, 400, str(e))
        return back()

    return router


router = build_router()
