"""TripWires: short-term monitors held as data, run by one heartbeat.

A tripwire is a record in $CUSTOMER_HOME/state/tripwires/tripwires.json, not a
script or a HEARTBEATS.md entry. chassis/scripts/gather-tripwires.py runs the
due ones, chassis/scripts/tripwire.py and the dashboard tab add and cancel them.
See chassis/docs/tripwires.md.

Standard library only: this file is imported in the container (no fastapi) and
by host dashboards.

Fetched pages are untrusted. Nothing from a response body, header or reason
phrase is ever stored or sent; only our own fixed strings and a sha256 are.

The store is shared across a virtiofs bind mount, measured on Colima on
2026-11-03:
- flock does not cross the mount (each side acquired it while the other held
  it), so the lock is an O_CREAT|O_EXCL file, which held under contention.
- After a rename on the host the guest can see ENOENT on that path for over
  half a second. A missing store therefore never reads as "empty" once one has
  existed: the .created marker says so, and the read retries or raises.
"""
from __future__ import annotations

import contextlib
import hashlib
import html
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

SCRIPTS_DIR = Path(__file__).resolve().parent

ACTIVE, FIRED, EXPIRED, CANCELLED, ARCHIVED = "active", "fired", "expired", "cancelled", "archived"
CHECK_TYPES = {
    "text_appears": "text appears",
    "text_disappears": "text disappears",
    "page_changed": "page changed",
    "http_status": "HTTP status equals",
}

MIN_INTERVAL_S = 15 * 60  # the dispatcher ticks every 15 minutes
FAILURE_ALERT_AFTER = 3
FETCH_TIMEOUT_S = 20
MAX_BODY_BYTES = 5 * 1024 * 1024
RUN_BUDGET_S = 180  # the gather blocks the dispatcher tick; the rest stay due
LOCK_WAIT_S = 10
LOCK_STALE_S = 30
MISSING_RETRY_S = 10
HEARTBEAT_STALE_S = 45 * 60
MAX_NAME, MAX_WHY, MAX_URL, MAX_TEXT, MAX_MESSAGE = 120, 500, 500, 300, 1200
USER_AGENT = "Mozilla/5.0 (compatible; BehalfBot-TripWire/1.0)"

# A status that says the site refused us, not that the page is in some state.
REFUSED_STATUSES = {401, 403, 407, 429, 451}


class TripwireError(Exception):
    """A request that cannot be honoured, with a message fit to show the user."""


class StoreBusy(Exception):
    pass


class StoreUnavailable(Exception):
    """The store exists but cannot be read. Never to be treated as empty."""


class FetchError(Exception):
    """Could not get a response. The message is one of our fixed categories."""


@dataclass
class Fetched:
    status: int
    body: str


Fetch = Callable[[str, bool], Fetched]
Notify = Callable[[str], bool]


# ---- time ----------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.astimezone()


def parse_duration(value: str) -> int | None:
    m = re.fullmatch(r"\s*(\d+)\s*([mhd])\s*", value or "")
    return int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)] if m else None


def parse_interval(value: str) -> int:
    seconds = parse_duration(value)
    if seconds is None:
        raise TripwireError(f"interval must look like 15m, 1h or 2d, not {value!r}")
    if seconds < MIN_INTERVAL_S:
        raise TripwireError("interval must be at least 15m, the heartbeat tick")
    return seconds


def parse_expiry(value: str, now: datetime) -> datetime:
    """A duration from now, an ISO timestamp, or a date meaning the end of that
    day where this process runs. Pass an offset when the exact hour matters."""
    value = (value or "").strip()
    seconds = parse_duration(value)
    if seconds is not None:
        out = now + timedelta(seconds=seconds)
    else:
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                day = date.fromisoformat(value) + timedelta(days=1)
                out = datetime.combine(day, dtime.min).astimezone()
            else:
                out = parse_iso(value)
        except ValueError:
            raise TripwireError("expiry must be a duration (36h, 2d), a date or an ISO timestamp") from None
    if out <= now:
        raise TripwireError("expiry is already in the past")
    return out


# ---- store ---------------------------------------------------------------------

def default_store_dir() -> Path:
    from _paths import customer_home
    return customer_home() / "state" / "tripwires"


