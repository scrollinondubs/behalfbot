"""Briefings tab: lists rendered briefing HTML and shows one in an iframe.

Files come from $CUSTOMER_HOME/briefings (BRIEFINGS_ROOT overrides). The
raw files are served at /briefings/files/<name> for the iframe, and at
/<name> and /briefings/<name> so links the old static briefing-server handed
out keep resolving.
"""
from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from tabs import current_registry, customer_home, page

router = APIRouter()

_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.+)\.html$")


def briefings_dir() -> Path:
    override = os.environ.get("BRIEFINGS_ROOT", "").strip()
    return Path(override) if override else customer_home() / "briefings"


def list_briefings() -> list[dict]:
    out = []
    for p in sorted(briefings_dir().glob("*.html"), reverse=True):
        m = _DATE_RE.match(p.name)
        if not m:
            continue
        out.append({
            "date": m.group(1),
            "slug": m.group(2),
            "label": m.group(2).replace("-", " ").title(),
            "filename": p.name,
        })
    return out


def pick(all_b: list[dict], wanted: str | None) -> dict | None:
    if wanted:
        return next((b for b in all_b if b["date"] == wanted or b["filename"] == wanted), None)
    today = date.today().isoformat()
    for b in all_b:
        if b["date"] == today and b["slug"] == "morning-briefing":
            return b
    for b in all_b:
        if b["date"] == today:
            return b
    return all_b[0] if all_b else None


def briefing_file(rel: str) -> Path:
    """A file under the briefings dir, or 404. Refuses anything that resolves
    outside it."""
    base = briefings_dir().resolve()
    target = (base / rel).resolve()
    if not target.is_relative_to(base) or not target.is_file():
        raise HTTPException(status_code=404)
    return target


@router.get("")
def briefings_index(request: Request, date: Optional[str] = None):  # Optional: FastAPI evaluates this on 3.9
    all_b = list_briefings()
    return page(request, "briefings.html", {"briefings": all_b, "selected": pick(all_b, date)})


@router.get("/files/{rel:path}")
def briefing_raw(rel: str):
    return FileResponse(briefing_file(rel))


@router.get("/{rel:path}")
def briefing_legacy(rel: str):
    if not rel:
        return RedirectResponse(url="/briefings")
    return FileResponse(briefing_file(rel))


legacy_root = APIRouter()


@legacy_root.get("/{rel:path}", include_in_schema=False)
def briefing_at_root(rel: str):
    # The pre-dashboard server served the briefings dir at /. Registered last,
    # so it only sees paths no tab claimed. Being a catch-all it also swallows
    # the trailing-slash redirect for a tab whose router serves "/", so do
    # that redirect here.
    try:
        return FileResponse(briefing_file(rel))
    except HTTPException:
        if "/" + rel.rstrip("/") in {t.prefix for t in current_registry().tabs} and not rel.endswith("/"):
            return RedirectResponse(url=f"/{rel}/")
        raise
