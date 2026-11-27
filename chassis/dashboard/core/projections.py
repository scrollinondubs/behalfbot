"""Projections tab: spend to date, projected and baseline for each paid service.

Reads $CUSTOMER_HOME/state/projections/spend.db (read-only), written by
chassis/scripts/gather-spend-snapshots.py (the spend-snapshots heartbeat).
The math lives in chassis/scripts/_spend.py so the page and the alert never
disagree. The tab is only mounted when config/spend-services.json lists at
least one service.
"""
from __future__ import annotations

import calendar
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request

import _spend
from tabs import page

router = APIRouter()

STALE_AFTER = timedelta(hours=13)

# Outlier clip for the stacked chart: the y-axis tops out at CLIP_FACTOR times
# the 90th-percentile day (nearest rank, over days with any visible spend). A
# day above that is drawn at the cap with a break mark and its true total on
# top, so one top-up cannot flatten the rest of the month. Below ten such days
# the 90th percentile is the tallest day itself, so nothing clips.
CLIP_FACTOR = 3.0
CLIP_PCTL = 0.9

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


def _day_label(d: date | datetime) -> str:
    return d.strftime("%b %-d")


def _stamp(t: datetime) -> str:
    return f"{_day_label(t)} {t:%H:%M} UTC"


def credits_view(readings: list[tuple], now: datetime) -> dict:
    """The drill-down for a credit-billed service (Firecrawl): daily credits
    used this period from our own balance readings, and the pace from the
    latest one. `readings` are (read_at, period_start, period_end, plan,
    remaining) oldest first; none gives {"n": 0} and the page says so."""
    if not readings:
        return {"n": 0}
    read_at, pstart, pend, plan, remaining = readings[-1]
    at = _spend._ts(read_at)
    measured = _spend.credit_measured([(r[0], r[1], r[4]) for r in readings], pstart)
    p = _spend.credit_pace(plan, remaining, pstart, pend, at, measured)
    by_day, first = _spend.credit_daily(readings, pstart, pend)
    start, end = p["start"], p["end"]
    last_day = (end - timedelta(seconds=1)).date()
    today = now.astimezone(timezone.utc).date()
    plan_per_day = plan / p["total_days"]
    top = max(list(by_day.values()) + [plan_per_day, 1])
    days, d = [], start.date()
    while d <= last_day:
        iso = d.isoformat()
        state = "future" if d > today else ("none" if first is None or d < first else "data")
        credits = by_day.get(iso, 0) if state == "data" else None
        days.append({"label": _day_label(d), "credits": credits, "state": state, "today": d == today,
                     "pct": round(100 * credits / top, 1) if credits else 0})
        d += timedelta(days=1)
    return {
        "n": len(readings), "days": days, "plan": plan, "remaining": remaining,
        "used": p["used"], "per_day": p["per_day"], "projected": p["projected"], "basis": p["basis"],
        "over": p["shortfall"] > 0 or remaining <= 0, "shortfall": p["shortfall"],
        "runout": _day_label(p["runout"]) if p["runout"] else None,
        "plan_per_day": plan_per_day, "plan_pct": round(100 * plan_per_day / top, 1),
        "since": _stamp(_spend._ts(next(r[0] for r in readings if r[1] == pstart))),
        "read_at": _stamp(at),
        "range": f"{_day_label(start)} to {_day_label(last_day)}",
    }


def clip_cap(totals: list[float], floor: float = 0.0) -> float | None:
    """The robust y-axis top, or None when no day is tall enough to need clipping."""
    vals = sorted(t for t in totals if t > 0)
    if not vals:
        return None
    p = vals[math.ceil(CLIP_PCTL * len(vals)) - 1]
    cap = max(CLIP_FACTOR * p, floor)
    return cap if vals[-1] > cap else None


