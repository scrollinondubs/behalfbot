"""CRM tab: contacts, lists (events) and who is on which list, in the chassis
Postgres. The data logic lives in chassis/scripts/_crm.py.

build_router takes the renderer and the store so an install with its own
dashboard app can mount the same tab; only the default renderer uses tabs.py.
Without a DSN, or before the migration has run, every page shows what to set
instead of an error.
"""
from __future__ import annotations

import functools
import importlib.util
import inspect
import os
import re
import secrets
import stat
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse

SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.append(str(SCRIPTS_DIR))
import _crm as crm  # noqa: E402
import crm_import as ci  # noqa: E402
from _tripwires import origin_allowed  # noqa: E402

MAX_FORM_BYTES = 64 * 1024
IMPORT_MAX_BYTES = 10 * 1024 * 1024
IMPORT_SUFFIXES = (".csv", ".vcf", ".json")
IMPORT_TTL_SECONDS = 15 * 60
IMPORT_SAMPLE = 8
CONTACTS_PER_PAGE = 500
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{20,64}")
FORMAT_LABELS = {
    "linkedin": "LinkedIn Connections.csv", "google": "Google Contacts CSV", "outlook": "Outlook CSV",
    "vcard": "vCard", "facebook": "Facebook friends JSON", "instagram": "Instagram followers/following JSON",
}
ACCEPTED = ("LinkedIn Connections.csv, a Google or Outlook contacts CSV, a vCard (.vcf), "
            "or Facebook / Instagram JSON")