class Store:
    """One JSON file, replaced atomically, with writers serialised by a lock
    file. Readers take no lock. The lock is never held across a fetch or a send."""

    def __init__(self, directory: Path | str | None = None) -> None:
        self.dir = Path(directory) if directory else default_store_dir()
        self.path = self.dir / "tripwires.json"
        self.lock_path = self.dir / ".lock"
        self.marker = self.dir / ".created"
        self.heartbeat_path = self.dir / "heartbeat.json"

    def read(self) -> dict:
        deadline = time.monotonic() + MISSING_RETRY_S
        while True:
            try:
                raw = self.path.read_text(encoding="utf8")
            except FileNotFoundError:
                if not self.marker.exists():
                    return {}
                if time.monotonic() >= deadline:
                    raise StoreUnavailable("the store file is missing") from None
                time.sleep(0.05)
                continue
            except OSError as e:
                raise StoreUnavailable(f"cannot read the store: {type(e).__name__}") from None
            try:
                data = json.loads(raw)
                if not isinstance(data, dict) or not isinstance(data.get("tripwires"), dict):
                    raise ValueError
            except ValueError:
                raise StoreUnavailable("the store is not valid JSON") from None
            return data["tripwires"]

    @contextlib.contextmanager
    def _lock(self) -> Iterator[None]:
        self.dir.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                os.close(os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
                break
            except FileExistsError:
                self._break_stale_lock()
                if time.monotonic() >= deadline:
                    raise StoreBusy("the tripwire store is locked") from None
                time.sleep(0.02)
        try:
            yield
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.lock_path)

    def _break_stale_lock(self) -> None:
        with contextlib.suppress(OSError):
            if time.time() - self.lock_path.stat().st_mtime > LOCK_STALE_S:
                aside = self.dir / f".lock.stale.{secrets.token_hex(4)}"
                os.rename(self.lock_path, aside)
                os.unlink(aside)

    def _write(self, tripwires: dict) -> None:
        tmp = self.dir / f".tripwires.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        try:
            with open(tmp, "w", encoding="utf8") as f:
                json.dump({"version": 1, "tripwires": tripwires}, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)

    @contextlib.contextmanager
    def transaction(self, create: bool = False) -> Iterator[dict]:
        """Yield the current records; what the block leaves there is written.
        Only `create` may bring a store into being, so a runner that looked too
        early can never replace one it failed to see."""
        with self._lock():
            tripwires = self.read()
            if not tripwires and not self.marker.exists() and not create:
                yield {}
                return
            yield tripwires
            if create and not self.marker.exists():
                self.marker.touch()
            self._write(tripwires)

    def mark_run(self, now: datetime) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / f".heartbeat.{os.getpid()}.tmp"
        tmp.write_text(json.dumps({"last_run": iso(now)}), encoding="utf8")
        os.replace(tmp, self.heartbeat_path)

    def last_run(self) -> datetime | None:
        try:
            return parse_iso(json.loads(self.heartbeat_path.read_text(encoding="utf8"))["last_run"])
        except (OSError, ValueError, KeyError, TypeError):
            return None


# ---- mutations -----------------------------------------------------------------

def _clean(value: str | None, label: str, limit: int, required: bool = True) -> str:
    out = (value or "").strip()
    if required and not out:
        raise TripwireError(f"{label} is required")
    if len(out) > limit:
        raise TripwireError(f"{label} is longer than {limit} characters")
    return out


def validate_url(url: str | None) -> str:
    url = _clean(url, "url", MAX_URL)
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise TripwireError("url must be http or https")
    if parts.username or parts.password:
        raise TripwireError("url must not carry credentials")
    return url


def build_check(check: str | None, url: str | None, text: str | None = None,
                status: str | int | None = None, allow_private: bool = False) -> dict:
    if check not in CHECK_TYPES:
        raise TripwireError(f"check must be one of {', '.join(CHECK_TYPES)}")
    params: dict = {"url": validate_url(url)}
    if allow_private:
        params["allow_private"] = True
    if check in ("text_appears", "text_disappears"):
        params["text"] = _clean(text, "text", MAX_TEXT)
    if check == "http_status":
        try:
            params["status"] = int(status)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise TripwireError("status must be a number such as 200") from None
        if not 100 <= params["status"] <= 599:
            raise TripwireError("status must be between 100 and 599")
    return {"check": check, "params": params}