def build_stacked(series: list[dict], month_start: date, through: date,
                  base_per_day: float | None = None) -> dict | None:
    """Daily usage for the calendar month, one bar per day stacked by service.

    `series` holds {service, label, color, by_day: {iso day: cost}, prepaid?}
    in stack order, bottom first. Days after `through` are empty slots so the
    bar row reads as the whole month. Segment pct is a share of the chart
    height, so the segments of one day add up to that day's bar. A day over
    the clip cap keeps its other segments at true scale and shrinks only its
    largest one to fit; that segment carries the break mark.
    """
    if not series:
        return None
    n = calendar.monthrange(month_start.year, month_start.month)[1]
    days = []
    for i in range(n):
        d = month_start + timedelta(days=i)
        label = d.strftime("%b %-d")
        if d > through:
            days.append({"label": label, "future": True, "total": 0.0, "segments": [], "clipped": False})
            continue
        segs = [
            {"service": x["service"], "label": x["label"], "color": x["color"],
             "prepaid": bool(x.get("prepaid")), "cost": x["by_day"].get(d.isoformat(), 0.0)}
            for x in series
        ]
        segs = [g for g in segs if g["cost"] > 0]
        days.append({"label": label, "future": False, "total": sum(g["cost"] for g in segs),
                     "segments": segs, "clipped": False})
    cap = clip_cap([d["total"] for d in days], base_per_day or 0.0)
    top = cap or max([d["total"] for d in days] + [base_per_day or 0]) or 1.0
    for d in days:
        for g in d["segments"]:
            g["pct"] = round(100 * g["cost"] / top, 2)
            g["broken"] = False
        if cap and d["total"] > cap:
            d["clipped"] = True
            big = max(d["segments"], key=lambda g: g["cost"])
            rest = d["total"] - big["cost"]
            if rest < cap:
                big["pct"] = round(100 * (cap - rest) / cap, 2)
            else:
                for g in d["segments"]:
                    g["pct"] = round(100 * g["cost"] / d["total"], 2)
            big["broken"] = True
    last = min(through, month_start + timedelta(days=n - 1))
    return {
        "days": days,
        "top": max(d["total"] for d in days),
        "cap": cap,
        "has_prepaid": any(g["prepaid"] for d in days for g in d["segments"]),
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


def load_services(now: datetime, hidden: frozenset[str] = frozenset()) -> tuple[list[dict], dict, bool]:
    """`hidden` drops services from the stacked chart and its baseline line only;
    the tiles, chips and tables still count them."""
    cfg = _spend.load_config()
    if not _spend.DB_PATH.exists():
        return [], {}, False
    today = now.date()
    month_start = today.replace(day=1)
    yesterday = today - timedelta(days=1)
    conn = sqlite3.connect(f"file:{_spend.DB_PATH}?mode=ro", uri=True)
    try:
        readings = _spend.credit_readings(conn)
        rows, series = [], []
        for i, (name, c) in enumerate(cfg.items()):
            s = _spend.summarize(conn, name, c, now)
            pill, tone = _pill(s, now)
            color = PALETTE[i] if i < len(PALETTE) else OTHER_COLOR
            credits = credits_view(readings.get(name, []), now) if _spend.is_credits(c) or name in readings else None
            rows.append({"s": s, "pill": pill, "tone": tone, "age": _age(s.last_ok_at, now),
                         "chart": _chart(s), "color": color, "prepaid": c.get("fetch") == "receipts",
                         "credits": credits})
            if s.kind == "api" and s.has_data:
                series.append({"service": name, "label": s.label, "color": color,
                               "prepaid": c.get("fetch") == "receipts",
                               "base": _usage_base_per_day(s),
                               "by_day": _spend.rows_by_day(conn, name, month_start, today, usage_only=True)})
        fired = conn.execute(
            "SELECT service, level, projected, fired_at FROM spend_alerts ORDER BY fired_at DESC LIMIT 10"
        ).fetchall()
    finally:
        conn.close()
    with_data = [r["s"] for r in rows if r["s"].has_data]
    shown = [x for x in series if x["service"] not in hidden]
    bases = [x["base"] for x in shown if x["base"] is not None]
    fixed = _fixed_lines(rows)
    totals = {
        "to_date": sum(s.to_date or 0 for s in with_data),
        "projected": sum(s.projected or 0 for s in with_data),
        "baseline": sum(s.baseline or 0 for s in with_data),
        "fired": fired,
        "stacked": build_stacked(shown, month_start, yesterday, sum(bases) if bases else None),
        "toggleable": [x["service"] for x in series],
        "hidden": [x["service"] for x in series if x["service"] in hidden],
        "fixed": fixed,
        "fixed_total": sum(f["amount"] for f in fixed),
    }
    return rows, totals, True


def _hide_href(hidden: set[str]) -> str:
    return "/projections?hide=" + ",".join(sorted(hidden))


def _chips(rows: list[dict], totals: dict, single: str | None) -> list[dict]:
    hidden = set(totals.get("hidden", []))
    toggleable = set(totals.get("toggleable", [])) if single is None else set()
    chips = [{"href": "/projections", "label": "All", "color": None, "to_date": totals.get("to_date"),
              "pill": None, "tone": None, "selected": single is None, "toggle": None, "hidden": False}]
    for r in rows:
        s = r["s"]
        off = s.service in hidden
        toggle = None
        if s.service in toggleable:
            toggle = {"href": _hide_href(hidden - {s.service} if off else hidden | {s.service}),
                      "action": "Show" if off else "Hide"}
        chips.append({"href": f"/projections/{s.service}", "label": s.label, "color": r["color"],
                      "to_date": s.to_date if s.has_data else None, "pill": r["pill"], "tone": r["tone"],
                      "selected": s.service == single, "toggle": toggle, "hidden": off})
    return chips


@router.get("")
def projections_page(request: Request, hide: str | None = None):
    """`hide` is a comma list of services left out of the stacked chart. The
    page mirrors it to localStorage and restores it from there when absent."""
    now = datetime.now(timezone.utc)
    hidden = frozenset(h for h in (hide or "").split(",") if h)
    rows, totals, have_db = load_services(now, hidden)
    return page(request, "projections.html", {
        "rows": rows, "totals": totals, "have_db": have_db, "chips": _chips(rows, totals, None),
        "hide_param": hide is not None,
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
