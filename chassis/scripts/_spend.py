"""Spend projections: billing periods, projection math, baselines and alert levels.

Shared by chassis/scripts/gather-spend-snapshots.py (writes
$CUSTOMER_HOME/state/projections/spend.db) and the dashboard's Projections tab
(reads it). Stdlib only, because the gather runs in the chassis container.

Services are configured in $CUSTOMER_HOME/config/spend-services.json, one
object per service keyed by a name of your choice. Keys starting with "_" are
not services ("_settings" holds the alert channel and dashboard URL).
"""
from __future__ import annotations

import calendar
import json
import os
import sqlite3
import statistics
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import customer_home  # noqa: E402

CONFIG_PATH = customer_home() / "config" / "spend-services.json"
DB_PATH = Path(os.environ.get("SPEND_DB", str(customer_home() / "state" / "projections" / "spend.db")))

WARN_RATIO = 1.5
WARN_FLOOR_USD = 10.0
BLEND_DAYS = 7
BASELINE_PERIODS = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS spend_daily (
  service      TEXT NOT NULL,
  day          TEXT NOT NULL,
  period_start TEXT NOT NULL,
  cost_usd     REAL NOT NULL,
  billed_usd   REAL,
  top_line     TEXT,
  source       TEXT NOT NULL CHECK (source IN ('api','fixed','estimate')),
  fetched_at   TEXT NOT NULL,
  PRIMARY KEY (service, day, source)
);
CREATE TABLE IF NOT EXISTS spend_alerts (
  service      TEXT NOT NULL,
  period_start TEXT NOT NULL,
  level        TEXT NOT NULL CHECK (level IN ('warned','capped')),
  projected    REAL NOT NULL,
  fired_at     TEXT NOT NULL,
  PRIMARY KEY (service, period_start, level)
);
CREATE TABLE IF NOT EXISTS spend_fetch (
  service      TEXT PRIMARY KEY,
  status       TEXT NOT NULL,
  last_ok_at   TEXT,
  last_error   TEXT
);
"""


def load_raw(path: Path | None = None) -> dict:
    return json.loads((path or CONFIG_PATH).read_text())


def load_config(path: Path | None = None) -> dict[str, dict]:
    raw = load_raw(path)
    return {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, dict)}


PROVIDER_KINDS = {"turso": "invoice", "resend": "resend", "fixed": "fixed"}


def provider_of(service: str, cfg: dict) -> str:
    """The billing API behind a service: `provider`, else the service's own key."""
    return str(cfg.get("provider") or service)


def kind_of(service: str, cfg: dict) -> str:
    """api (daily usage from a billing API), invoice, resend or fixed."""
    return str(cfg.get("kind") or PROVIDER_KINDS.get(provider_of(service, cfg), "api"))


def settings(path: Path | None = None) -> dict:
    s = load_raw(path).get("_settings")
    return s if isinstance(s, dict) else {}


def has_services(path: Path | None = None) -> bool:
    """True when the config exists and lists at least one service."""
    try:
        return bool(load_config(path))
    except (OSError, ValueError, AttributeError):
        return False


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    return conn


def today_in(tz: str, now: datetime | None = None) -> date:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(ZoneInfo(tz)).date()


def _anchored(year: int, month: int, anchor_day: int) -> date:
    return date(year, month, min(anchor_day, calendar.monthrange(year, month)[1]))


@dataclass(frozen=True)
class Period:
    start: date
    end: date  # exclusive

    @property
    def days_total(self) -> int:
        return (self.end - self.start).days

    @property
    def key(self) -> str:
        return self.start.isoformat()

    def label(self) -> str:
        if self.start.day == 1:
            return self.start.strftime("%B")
        last = self.end - timedelta(days=1)
        return f"{self.start.strftime('%b')} {self.start.day} to {last.strftime('%b')} {last.day}"


def period_for(day: date, anchor_day: int = 1) -> Period:
    """The monthly billing period that contains `day`, renewing on `anchor_day`."""
    start = _anchored(day.year, day.month, anchor_day)
    if start > day:
        y, m = (day.year, day.month - 1) if day.month > 1 else (day.year - 1, 12)
        start = _anchored(y, m, anchor_day)
    y, m = (start.year, start.month + 1) if start.month < 12 else (start.year + 1, 1)
    return Period(start, _anchored(y, m, anchor_day))


def previous_periods(period: Period, anchor_day: int, n: int) -> list[Period]:
    out, p = [], period
    for _ in range(n):
        p = period_for(p.start - timedelta(days=1), anchor_day)
        out.append(p)
    return out


def project(daily: list[float], period: Period, baseline: float | None) -> float:
    """Spend to date plus the recent daily rate for the days left.

    `daily` holds one cost per complete day from the period start, oldest first.
    For the first BLEND_DAYS days the rate is blended with the baseline rate,
    so one quiet (or one loud) day does not swing the projection on its own.
    """
    to_date = sum(daily)
    left = max(period.days_total - len(daily), 0)
    recent = daily[-BLEND_DAYS:]
    rate = sum(recent) / len(recent) if recent else 0.0
    if len(recent) < BLEND_DAYS and baseline is not None:
        w = len(recent) / BLEND_DAYS
        rate = w * rate + (1 - w) * (baseline / period.days_total)
    return to_date + rate * left