class ImportRefused(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _default_import_dir() -> Path:
    return Path(tempfile.gettempdir()) / f"behalfbot-crm-import-{os.getuid()}"


def _private_dir(path: Path) -> Path:
    """The stash holds contact details: our own directory, nobody else's,
    never a symlink someone planted in a shared temp dir."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise RuntimeError(f"{path} is not a directory owned by this user")
    os.chmod(path, 0o700)
    return path


def _multipart_installed() -> bool:
    return any(importlib.util.find_spec(m) for m in ("python_multipart", "multipart"))


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
                 prefix: str = "/crm", import_dir: Path | None = None) -> APIRouter:
    router = APIRouter()
    db = store or crm.Store()
    stash_root = import_dir or _default_import_dir()

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

    def import_result(request: Request) -> dict | None:
        qp = request.query_params
        if qp.get("imported") not in ci.FORMATS:
            return None
        try:
            nums = {k: int(qp.get(k) or 0) for k in ("new", "enriched", "unchanged", "skipped")}
        except ValueError:
            return None
        return {"label": FORMAT_LABELS[qp["imported"]], **nums}

    def contacts_page(request: Request, status_code: int = 200, error: str | None = None,
                      form: dict | None = None):
        q = (request.query_params.get("q") or "").strip()
        total = db.count_contacts(q)
        pages = max(1, -(-total // CONTACTS_PER_PAGE))
        try:
            page = min(max(1, int(request.query_params.get("page") or 1)), pages)
        except ValueError:
            page = 1
        offset = (page - 1) * CONTACTS_PER_PAGE
        contacts = db.contacts(q, limit=CONTACTS_PER_PAGE, offset=offset)

        def page_url(n: int) -> str:
            params = {k: v for k, v in (("q", q), ("page", n if n > 1 else "")) if v}
            return prefix + ("?" + urllib.parse.urlencode(params) if params else "")
        return show(request, "crm_contacts.html", {
            "view": "contacts", "q": q, "contacts": contacts, "total": total,
            "page": page, "pages": pages, "first": offset + 1, "last": offset + len(contacts),
            "prev_url": page_url(page - 1) if page > 1 else None,
            "next_url": page_url(page + 1) if page < pages else None,
            "imported": None if error else import_result(request),
            "max_mb": IMPORT_MAX_BYTES // (1024 * 1024),
            "error": error, "form": form or {}}, status_code)

    @router.get("")
    @guarded
    def index(request: Request):
        try:
            sweep_stash(create=False)
        except RuntimeError:
            pass
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

    # ---- import from a contacts export (chassis/scripts/crm_import.py)

    def sweep_stash(create: bool = True) -> Path | None:
        if not create and not stash_root.is_dir():
            return None
        root = _private_dir(stash_root)
        cutoff = time.time() - IMPORT_TTL_SECONDS
        for f in root.iterdir():
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except FileNotFoundError:
                pass
        return root

    def stashed(token: str) -> Path | None:
        if not TOKEN_RE.fullmatch(token or ""):
            return None
        root = sweep_stash()
        return next((root / f"{token}{s}" for s in IMPORT_SUFFIXES if (root / f"{token}{s}").is_file()), None)

    async def read_upload(request: Request) -> tuple[str, bytes]:
        try:
            length = int(request.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        too_big = ImportRefused(f"That file is larger than {IMPORT_MAX_BYTES // (1024 * 1024)} MB. "
                                "Export only the contacts file.", 413)
        if length > IMPORT_MAX_BYTES + MAX_FORM_BYTES:
            raise too_big
        if not _multipart_installed():
            raise ImportRefused("File upload needs the python-multipart package on the dashboard host: "
                                "python3 -m pip install -r chassis/dashboard/requirements.txt, then restart "
                                "the dashboard.", 503)
        form = await request.form(max_files=1, max_fields=4)
        upload = form.get("file")
        if upload is None or isinstance(upload, str) or not upload.filename:
            raise ImportRefused("Choose a file to import.")
        name = Path(upload.filename.replace("\\", "/")).name
        suffix = Path(name).suffix.lower()
        if suffix == ".zip":
            raise ImportRefused(f"{name} is a whole archive. Unzip it and upload only the contacts file "
                                "(for LinkedIn: Connections.csv). Archives also hold messages, which this "
                                "never opens.")
        if suffix not in IMPORT_SUFFIXES:
            raise ImportRefused(f"{name} is not a .csv, .vcf or .json file. Upload {ACCEPTED}.")
        data = await upload.read(IMPORT_MAX_BYTES + 1)
        await form.close()
        if len(data) > IMPORT_MAX_BYTES:
            raise too_big
        return name, data

    def parse(name: str, data: bytes) -> tuple[str, list]:
        try:
            return ci.load_bytes(name, data)
        except ci.ExportFileError as e:
            text = str(e)
            if text.endswith("; pass --format"):
                text = text.removesuffix("; pass --format") + f". Upload {ACCEPTED}."
            raise ImportRefused(text) from None

    @router.post("/import")
    @guarded
    async def import_preview(request: Request):
        refuse_foreign(request)
        try:
            name, data = await read_upload(request)
            fmt, records = await run_in_threadpool(parse, name, data)
        except ImportRefused as e:
            return contacts_page(request, e.status_code, str(e))
        new_names: list[str] = []
        counts = await run_in_threadpool(ci.run, db, records, fmt, False, None, new_names)
        token = secrets.token_urlsafe(24)
        path = sweep_stash() / f"{token}{Path(name).suffix.lower()}"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return show(request, "crm_import.html", {
            "view": "contacts", "filename": name, "format_label": FORMAT_LABELS[fmt], "token": token,
            "counts": counts, "skipped": counts["skipped_no_name"] + counts["skipped_invalid"],
            "sample": new_names[:IMPORT_SAMPLE], "more": max(0, len(new_names) - IMPORT_SAMPLE),
            "ttl_minutes": IMPORT_TTL_SECONDS // 60})

    def claim(token: str) -> tuple[bytes, str] | None:
        """Read and delete in one go, so a double submit applies once."""
        path = stashed(token)
        if path is None:
            return None
        try:
            data = path.read_bytes()
            path.unlink()
        except FileNotFoundError:
            return None
        return data, path.suffix

    @router.post("/import/confirm")
    @guarded
    async def import_confirm(request: Request):
        refuse_foreign(request)
        form = await _form(request)
        got = claim(form.get("token") or "")
        if got is None:
            return contacts_page(request, 400, "That import preview has expired or was already applied. "
                                               "Upload the file again.")
        data, suffix = got
        try:
            fmt, records = await run_in_threadpool(parse, f"upload{suffix}", data)
        except ImportRefused as e:
            return contacts_page(request, e.status_code, str(e))
        counts = await run_in_threadpool(ci.run, db, records, fmt, True)
        skipped = counts["skipped_no_name"] + counts["skipped_invalid"]
        return go("?" + urllib.parse.urlencode({
            "imported": fmt, "new": counts["new"], "enriched": counts["enriched"],
            "unchanged": counts["unchanged"], "skipped": skipped}))

    @router.post("/import/cancel")
    @guarded
    async def import_cancel(request: Request):
        refuse_foreign(request)
        claim((await _form(request)).get("token") or "")
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
