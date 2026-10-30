"""Projections tab: spend to date, projected and baseline for each paid service.

Reads $CUSTOMER_HOME/state/projections/spend.db (read-only), written by
chassis/scripts/gather-spend-snapshots.py (the spend-snapshots heartbeat).
The math lives in chassis/scripts/_spend.py so the page and the alert never
disagree. The tab is only mounted when config/spend-services.json lists at
least one service.
"""
from __future__ import annotations

import calendar
import sqlite3
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request

import _spend
from tabs import page

router = APIRouter()

STALE_AFTER = timedelta(hours=13)

# Categorical dark steps, validated against the page surface #0f0f10 (CVD and
# normal-vision separation, 3:1 contrast). Assigned in config order so a
# service keeps its colour whether or not the others have data; a ninth
# service folds to grey rather than getting a generated hue.
PALETTE = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]
OTHER_COLOR = "#6b7280"


def _age(iso: str | None, now: datetime) -> str | None:
    if not iso:
        return None
    delta = now - datetime.fromisoformat(iso.replace("Z", "+00:00"))
    hours = delta.total_seconds() / 3600
    return f"{hours:.0f} h" if hours < 48 else f"{hours / 24:.0f} d"


def _pill(s: _spend.Summary, now: datetime) -> tuple[str, str]:
    if s.status == "needs key":
        return "needs key", "dim"
    if s.status == "error" and not s.has_data:
        return "error", "bad"
    if not s.has_data:
        return "no data", "dim"
    if s.level == "capped":
        return "over cap", "bad"
    if s.level == "warned":
        return "above path", "warn"
    if s.status == "error" or (
        s.last_ok_at and now - datetime.fromisoformat(s.last_ok_at.replace("Z", "+00:00")) > STALE_AFTER
    ):
        return f"stale {_age(s.last_ok_at, now)}", "warn"
    if s.kind == "fixed":
        return "fixed", "dim"
    return "ok", "ok"


def _chart(s: _spend.Summary) -> dict | None:
    if not s.daily:
        return None
    base_per_day = (s.baseline / s.period.days_total) if s.baseline else None
    top = max(max(s.daily), base_per_day or 0) or 1.0
    days = [
        {
            "label": (s.period.start + timedelta(days=i)).strftime("%b %-d"),
            "cost": c,
            "pct": round(100 * c / top, 1),
        }
        for i, c in enumerate(s.daily)
    ]
    last = s.period.start + timedelta(days=len(s.daily) - 1)
    return {
        "days": days,
        "base_pct": round(100 * base_per_day / top, 1) if base_per_day else None,
        "base_per_day": base_per_day,
        "range": f"{s.period.start.strftime('%b %-d')} to {last.strftime('%b %-d')}",
    }


def build_stacked(series: list[dict], month_start: date, through: date,
                  base_per_day: float | None = None) -> dict | None:
    """Daily usage for the calendar month, one bar per day stacked by service.

    `series` holds {service, label, color, by_day: {iso day: cost}} in stack
    order, bottom first. Days after `through` are empty slots so the bar row
    reads as the whole month. Segment pct is a share of the chart height, so
    the segments of one day add up to that day's bar.
    """
    if not series:
        return None
    n = calendar.monthrange(month_start.year, month_start.month)[1]
    days = []
    for i in range(n):
        d = month_start + timedelta(days=i)
        label = d.strftime("%b %-d")
        if d > through:
            days.append({"label": label, "future": True, "total": 0.0, "segments": []})
            continue
        segs = [
            {"service": x["service"], "label": x["label"], "color": x["color"], "cost": x["by_day"].get(d.isoformat(), 0.0)}
            for x in series
        ]
        segs = [g for g in segs if g["cost"] > 0]
        days.append({"label": label, "future": False, "total": sum(g["cost"] for g in segs), "segments": segs})
    top = max([d["total"] for d in days] + [base_per_day or 0]) or 1.0
    for d in days:
        for g in d["segments"]:
            g["pct"] = round(100 * g["cost"] / top, 2)
    last = min(through, month_start + timedelta(days=n - 1))
    return {
        "days": days,
        "top": max(d["total"] for d in days),
        "base_per_day": base_per_day,
        "base_pct": round(100 * base_per_day / top, 1) if base_per_day else None,
        "range": f"{month_start.strftime('%b %-d')} to {last.strftime('%b %-d')}" if last >= month_start else None,
    }


