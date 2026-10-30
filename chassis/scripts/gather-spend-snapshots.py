#!/usr/bin/env python3
"""Spend snapshots: pull daily spend from each billing API into spend.db,
then post an alert when a projection leaves its normal path.

Runs as the `spend-snapshots` heartbeat every 6 hours. All vendor calls are
GETs. Progress lines go to stderr (the dispatcher log); stdout carries only
the JSON verdict, which always has count 0 so the dispatcher never invokes a
model - the alert is posted from here, through discord-post.sh.

Services come from $CUSTOMER_HOME/config/spend-services.json; see
chassis/docs/dashboard.md for the providers and their fields. A service with
no key in the environment is recorded as 'needs key' and skipped. A fetch
that fails keeps the last good rows; the page shows their age.

Env:
  DRY_RUN=1   do every GET and compute alerts, but print them instead of
              posting to Discord and do not record them as fired.
  SPEND_DB    override $CUSTOMER_HOME/state/projections/spend.db.
  SPEND_ALERT_CHANNEL_ID, DASHBOARD_URL
              fallbacks for _settings.alert_channel_id / _settings.dashboard_url.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _spend  # noqa: E402

HTTP_TIMEOUT_S = 90
USER_AGENT = "behalfbot-spend-snapshots/1.0 (+https://github.com/scrollinondubs/behalfbot)"
DISCORD_POST = Path(__file__).resolve().parent / "discord-post.sh"
# Vendors revise recent days, so each run re-reads a short tail.
REFRESH_TAIL_DAYS = 3
VERCEL_CHUNK_DAYS = 8


class MissingKey(Exception):
    pass


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def http_get(url: str, headers: dict[str, str], attempts: int = 3) -> bytes:
    # Vercel's billing export usually answers in seconds but sometimes stalls a
    # request outright; a fresh request goes through.
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": USER_AGENT, **headers})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429 or attempt == attempts - 1:
                raise
        except (TimeoutError, urllib.error.URLError):
            if attempt == attempts - 1:
                raise
        time.sleep(5 * (attempt + 1))
    raise AssertionError("unreachable")


def get_json(url: str, headers: dict[str, str]):
    return json.loads(http_get(url, headers))


def key_for(cfg: dict) -> str:
    val = os.environ.get(cfg["env"], "").strip()
    if not val:
        raise MissingKey(cfg["env"])
    return val


def utc_midnight(d: date, tz: str) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)


def iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def add(days: dict, day: str, cost: float, billed: float | None, line: str | None, line_cost: float) -> None:
    d = days.setdefault(day, {"cost": 0.0, "billed": None, "lines": {}})
    d["cost"] += cost
    if billed is not None:
        d["billed"] = (d["billed"] or 0.0) + billed
    if line:
        d["lines"][line] = d["lines"].get(line, 0.0) + line_cost


# ---- fetchers: (cfg, start, end) -> {day: {cost, billed, lines}} -------------
# start inclusive, end exclusive, both dates in the service's billing tz.

def fetch_vercel(cfg: dict, start: date, end: date) -> dict:
    h = {"Authorization": "Bearer " + key_for(cfg)}
    teams = get_json("https://api.vercel.com/v2/teams", h).get("teams", [])
    team = next((t for t in teams if t.get("slug") == cfg.get("team_slug")), teams[0] if teams else None)
    if not team:
        raise RuntimeError("no Vercel team visible to the token")
    tz = ZoneInfo(cfg["tz"])
    days: dict = {}
    chunk = start
    while chunk < end:
        chunk_end = min(chunk + timedelta(days=VERCEL_CHUNK_DAYS), end)
        q = urllib.parse.urlencode({
            "teamId": team["id"],
            "from": iso_z(utc_midnight(chunk, cfg["tz"])),
            "to": iso_z(utc_midnight(chunk_end, cfg["tz"])),
        })
        body = http_get(f"https://api.vercel.com/v1/billing/charges?{q}", h).decode()
        for line in body.splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            ts = datetime.fromisoformat(r["ChargePeriodStart"].replace("Z", "+00:00"))
            day = ts.astimezone(tz).date().isoformat()
            eff = float(r.get("EffectiveCost") or 0)
            tags = r.get("Tags") or {}
            proj = tags.get("ProjectName") or tags.get("projectName") or tags.get("project")
            name = r.get("ServiceName") or "?"
            add(days, day, eff, float(r.get("BilledCost") or 0), f"{name}, project {proj}" if proj else name, eff)
        chunk = chunk_end
    return days


def fetch_openai(cfg: dict, start: date, end: date) -> dict:
    h = {"Authorization": "Bearer " + key_for(cfg)}
    days: dict = {}
    page = None
    while True:
        params = {
            "start_time": int(utc_midnight(start, "UTC").timestamp()),
            "end_time": int(utc_midnight(end, "UTC").timestamp()),
            "bucket_width": "1d", "limit": 31, "group_by": "line_item",
        }
        if page:
            params["page"] = page
        o = get_json("https://api.openai.com/v1/organization/costs?" + urllib.parse.urlencode(params), h)
        for b in o.get("data", []):
            day = datetime.fromtimestamp(b["start_time"], timezone.utc).date().isoformat()
            days.setdefault(day, {"cost": 0.0, "billed": None, "lines": {}})
            for r in b.get("results", []):
                v = float((r.get("amount") or {}).get("value") or 0)
                add(days, day, v, None, r.get("line_item"), v)
        if not o.get("has_more") or not o.get("next_page"):
            return days
        page = o["next_page"]


def fetch_anthropic(cfg: dict, start: date, end: date) -> dict:
    h = {"x-api-key": key_for(cfg), "anthropic-version": "2023-06-01"}
    days: dict = {}
    page = None
    while True:
        params = [
            ("starting_at", utc_midnight(start, "UTC").strftime("%Y-%m-%dT%H:%M:%SZ")),
            ("ending_at", utc_midnight(end, "UTC").strftime("%Y-%m-%dT%H:%M:%SZ")),
            ("bucket_width", "1d"), ("limit", 31), ("group_by[]", "description"),
        ]
        if page:
            params.append(("page", page))
        try:
            o = get_json("https://api.anthropic.com/v1/organizations/cost_report?" + urllib.parse.urlencode(params), h)
        except urllib.error.HTTPError as e:
            # A workspace-scoped key is a key, but not one that can read cost.
            if e.code in (401, 403):
                why = _scrub(e.read().decode(errors="replace"))[:120]
                raise MissingKey(f"{cfg['env']} with org admin scope ({e.code}: {why})") from None
            raise
        for b in o.get("data", []):
            day = b["starting_at"][:10]
            days.setdefault(day, {"cost": 0.0, "billed": None, "lines": {}})
            for r in b.get("results", []):
                usd = float(r.get("amount") or 0) / 100.0  # amount is cents, as a decimal string
                add(days, day, usd, None, r.get("description") or r.get("model"), usd)
        if not o.get("has_more") or not o.get("next_page"):
            return days
        page = o["next_page"]


def fetch_github(cfg: dict, start: date, end: date) -> dict:
    h = {
        "Authorization": "Bearer " + key_for(cfg),
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    days: dict = {}
    months = sorted({(d.year, d.month) for d in (start + timedelta(days=i) for i in range((end - start).days))})
    for y, m in months:
        o = get_json(f"https://api.github.com/users/{cfg['user']}/settings/billing/usage?year={y}&month={m}", h)
        for it in o.get("usageItems", []):
            day = str(it.get("date", ""))[:10]
            if not (start.isoformat() <= day < end.isoformat()):
                continue
            net = float(it.get("netAmount") or 0)
            add(days, day, net, net, f"{it.get('product')} {it.get('sku')}".strip(), net)
    return days


def _cloudflare_account(h: dict) -> str:
    accts = get_json("https://api.cloudflare.com/client/v4/accounts", h).get("result", [])
    if len(accts) != 1:
        raise RuntimeError(f"expected 1 Cloudflare account, token sees {len(accts)}")
    return accts[0]["id"]


def fetch_cloudflare(cfg: dict, start: date, end: date) -> dict:
    h = {"Authorization": "Bearer " + key_for(cfg)}
    aid = _cloudflare_account(h)
    days: dict = {}
    # billable-usage rejects ranges longer than one billing period, and a
    # `from` after the period start returns no rows at all (seen 2026-10-26),
    # so always ask from the period start and filter here.
    p = _spend.period_for(start, int(cfg["anchor_day"]))
    while p.start < end:
        lo, hi = max(p.start, start), min(p.end, end)
        q = urllib.parse.urlencode({"from": p.start.isoformat(), "to": hi.isoformat()})
        o = get_json(f"https://api.cloudflare.com/client/v4/accounts/{aid}/billable-usage?{q}", h)
        for r in o.get("result", []):
            day = str(r.get("ChargePeriodStart", ""))[:10]
            if not (lo.isoformat() <= day < hi.isoformat()):
                continue
            billed = float(r.get("BilledCost") or 0)
            cost = float(r["EffectiveCost"]) if r.get("EffectiveCost") is not None else billed
            add(days, day, cost, billed, r.get("ServiceFamilyName") or r.get("ServiceName"), cost)
        p = _spend.period_for(p.end, int(cfg["anchor_day"]))
    return days


def cloudflare_plan_fees(cfg: dict) -> tuple[float, str]:
    h = {"Authorization": "Bearer " + key_for(cfg)}
    aid = _cloudflare_account(h)
    subs = get_json(f"https://api.cloudflare.com/client/v4/accounts/{aid}/subscriptions", h).get("result", [])
    paid = [s for s in subs if s.get("frequency") == "monthly" and float(s.get("price") or 0) > 0]
    total = sum(float(s["price"]) for s in paid)
    names = ", ".join(f"{s['rate_plan']['public_name']} ${float(s['price']):.2f}" for s in paid)
    return total, names or "no paid plans"


def fetch_turso(cfg: dict) -> tuple[str, dict[str, tuple[float, list[str]]]]:
    """Invoice totals per billing period, from the invoices endpoint.

    Turso bills the plan in advance and overages (only possible when the
    subscription has them on) on a later invoice, so invoices are the spend.
    Each invoice line is booked to the period that contains its start date.
    """
    h = {"Authorization": "Bearer " + key_for(cfg)}
    base = f"https://api.turso.tech/v1/organizations/{cfg['org']}"
    sub = get_json(base + "/subscription", h)["subscription"]
    note = f"{sub.get('plan')} plan, overages {'ON' if sub.get('overages') else 'off'}"
    tz = ZoneInfo(cfg["tz"])
    per: dict[str, tuple[float, list[str]]] = {}
    for inv in get_json(base + "/invoices", h).get("invoices", []):
        if inv.get("status") not in ("paid", "issued", "open", "draft"):
            continue
        for li in inv.get("line_items") or []:
            started = datetime.fromisoformat(li["start_date"]).astimezone(tz).date()
            key = _spend.period_for(started, int(cfg.get("anchor_day", 1))).key
            total, lines = per.get(key, (0.0, []))
            amt = float(li.get("amount") or 0)
            per[key] = (total + amt, lines + [f"{li.get('name')} ${amt:.2f}"])
    return note, per


def count_resend_since(cfg: dict, since: datetime) -> int:
    h = {"Authorization": "Bearer " + key_for(cfg)}
    n, after = 0, None
    while True:
        q = {"limit": 100}
        if after:
            q["after"] = after
        o = get_json("https://api.resend.com/emails?" + urllib.parse.urlencode(q), h)
        data = o.get("data", [])
        for e in data:
            created = datetime.fromisoformat(e["created_at"].replace(" ", "T").replace("+00", "+00:00"))
            if created < since:
                return n
            n += 1
        if not o.get("has_more") or not data:
            return n
        after = data[-1]["id"]
        time.sleep(0.6)  # Resend allows 2 requests per second


def resend_cost(sent: int, cfg: dict) -> float:
    over = max(0, sent - int(cfg["quota"]))
    return float(cfg["fixed"]) + math.ceil(over / 1000) * float(cfg["over_per_1000"])


API_FETCHERS = {
    "vercel": fetch_vercel,
    "openai": fetch_openai,
    "anthropic": fetch_anthropic,
    "github": fetch_github,
    "cloudflare": fetch_cloudflare,
}


# ---- storage -----------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def upsert_days(conn, service: str, cfg: dict, days: dict, source: str = "api") -> None:
    anchor = int(cfg.get("anchor_day", 1))
    for day, d in days.items():
        top = max(d["lines"].items(), key=lambda kv: kv[1]) if d["lines"] else None
        top_line = f"{top[0]} ${top[1]:.2f}" if top and top[1] > 0 else None
        period = _spend.period_for(date.fromisoformat(day), anchor)
        conn.execute(
            "INSERT OR REPLACE INTO spend_daily (service, day, period_start, cost_usd, billed_usd, top_line, source, fetched_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (service, day, period.key, round(d["cost"], 6), d["billed"], top_line, source, now_iso()),
        )


def mark(conn, service: str, status: str, error: str | None = None) -> None:
    if status == "ok":
        conn.execute(
            "INSERT INTO spend_fetch (service, status, last_ok_at, last_error) VALUES (?,?,?,NULL)"
            " ON CONFLICT(service) DO UPDATE SET status=excluded.status, last_ok_at=excluded.last_ok_at, last_error=NULL",
            (service, status, now_iso()),
        )
    else:
        conn.execute(
            "INSERT INTO spend_fetch (service, status, last_error) VALUES (?,?,?)"
            " ON CONFLICT(service) DO UPDATE SET status=excluded.status, last_error=excluded.last_error",
            (service, status, error),
        )


def fetch_window(conn, service: str, cfg: dict, today: date) -> tuple[date, date]:
    """First run backfills 3 full periods; later runs re-read a short tail."""
    anchor = int(cfg.get("anchor_day", 1))
    period = _spend.period_for(today, anchor)
    last = conn.execute(
        "SELECT MAX(day) FROM spend_daily WHERE service=? AND source='api'", (service,)
    ).fetchone()[0]
    if last is None or last < _spend.previous_periods(period, anchor, 1)[0].start.isoformat():
        start = _spend.previous_periods(period, anchor, _spend.BASELINE_PERIODS)[-1].start
    else:
        start = max(date.fromisoformat(last) - timedelta(days=REFRESH_TAIL_DAYS),
                    _spend.previous_periods(period, anchor, 1)[0].start)
    return start, today  # today's partial day is excluded


def zero_fill(days: dict, start: date, end: date) -> dict:
    """An API that answered for a range spent nothing on days it left out."""
    for i in range((end - start).days):
        days.setdefault((start + timedelta(days=i)).isoformat(), {"cost": 0.0, "billed": None, "lines": {}})
    return days


def collect(conn, services: dict, now: datetime) -> dict[str, str]:
    status: dict[str, str] = {}
    for name, cfg in services.items():
        kind = _spend.kind_of(name, cfg)
        provider = _spend.provider_of(name, cfg)
        tz = cfg.get("tz", "UTC")
        today = _spend.today_in(tz, now)
        period = _spend.period_for(today, int(cfg.get("anchor_day", 1)))
        try:
            if kind == "fixed":
                upsert_days(conn, name, cfg, {period.key: {"cost": float(cfg["fixed"]), "billed": None, "lines": {}}}, "fixed")
                mark(conn, name, "fixed")
                status[name] = "fixed"
                log(f"= {name:<11} fixed ${float(cfg['fixed']):.2f}")
                continue
            if kind == "invoice":
                note, per_period = fetch_turso(cfg)
                conn.execute("DELETE FROM spend_daily WHERE service=?", (name,))
                for key, (total, lines) in per_period.items():
                    conn.execute(
                        "INSERT INTO spend_daily (service, day, period_start, cost_usd, billed_usd, top_line, source, fetched_at)"
                        " VALUES (?,?,?,?,?,?,'fixed',?)",
                        (name, key, key, total, total, "; ".join(lines)[:200], now_iso()))
                mark(conn, name, "ok")
                status[name] = "ok"
                this = per_period.get(period.key, (0.0, []))[0]
                log(f"\u2713 {name:<11} {note}, {len(per_period)} invoiced period(s)   ${this:.2f}")
                continue
            if kind == "resend":
                since = utc_midnight(period.start, tz)
                sent = count_resend_since(cfg, since)
                cost = resend_cost(sent, cfg)
                line = f"{sent} sent, quota {int(cfg['quota']):,}"
                conn.execute("DELETE FROM spend_daily WHERE service=? AND day>=? AND day<?",
                             (name, period.start.isoformat(), period.end.isoformat()))
                conn.execute(
                    "INSERT INTO spend_daily (service, day, period_start, cost_usd, billed_usd, top_line, source, fetched_at)"
                    " VALUES (?,?,?,?,?,?,'estimate',?)",
                    (name, period.key, period.key, cost, None, line, now_iso()),
                )
                mark(conn, name, "ok")
                status[name] = "ok"
                log(f"✓ {name:<11} {line}   ${cost:.2f}")
                continue

            if provider not in API_FETCHERS:
                raise RuntimeError(f"unknown provider {provider!r}, expected one of {', '.join(sorted(API_FETCHERS))}")
            start, end = fetch_window(conn, name, cfg, today)
            days = zero_fill(API_FETCHERS[provider](cfg, start, end), start, end)
            upsert_days(conn, name, cfg, {d: v for d, v in days.items() if start.isoformat() <= d < end.isoformat()})
            if provider == "cloudflare":
                # The API only shows today's plans. Earlier periods with no fee
                # row yet are booked at today's fee, once, so the baseline is
                # not low by the plan price.
                fee, names = cloudflare_plan_fees(cfg)
                upsert_days(conn, name, cfg, {period.key: {"cost": fee, "billed": fee, "lines": {f"plans: {names}": fee}}}, "fixed")
                for p in _spend.previous_periods(period, int(cfg["anchor_day"]), _spend.BASELINE_PERIODS):
                    has_usage = conn.execute(
                        "SELECT 1 FROM spend_daily WHERE service=? AND day>=? AND day<? AND source='api'",
                        (name, p.start.isoformat(), p.end.isoformat())).fetchone()
                    if has_usage:
                        conn.execute(
                            "INSERT OR IGNORE INTO spend_daily (service, day, period_start, cost_usd, billed_usd, top_line, source, fetched_at)"
                            " VALUES (?,?,?,?,?,?,'fixed',?)",
                            (name, p.key, p.key, fee, fee, f"plans: {names} (booked at today's fee)", now_iso()))
            mark(conn, name, "ok")
            status[name] = "ok"
            s = _spend.summarize(conn, name, cfg, now)
            log(f"✓ {name:<11} {start} to {end - timedelta(days=1)}   to date ${s.to_date:.2f}")
        except MissingKey as e:
            mark(conn, name, "needs key", f"no {e}")
            status[name] = "needs key"
            log(f"- {name:<11} no {e}")
        except Exception as e:  # noqa: BLE001 - one vendor failing must not stop the rest
            msg = _scrub(f"{type(e).__name__}: {e}")[:200]
            mark(conn, name, "error", msg)
            status[name] = "error"
            log(f"x {name:<11} {msg}")
        finally:
            conn.commit()
    return status


# Env names whose values must never reach a log line or the verdict. main()
# adds every `env` named in spend-services.json.
SECRET_ENVS: set[str] = {"DISCORD_BOT_TOKEN"}


def _scrub(text: str) -> str:
    for name in SECRET_ENVS:
        v = os.environ.get(name)
        if v:
            text = text.replace(v, "***")
    return text


# ---- alerts ------------------------------------------------------------------

def alert_text(s: _spend.Summary, level: str, top_line: str | None, page_url: str | None = None) -> str:
    period = s.period.label()
    if level == "capped":
        lines = [f"**{s.label} will pass its ${s.cap:,.0f} cap**",
                 f"Projected ${s.projected:,.2f} for {period}. To date ${s.to_date:,.2f}."]
    else:
        vs = f"+{(s.projected / s.baseline - 1) * 100:.0f}%" if s.baseline else "new spend"
        lines = [f"**{s.label} spend is above its normal path**",
                 f"Projected ${s.projected:,.2f} for {period}. Baseline ${s.baseline:,.2f} ({vs})."]
    if top_line:
        lines.append(f"Top line yesterday: {top_line}.")
    if page_url:
        lines.append(f"{page_url.rstrip('/')}/{s.service}")
    return "\n".join(lines)


def pending_alerts(conn, services: dict, status: dict[str, str], now: datetime) -> list[tuple[_spend.Summary, str]]:
    """Crossings not yet recorded in spend_alerts. A capped alert supersedes warned."""
    out = []
    for name, cfg in services.items():
        if status.get(name) not in ("ok", "fixed"):
            continue
        s = _spend.summarize(conn, name, cfg, now)
        fired = {r[0] for r in conn.execute(
            "SELECT level FROM spend_alerts WHERE service=? AND period_start=?", (name, s.period.key))}
        if s.level in ("warned", "capped") and s.level not in fired and not (s.level == "warned" and "capped" in fired):
            out.append((s, s.level))
    return out


def record_alert(conn, s: _spend.Summary, level: str) -> bool:
    return record_alert_row(conn, s.service, s.period.key, level, s.projected)


def record_alert_row(conn, key: str, period_key: str, level: str, amount: float) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO spend_alerts (service, period_start, level, projected, fired_at) VALUES (?,?,?,?,?)",
        (key, period_key, level, round(amount, 2), now_iso()),
    )
    conn.commit()
    return cur.rowcount == 1


def top_line_yesterday(conn, s: _spend.Summary, now: datetime) -> str | None:
    if not s.daily:
        return None
    yday = s.period.start + timedelta(days=len(s.daily) - 1)
    row = conn.execute(
        "SELECT top_line FROM spend_daily WHERE service=? AND day=? AND source!='fixed'", (s.service, str(yday))
    ).fetchone()
    return row[0] if row and row[0] else None


def alert_target(cfg_settings: dict) -> tuple[str, str]:
    """(channel id, projections page URL) from _settings, else env."""
    channel = str(cfg_settings.get("alert_channel_id") or os.environ.get("SPEND_ALERT_CHANNEL_ID", "")).strip()
    url = str(cfg_settings.get("dashboard_url") or os.environ.get("DASHBOARD_URL", "")).strip().rstrip("/")
    if url and not url.endswith("/projections"):
        url += "/projections"
    return channel, url


def post_discord(channel: str, text: str) -> None:
    if not channel:
        raise RuntimeError("no alert channel: set _settings.alert_channel_id or SPEND_ALERT_CHANNEL_ID")
    r = subprocess.run(["bash", str(DISCORD_POST), channel, text], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"discord-post.sh exit {r.returncode}: {(r.stderr or r.stdout).strip()[-160:]}")


def check_alerts(conn, services: dict, status: dict[str, str], now: datetime, dry_run: bool,
                 poster=None, cfg_settings: dict | None = None) -> list[str]:
    channel, page_url = alert_target(cfg_settings or {})
    poster = poster or (lambda text: post_discord(channel, text))
    posted = []
    for s, level in pending_alerts(conn, services, status, now):
        text = alert_text(s, level, top_line_yesterday(conn, s, now), page_url)
        if dry_run:
            log(f"! {s.service:<11} {level} (DRY_RUN, not posted):\n{text}")
            posted.append(f"{s.service}:{level}:dry")
            continue
        try:
            poster(text)
        except Exception as e:  # noqa: BLE001
            log(f"x {s.service:<11} alert post failed: {_scrub(str(e))[:160]}")
            continue
        if record_alert(conn, s, level):
            log(f"! {s.service:<11} {level}: alert posted")
            posted.append(f"{s.service}:{level}")
    return posted


def main() -> int:
    dry_run = os.environ.get("DRY_RUN", "0") == "1"
    now = datetime.now(timezone.utc)
    try:
        services = _spend.load_config()
        cfg_settings = _spend.settings()
    except FileNotFoundError:
        print(json.dumps({"count": 0, "reason": f"no {_spend.CONFIG_PATH}, nothing to snapshot"}))
        return 0
    SECRET_ENVS.update(str(c["env"]) for c in services.values() if c.get("env"))
    conn = _spend.connect()
    status = collect(conn, services, now)
    alerts = check_alerts(conn, services, status, now, dry_run, cfg_settings=cfg_settings)
    n_api = sum(1 for v in status.values() if v == "ok")
    n_fixed = sum(1 for v in status.values() if v == "fixed")
    n_key = sum(1 for v in status.values() if v == "needs key")
    n_err = sum(1 for v in status.values() if v == "error")
    reason = f"{n_api} api, {n_fixed} fixed, {n_key} no key, {n_err} error, {len(alerts)} alert"
    print(json.dumps({
        "count": 0,
        "reason": reason,
        "services": {k: v if v in ("ok", "fixed") else f"unknown:{v}" for k, v in status.items()},
        "alerts": alerts,
        "dry_run": dry_run,
    }))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 - the dispatcher treats a non-zero exit as gather_failed
        print(json.dumps({"count": 0, "reason": f"unknown:gather_crashed:{_scrub(str(e))[:200]}"}))
        sys.exit(0)
