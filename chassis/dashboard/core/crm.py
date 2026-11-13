"""CRM tab: contacts, lists (events) and who is on which list, in the chassis
Postgres. The data logic lives in chassis/scripts/_crm.py.

build_router takes the renderer and the store so an install with its own
dashboard app can mount the same tab; only the default renderer uses tabs.py.
Without a DSN, or before the migration has run, every page shows what to set
instead of an error.
"""
from __future__ import annotations

import functools
import inspect
import sys
import urllib.parse
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.append(str(SCRIPTS_DIR))
import _crm as crm  # noqa: E402
from _tripwires import origin_allowed  # noqa: E402

MAX_FORM_BYTES = 64 * 1024


def _default_render(request: Request, template: str, context: dict, status_code: int = 200):
    from tabs import page
    return page(request, template, context, status_code=status_code)


async def _form(request: Request) -> dict[str, str]:
    body = await request.body()
    if len(body) > MAX_FORM_BYTES:
        raise HTTPException(413, "form too large")
    return {k: v[-1] for k, v in urllib.parse.parse_qs(body.decode("utf8", "replace"),
                                                        keep_blank_values=True).items()}


def build_router(render: Callable = _default_render, store: crm.Store | None = None,
                 prefix: str = "/crm") -> APIRouter:
    router = APIRouter()
    db = store or crm.Store()

    def refuse_foreign(request: Request) -> None:
        if not origin_allowed(request.headers):
            raise HTTPException(403, "cross-origin request refused")

    def show(request: Request, template: str, context: dict, status_code: int = 200):
        base = {"active_section": "crm", "prefix": prefix, "statuses": crm.STATUSES,
                "error": None, "form": {}}
        return render(request, template, {**base, **context}, status_code)

    def unavailable(request: Request, e: crm.CrmUnavailable):
        return show(request, "crm_unavailable.html", {
            "view": "unavailable", "reason": e.reason, "hint": crm.unavailable_hint(e.reason),
            "detail": str(e) if e.reason == "unreachable" else None}, 503)

    def not_found(what: str):
        raise HTTPException(404, f"{what} not found")

    def go(path: str) -> RedirectResponse:
        return RedirectResponse(prefix + path, status_code=303)

    def guarded(fn: Callable) -> Callable:
        """Turns a missing database into the setup page on every route."""
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapper(request: Request, *a, **kw):
                try:
                    return await fn(request, *a, **kw)
                except crm.CrmUnavailable as e:
                    return unavailable(request, e)
        else:
            @functools.wraps(fn)
            def wrapper(request: Request, *a, **kw):
                try:
                    return fn(request, *a, **kw)
                except crm.CrmUnavailable as e:
                    return unavailable(request, e)
        return wrapper

    # ---- contacts

    def contacts_page(request: Request, status_code: int = 200, error: str | None = None,
                      form: dict | None = None):
        q = (request.query_params.get("q") or "").strip()
        return show(request, "crm_contacts.html", {
            "view": "contacts", "q": q, "contacts": db.contacts(q),
            "error": error, "form": form or {}}, status_code)

    @router.get("")
    @guarded
    def index(request: Request):
        return contacts_page(request)

    @router.post("/contacts")
    @guarded
    async def add_contact(request: Request):
        refuse_foreign(request)
        form = await _form(request)
        try:
            cid = db.add_contact(crm.clean_contact(form))
        except crm.CrmError as e:
            return contacts_page(request, 400, str(e), form)
        return go(f"/contacts/{cid}")

    def contact_page(request: Request, contact_id: int, status_code: int = 200,
                     error: str | None = None, form: dict | None = None):
        contact = db.contact(contact_id) or not_found("contact")
        return show(request, "crm_contact.html", {
            "view": "contact", "contact": contact, "form": form or contact,
            "memberships": db.memberships_of(contact_id), "error": error}, status_code)

    @router.get("/contacts/{contact_id}")
    @guarded
    def contact(request: Request, contact_id: int):
        return contact_page(request, contact_id)

    @router.post("/contacts/{contact_id}")
    @guarded
    async def edit_contact(request: Request, contact_id: int):
        refuse_foreign(request)
        form = await _form(request)
        try:
            fields = crm.clean_contact(form)
        except crm.CrmError as e:
            return contact_page(request, contact_id, 400, str(e), {**form, "id": contact_id})
        if not db.update_contact(contact_id, fields):
            not_found("contact")
        return go(f"/contacts/{contact_id}")

    @router.post("/contacts/{contact_id}/delete")
    @guarded
    def delete_contact(request: Request, contact_id: int):
        refuse_foreign(request)
        if not db.delete_contact(contact_id):
            not_found("contact")
        return go("")

    # ---- lists

    def lists_page(request: Request, status_code: int = 200, error: str | None = None,
                   form: dict | None = None):
        return show(request, "crm_lists.html", {
            "view": "lists", "lists": db.lists(), "error": error, "form": form or {}}, status_code)

    @router.get("/lists")
    @guarded
    def lists(request: Request):
        return lists_page(request)

    @router.post("/lists")
    @guarded
    async def add_list(request: Request):
        refuse_foreign(request)
        form = await _form(request)
        try:
            lid = db.add_list(crm.clean_list(form))
        except crm.CrmError as e:
            return lists_page(request, 400, str(e), form)
        return go(f"/lists/{lid}")

    def list_page(request: Request, list_id: int, status_code: int = 200, error: str | None = None,
                  list_form: dict | None = None):
        the_list = db.get_list(list_id) or not_found("list")
        members = db.members(list_id)
        status = request.query_params.get("status") or ""
        shown = [m for m in members if m["status"] == status] if status in crm.STATUSES else members
        on_list = {m["contact_id"] for m in members}
        return show(request, "crm_list.html", {
            "view": "list", "list": the_list, "list_form": list_form or the_list,
            "members": shown, "status_filter": status if status in crm.STATUSES else "",
            "summary": crm.summarize(members),
            "candidates": [c for c in db.contacts() if c["id"] not in on_list],
            "error": error}, status_code)

    @router.get("/lists/{list_id}")
    @guarded
    def one_list(request: Request, list_id: int):
        return list_page(request, list_id)

    @router.post("/lists/{list_id}")
    @guarded
    async def edit_list(request: Request, list_id: int):
        refuse_foreign(request)
        form = await _form(request)
        try:
            fields = crm.clean_list(form)
        except crm.CrmError as e:
            return list_page(request, list_id, 400, str(e), {**form, "id": list_id})
        if not db.update_list(list_id, fields):
            not_found("list")
        return go(f"/lists/{list_id}")

    @router.post("/lists/{list_id}/delete")
    @guarded
    def delete_list(request: Request, list_id: int):
        refuse_foreign(request)
        if not db.delete_list(list_id):
            not_found("list")
        return go("/lists")

    @router.post("/lists/{list_id}/members")
    @guarded
    async def add_member(request: Request, list_id: int):
        refuse_foreign(request)
        form = await _form(request)
        try:
            contact_id = crm.parse_id(form.get("contact_id"), "contact")
            fields = crm.clean_member(form)
            if not db.get_list(list_id):
                not_found("list")
            if not db.contact(contact_id):
                raise crm.CrmError("that contact no longer exists")
            if not db.add_member(list_id, contact_id, fields):
                raise crm.CrmError("that contact is already on this list")
        except crm.CrmError as e:
            return list_page(request, list_id, 400, str(e))
        return go(f"/lists/{list_id}")

    @router.post("/lists/{list_id}/members/{contact_id}")
    @guarded
    async def edit_member(request: Request, list_id: int, contact_id: int):
        refuse_foreign(request)
        form = await _form(request)
        try:
            if not db.update_member(list_id, contact_id, crm.clean_member(form, partial=True)):
                not_found("member")
        except crm.CrmError as e:
            return list_page(request, list_id, 400, str(e))
        back = form.get("back_status") or ""
        return go(f"/lists/{list_id}" + (f"?status={back}" if back in crm.STATUSES else "")
                  + f"#m{contact_id}")

    @router.post("/lists/{list_id}/members/{contact_id}/remove")
    @guarded
    def remove_member(request: Request, list_id: int, contact_id: int):
        refuse_foreign(request)
        if not db.remove_member(list_id, contact_id):
            not_found("member")
        return go(f"/lists/{list_id}")

    return router


router = build_router()