def add(store: Store, *, name: str | None, why: str | None, check: str | None, url: str | None,
        every: str | None, expires: str | None, message: str | None, text: str | None = None,
        status: str | int | None = None, keep_watching: bool = False, allow_private: bool = False,
        expiry_message: str | None = None, now: datetime | None = None) -> dict:
    now = now or utcnow()
    record = {
        "id": f"tw-{secrets.token_hex(4)}",
        "name": _clean(name, "name", MAX_NAME),
        "why": _clean(why, "why", MAX_WHY),
        **build_check(check, url, text, status, allow_private),
        "interval_s": parse_interval(every or "1h"),
        "expires_at": iso(parse_expiry(expires or "", now)),
        "message": _clean(message, "message", MAX_MESSAGE),
        "expiry_message": _clean(expiry_message, "expiry message", MAX_MESSAGE, required=False),
        "keep_watching": bool(keep_watching),
        "status": ACTIVE,
        "created_at": iso(now),
        "last_checked": None,
        "next_check": iso(now),
        "last_result": None,
        "last_outcome": None,
        "consecutive_failures": 0,
        "failure_alerted": False,
        "fire_count": 0,
        "condition_met": False,
        "seen": False,
        "baseline_hash": None,
        "outbox": [],
    }
    with store.transaction(create=True) as tripwires:
        tripwires[record["id"]] = record
    return record


def _get(tripwires: dict, tripwire_id: str) -> dict:
    rec = tripwires.get(tripwire_id)
    if rec is None:
        raise TripwireError(f"no tripwire with id {tripwire_id}")
    return rec


def cancel(store: Store, tripwire_id: str) -> dict:
    with store.transaction() as tripwires:
        rec = _get(tripwires, tripwire_id)
        if rec["status"] != ACTIVE:
            raise TripwireError(f"{tripwire_id} is {rec['status']}, only an active tripwire can be cancelled")
        rec["status"] = CANCELLED
        rec["outbox"] = []
        return dict(rec)


def archive(store: Store, tripwire_id: str) -> dict:
    with store.transaction() as tripwires:
        rec = _get(tripwires, tripwire_id)
        if rec["status"] == ACTIVE:
            raise TripwireError(f"{tripwire_id} is still active, cancel it first")
        if rec["status"] != ARCHIVED:
            rec["archived_from"] = rec["status"]
            rec["status"] = ARCHIVED
        return dict(rec)


# ---- fetching ------------------------------------------------------------------

def _refuse_private(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchError("unsupported redirect")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except OSError:
        raise FetchError("DNS failure") from None
    for info in infos:
        if not ipaddress.ip_address(info[4][0].split("%")[0]).is_global:
            raise FetchError("private address refused")


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, allow_private: bool) -> None:
        self.allow_private = allow_private

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self.allow_private:
            _refuse_private(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_url(url: str, allow_private: bool = False) -> Fetched:
    """GET a page. Any HTTP response comes back with its status; anything else
    raises FetchError naming a category of ours, never text from the remote."""
    try:
        if not allow_private:
            _refuse_private(url)
        opener = urllib.request.build_opener(_CheckedRedirect(allow_private))
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
        try:
            with opener.open(req, timeout=FETCH_TIMEOUT_S) as resp:
                return Fetched(resp.status, resp.read(MAX_BODY_BYTES).decode("utf8", "ignore"))
        except urllib.error.HTTPError as e:
            with e:
                return Fetched(e.code, e.read(MAX_BODY_BYTES).decode("utf8", "ignore"))
    except FetchError:
        raise
    except urllib.error.URLError as e:
        raise FetchError(_category(e.reason)) from None
    except Exception as e:  # noqa: BLE001 - anything that goes wrong is "could not check"
        raise FetchError(_category(e)) from None


def _category(exc: object) -> str:
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(exc, socket.gaierror):
        return "DNS failure"
    if isinstance(exc, ssl.SSLError):
        return "TLS failure"
    if isinstance(exc, FetchError):
        return str(exc)
    return "connection failed"


# ---- checks --------------------------------------------------------------------

_DROP = re.compile(r"<(script|style|noscript)\b.*?</\1\s*>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_BLOCK_TITLES = ("just a moment", "attention required", "access denied", "security check",
                 "are you a robot", "are you human", "pardon our interruption", "captcha",
                 "request blocked", "bot verification")
_BLOCK_MARKERS = ("cf-browser-verification", "cf_chl_opt", "/_incapsula_resource", "px-captcha",
                  "captcha-delivery.com", "enable javascript and cookies to continue",
                  "verify you are a human", "please verify you are human")
_THIN_PAGE_CHARS = 60


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text)).strip().lower()