def _usage_base_per_day(s: _spend.Summary) -> float | None:
    if s.baseline is None:
        return None
    return max(s.baseline - s.fee, 0.0) / s.period.days_total


def _fixed_lines(rows: list[dict]) -> list[dict]:
    """Plan fees kept out of the daily bars, listed under the chart instead."""
    out = []
    for r in rows:
        s = r["s"]
        if not s.has_data:
            continue
        amount = s.fee if s.kind == "api" else (s.to_date or 0)
        if amount > 0:
            out.append({"label": s.label + (" plan" if s.kind == "api" else ""), "amount": amount, "color": r["color"]})
    return out


def load_services(now: datetime) -> tuple[list[dict], dict, bool]:
    cfg = _spend.load_config()
    if not _spend.DB_PATH.exists():
        return [], {}, False
    today = now.date()
    month_start = today.replace(day=1)
    yesterday = today - timedelta(days=1)
    conn = sqlite3.connect(f"file:{_spend.DB_PATH}?mode=ro", uri=True)
    try:
        rows, series = [], []
        for i, (name, c) in enumerate(cfg.items()):
            s = _spend.summarize(conn, name, c, now)
            pill, tone = _pill(s, now)
            color = PALETTE[i] if i < len(PALETTE) else OTHER_COLOR
            rows.append({"s": s, "pill": pill, "tone": tone, "age": _age(s.last_ok_at, now),
                         "chart": _chart(s), "color": color})
            if s.kind == "api" and s.has_data:
                series.append({"service": name, "label": s.label, "color": color,
                               "by_day": _spend.rows_by_day(conn, name, month_start, today, usage_only=True)})
        fired = conn.execute(
            "SELECT service, level, projected, fired_at FROM spend_alerts ORDER BY fired_at DESC LIMIT 10"
        ).fetchall()
    finally:
        conn.close()
    with_data = [r["s"] for r in rows if r["s"].has_data]
    bases = [b for b in (_usage_base_per_day(s) for s in with_data if s.kind == "api") if b is not None]
    fixed = _fixed_lines(rows)
    totals = {
        "to_date": sum(s.to_date or 0 for s in with_data),
        "projected": sum(s.projected or 0 for s in with_data),
        "baseline": sum(s.baseline or 0 for s in with_data),
        "fired": fired,
        "stacked": build_stacked(series, month_start, yesterday, sum(bases) if bases else None),
        "fixed": fixed,
        "fixed_total": sum(f["amount"] for f in fixed),
    }
    return rows, totals, True


def _chips(rows: list[dict], totals: dict, single: str | None) -> list[dict]:
    chips = [{"href": "/projections", "label": "All", "color": None, "to_date": totals.get("to_date"),
              "pill": None, "tone": None, "selected": single is None}]
    for r in rows:
        s = r["s"]
        chips.append({"href": f"/projections/{s.service}", "label": s.label, "color": r["color"],
                      "to_date": s.to_date if s.has_data else None, "pill": r["pill"], "tone": r["tone"],
                      "selected": s.service == single})
    return chips


@router.get("")
def projections_page(request: Request):
    now = datetime.now(timezone.utc)
    rows, totals, have_db = load_services(now)
    return page(request, "projections.html", {
        "rows": rows, "totals": totals, "have_db": have_db, "chips": _chips(rows, totals, None),
        "checked_at": now.strftime("%Y-%m-%d %H:%M UTC"), "month": now.strftime("%B %Y"), "single": None,
    })


@router.get("/{service}")
def service_page(request: Request, service: str):
    now = datetime.now(timezone.utc)
    rows, totals, have_db = load_services(now)
    match = [r for r in rows if r["s"].service == service]
    if have_db and not match:
        raise HTTPException(status_code=404, detail="unknown service")
    return page(request, "projections.html", {
        "rows": match, "totals": totals, "have_db": have_db, "chips": _chips(rows, totals, service),
        "focus": match[0] if match else None,
        "checked_at": now.strftime("%Y-%m-%d %H:%M UTC"), "month": now.strftime("%B %Y"), "single": service,
    })