def median_baseline(full_period_totals: list[float]) -> tuple[float | None, int]:
    """Median of up to the last BASELINE_PERIODS full periods, newest first.

    Returns (baseline, number of periods it was taken from).
    """
    totals = full_period_totals[:BASELINE_PERIODS]
    if not totals:
        return None, 0
    return statistics.median(totals), len(totals)


def level_for(projected: float, baseline: float | None, cap: float | None) -> str | None:
    if cap is not None and projected > cap:
        return "capped"
    if baseline is not None and projected > WARN_RATIO * baseline and projected > baseline + WARN_FLOOR_USD:
        return "warned"
    return None


def rows_by_day(conn: sqlite3.Connection, service: str, start: date, end: date,
                usage_only: bool = False) -> dict[str, float]:
    sql = "SELECT day, SUM(cost_usd) FROM spend_daily WHERE service=? AND day>=? AND day<?"
    if usage_only:
        sql += " AND source != 'fixed'"
    cur = conn.execute(sql + " GROUP BY day", (service, start.isoformat(), end.isoformat()))
    return {d: c for d, c in cur.fetchall()}


def full_period_totals(conn: sqlite3.Connection, service: str, period: Period, anchor_day: int) -> list[float]:
    """Totals of earlier full periods that have any stored rows, newest first."""
    out = []
    for p in previous_periods(period, anchor_day, BASELINE_PERIODS):
        rows = rows_by_day(conn, service, p.start, p.end)
        if rows:
            out.append(sum(rows.values()))
    return out


def daily_series(conn: sqlite3.Connection, service: str, period: Period, through: date) -> list[float]:
    """Usage cost per day from the period start through `through`, zero-filled."""
    rows = rows_by_day(conn, service, period.start, min(through + timedelta(days=1), period.end), usage_only=True)
    n = (min(through, period.end - timedelta(days=1)) - period.start).days + 1
    return [rows.get((period.start + timedelta(days=i)).isoformat(), 0.0) for i in range(max(n, 0))]


@dataclass
class Summary:
    service: str
    label: str
    kind: str
    period: Period
    status: str
    to_date: float | None = None
    projected: float | None = None
    baseline: float | None = None
    baseline_n: int = 0
    cap: float | None = None
    level: str | None = None
    daily: list[float] | None = None
    fee: float = 0.0
    last_ok_at: str | None = None
    last_error: str | None = None
    note: str | None = None
    has_data: bool = False


def summarize(conn: sqlite3.Connection, service: str, cfg: dict, now: datetime | None = None) -> Summary:
    tz = cfg.get("tz", "UTC")
    anchor = int(cfg.get("anchor_day", 1))
    today = today_in(tz, now)
    period = period_for(today, anchor)
    fetch = conn.execute(
        "SELECT status, last_ok_at, last_error FROM spend_fetch WHERE service=?", (service,)
    ).fetchone()
    status, last_ok, last_err = fetch if fetch else ("not run", None, None)
    s = Summary(
        service=service, label=cfg.get("label", service), kind=kind_of(service, cfg),
        period=period, status=status, cap=cfg.get("cap"), last_ok_at=last_ok,
        last_error=last_err, note=cfg.get("note"),
    )
    has_rows = conn.execute("SELECT 1 FROM spend_daily WHERE service=? LIMIT 1", (service,)).fetchone()
    if not has_rows:
        return s
    s.has_data = True
    if s.kind == "api":
        totals = full_period_totals(conn, service, period, anchor)
        s.baseline, s.baseline_n = median_baseline(totals)
        yesterday = today - timedelta(days=1)
        s.daily = daily_series(conn, service, period, yesterday) if yesterday >= period.start else []
        # Plan fees (source 'fixed', e.g. Cloudflare Workers Paid) are booked
        # once per period. They sit outside the daily rate so a $5 fee on day 1
        # does not read as a $5/day run rate.
        fixed_fee = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM spend_daily WHERE service=? AND day>=? AND day<? AND source='fixed'",
            (service, period.start.isoformat(), period.end.isoformat()),
        ).fetchone()[0]
        s.fee = fixed_fee
        usage_baseline = None if s.baseline is None else max(s.baseline - fixed_fee, 0.0)
        s.to_date = sum(s.daily) + fixed_fee
        s.projected = project(s.daily, period, usage_baseline) + fixed_fee
    else:
        rows = rows_by_day(conn, service, period.start, period.end)
        s.to_date = sum(rows.values())
        s.projected = s.to_date
        fixed = cfg.get("fixed")
        s.baseline = float(fixed) if fixed is not None else None
        s.baseline_n = 0
        if s.kind == "invoice":
            # Invoiced services: the baseline is the median of earlier invoiced
            # periods; until there are any, the plan price stands in. A period
            # not invoiced yet projects the plan price.
            totals = full_period_totals(conn, service, period, anchor)
            if totals:
                s.baseline, s.baseline_n = median_baseline(totals)
            if not rows and fixed is not None:
                s.projected = float(fixed)
    s.level = level_for(s.projected, s.baseline, s.cap)
    return s