def visible_text(body: str) -> str:
    return norm(_TAG.sub(" ", _DROP.sub(" ", body)))


def suspect_page(body: str) -> str | None:
    """Why a 200 response may not be the real page. A bot wall or an empty
    JavaScript shell must read as "could not check", never as "text is absent"."""
    title = _TITLE.search(body)
    if title and any(m in norm(title.group(1)) for m in _BLOCK_TITLES):
        return "bot check page"
    lowered = body.lower()
    if any(m in lowered for m in _BLOCK_MARKERS):
        return "bot check page"
    if len(visible_text(body)) < _THIN_PAGE_CHARS and "<script" in lowered:
        return "JavaScript-only page"
    return None


@dataclass
class Observation:
    outcome: str  # met | not_met | baseline | could_not_check
    result: str   # shown to the user; only ever our own wording
    seen: bool = False
    page_hash: str | None = None


def evaluate(rec: dict, fetch: Fetch = fetch_url) -> Observation:
    params = rec["params"]
    could_not = lambda why: Observation("could_not_check", f"could not check: {why}")  # noqa: E731
    try:
        got = fetch(params["url"], bool(params.get("allow_private")))
    except FetchError as e:
        return could_not(str(e))

    if rec["check"] == "http_status":
        if got.status == params["status"]:
            return Observation("met", f"HTTP {got.status}")
        if got.status in REFUSED_STATUSES or got.status >= 500:
            return could_not(f"HTTP {got.status}")
        return Observation("not_met", f"HTTP {got.status}")

    if not 200 <= got.status < 300:
        return could_not(f"HTTP {got.status}")
    suspect = suspect_page(got.body)
    visible = visible_text(got.body)

    if rec["check"] == "page_changed":
        if suspect:
            return could_not(suspect)
        digest = hashlib.sha256(visible.encode("utf8")).hexdigest()
        if not rec.get("baseline_hash"):
            return Observation("baseline", "baseline recorded", page_hash=digest)
        if digest == rec["baseline_hash"]:
            return Observation("not_met", "no change")
        return Observation("met", "changed", page_hash=digest)

    needle = norm(params["text"])
    found = needle in visible or needle in norm(got.body)
    if rec["check"] == "text_appears":
        if found:
            return Observation("met", "text found", seen=True)
        return could_not(suspect) if suspect else Observation("not_met", "text not found")
    if found:
        return Observation("not_met", "text still there", seen=True)
    if suspect:
        return could_not(suspect)
    if not rec.get("seen"):
        # Never seen, so its absence says nothing: the text may be wrong, or
        # only rendered by JavaScript. Not armed is not healthy.
        return could_not("text never seen on this page, so its absence means nothing yet")
    return Observation("met", "text gone")


# ---- messages ------------------------------------------------------------------

def fired_text(rec: dict) -> str:
    return f"TripWire fired: {rec['name']}\n{rec['message']}\n{rec['params']['url']}"


def expired_text(rec: dict) -> str:
    if rec.get("fire_count"):
        head = f"TripWire ended at its expiry: {rec['name']}. It fired {rec['fire_count']} time(s)."
    else:
        head = f"TripWire expired without firing: {rec['name']}\nWhy it existed: {rec['why']}"
    lines = [head]
    if rec.get("expiry_message"):
        lines.append(rec["expiry_message"])
    lines.append(f"Last result: {rec.get('last_result') or 'never checked'}")
    lines.append(rec["params"]["url"])
    return "\n".join(lines)


def failure_text(rec: dict) -> str:
    return (f"TripWire cannot see its page: {rec['name']}. {rec['consecutive_failures']} checks in a row "
            f"failed (last: {rec['last_result']}). It stays active, but treat it as blind until this "
            f"clears. This is the only alert for this run of failures.\n{rec['params']['url']}")


# ---- the run -------------------------------------------------------------------

def _pending_end(rec: dict) -> bool:
    return any(item["kind"] in (FIRED, EXPIRED) for item in rec.get("outbox", []))


def is_due(rec: dict, now: datetime) -> bool:
    if rec["status"] != ACTIVE or _pending_end(rec):
        return False
    return now >= parse_iso(rec["next_check"]) or now >= parse_iso(rec["expires_at"])


def apply_observation(rec: dict, obs: Observation, now: datetime) -> None:
    rec["last_checked"] = iso(now)
    rec["next_check"] = iso(now + timedelta(seconds=rec["interval_s"]))
    rec["last_result"] = obs.result
    rec["last_outcome"] = obs.outcome

    if obs.outcome == "could_not_check":
        rec["consecutive_failures"] += 1
        if rec["consecutive_failures"] >= FAILURE_ALERT_AFTER and not rec["failure_alerted"]:
            rec["failure_alerted"] = True
            rec["outbox"].append({"kind": "failure", "text": failure_text(rec)})
    else:
        rec["consecutive_failures"] = 0
        rec["failure_alerted"] = False
        rec["seen"] = rec["seen"] or obs.seen
        if obs.page_hash:
            rec["baseline_hash"] = obs.page_hash
        if obs.outcome == "met":
            # keep_watching fires when the condition becomes true, not on every
            # check while it stays true. Each page change is a new event.
            if not rec["condition_met"]:
                rec["fire_count"] += 1
                kind = FIRED if not rec["keep_watching"] else "refire"
                rec["outbox"].append({"kind": kind, "text": fired_text(rec)})
            rec["condition_met"] = rec["check"] != "page_changed"
        else:
            rec["condition_met"] = False

    if now >= parse_iso(rec["expires_at"]) and not _pending_end(rec):
        rec["outbox"].append({"kind": EXPIRED, "text": expired_text(rec)})


def _deliver(store: Store, notify: Notify, now: datetime) -> tuple[int, int]:
    sent = failed = 0
    for tripwire_id, rec in store.read().items():
        if rec["status"] != ACTIVE:
            continue
        for item in rec.get("outbox", []):
            if not notify(item["text"]):
                failed += 1
                break  # keep the order; the next tick tries again
            sent += 1
            with store.transaction() as tripwires:
                current = tripwires.get(tripwire_id)
                if not current or current["status"] != ACTIVE:
                    break
                current["outbox"] = [i for i in current["outbox"] if i != item]
                if item["kind"] in (FIRED, EXPIRED):
                    current["status"] = item["kind"]
                    current["ended_at"] = iso(now)
    return sent, failed


def run_due(store: Store, fetch: Fetch = fetch_url, notify: Notify | None = None,
            now: datetime | None = None, budget_s: float = RUN_BUDGET_S) -> dict:
    """One heartbeat tick. Checks happen outside the lock, and each result is
    applied to a fresh read, so a cancel that lands during a fetch wins."""
    notify = notify or send_notification
    now = now or utcnow()
    started = time.monotonic()
    tripwires = store.read()
    due = sorted((r for r in tripwires.values() if is_due(r, now)), key=lambda r: r["next_check"])
    checked = 0
    for snapshot in due:
        if time.monotonic() - started > budget_s:
            break
        obs = evaluate(snapshot, fetch)
        checked += 1
        with store.transaction() as current:
            rec = current.get(snapshot["id"])
            if rec and rec["status"] == ACTIVE:
                apply_observation(rec, obs, now)
    sent, failed = _deliver(store, notify, now)
    store.mark_run(now)
    active = sum(1 for r in store.read().values() if r["status"] == ACTIVE)
    return {"active": active, "due": len(due), "checked": checked, "sent": sent, "send_failed": failed}


# ---- notification --------------------------------------------------------------

def send_notification(text: str) -> bool:
    """Through chassis_alert in _alert.sh, so an install's existing alert route
    applies. TRIPWIRE_CHANNEL_ID sends to that Discord channel with the bot
    token instead. Every message starts with our own prefix, so discord-post.sh
    can never read one as an @file."""
    env = os.environ.copy()
    channel = env.get("TRIPWIRE_CHANNEL_ID", "").strip()
    script = '. "$ALERT_LIB"; chassis_alert "$1"'
    if channel:
        if not channel.isdigit():
            return False
        env["CHASSIS_ALERT_CMD"] = f"{SCRIPTS_DIR / 'discord-post.sh'} {channel}"
        if not env.get("DISCORD_BOT_TOKEN"):
            home = env.get("CUSTOMER_HOME") or env.get("CHASSIS_HOME") or ""
            script = f'set -a; . "{home}/.env" >/dev/null 2>&1; set +a; {script}'
    env["ALERT_LIB"] = str(SCRIPTS_DIR / "_alert.sh")
    try:
        done = subprocess.run(["bash", "-c", script, "tripwire", text], env=env, timeout=90,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


# ---- what the dashboard shows --------------------------------------------------

def relative(dt: datetime | None, now: datetime) -> str:
    if dt is None:
        return "never"
    seconds = int((dt - now).total_seconds())
    span = abs(seconds)
    if span < 60:
        amount = "under a minute"
    elif span < 3600:
        amount = f"{span // 60} min"
    elif span < 172800:
        amount = f"{span // 3600} h"
    else:
        amount = f"{span // 86400} days"
    return f"in {amount}" if seconds >= 0 else f"{amount} ago"


def _row(rec: dict, now: datetime) -> dict:
    parse = lambda key: parse_iso(rec[key]) if rec.get(key) else None  # noqa: E731
    params = rec["params"]
    target = {"text_appears": params.get("text"), "text_disappears": params.get("text"),
              "http_status": params.get("status")}.get(rec["check"])
    active = rec["status"] == ACTIVE
    blind = active and rec.get("last_outcome") == "could_not_check"
    if not active:
        next_check = "-"
    elif parse("next_check") <= now:
        next_check = "next tick"
    else:
        next_check = relative(parse("next_check"), now)
    return {
        **rec,
        "check_label": CHECK_TYPES.get(rec["check"], rec["check"]),
        "target": target,
        "url": params["url"],
        "active": active,
        "blind": blind,
        "last_checked_label": relative(parse("last_checked"), now),
        "next_check_label": next_check,
        "expires_label": relative(parse("expires_at"), now) if active else "-",
        "expires_iso": rec["expires_at"],
        "archivable": rec["status"] in (FIRED, EXPIRED, CANCELLED),
    }


def view(store: Store, show_archived: bool = False, now: datetime | None = None) -> dict:
    """Everything the tab renders. A dead heartbeat must not look like a quiet
    watch, and an unreadable store must not look like an empty one."""
    now = now or utcnow()
    try:
        records = store.read()
        error = None
    except StoreUnavailable as e:
        records, error = {}, str(e)
    order = {ACTIVE: 0, FIRED: 1, EXPIRED: 1, CANCELLED: 2, ARCHIVED: 3}
    ordered = sorted(records.values(), key=lambda r: r["created_at"], reverse=True)
    ordered.sort(key=lambda r: order.get(r["status"], 9))
    archived_count = sum(1 for r in ordered if r["status"] == ARCHIVED)
    rows = [_row(r, now) for r in ordered if show_archived or r["status"] != ARCHIVED]
    last_run = store.last_run()
    active = any(r["status"] == ACTIVE for r in ordered)
    stale = active and (last_run is None or (now - last_run).total_seconds() > HEARTBEAT_STALE_S)
    return {
        "rows": rows,
        "store_error": error,
        "archived_count": archived_count,
        "show_archived": show_archived,
        "heartbeat_stale": stale,
        "last_run_label": relative(last_run, now),
        "check_types": CHECK_TYPES,
    }


def origin_allowed(headers) -> bool:
    """State changes only from our own pages. Behind a proxy Host may not be
    the name the browser used, so X-Forwarded-Host counts, and Sec-Fetch-Site
    (which a page cannot set) settles it where the browser sends it."""
    fetch_site = (headers.get("sec-fetch-site") or "").lower()
    if fetch_site == "same-origin":
        return True
    if fetch_site in ("cross-site", "same-site"):
        return False
    source = headers.get("origin") or headers.get("referer")
    if not source:
        return False
    netloc = urllib.parse.urlsplit(source).netloc.lower()
    names = {(headers.get("host") or "").lower(), (headers.get("x-forwarded-host") or "").lower()}
    return bool(netloc) and netloc in names
