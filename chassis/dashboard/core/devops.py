"""DevOps tab: service probes, remote containers, backups, heartbeats, disk,
plus any panels plugins contribute.

What to probe comes from the `dashboard.devops` key in
$CUSTOMER_HOME/chassis.config.yaml. With no such key the page still shows the
heartbeat state, the backups dir if one exists, and the disk panel.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request

from core.disk import load_summary as load_disk_summary
from core.disk import router as disk_router
from tabs import current_registry, customer_home, page, run_panels

log = logging.getLogger("dashboard")

router = APIRouter()
router.include_router(disk_router)

PROBE_KINDS = ("launchd", "container", "http", "tmux")
DEFAULT_STALE_HOURS = 48


# ---- config --------------------------------------------------------------------

def load_config(path: Path | None = None) -> tuple[dict, str | None]:
    """The dashboard.devops mapping and a note when it could not be read.

    A missing file, a missing key, no pyyaml and a parse error all yield {}:
    the page renders with defaults and shows the note instead of failing.
    """
    path = path or customer_home() / "chassis.config.yaml"
    if not path.is_file():
        return {}, None
    try:
        import yaml
    except ImportError:
        return {}, f"{path.name} not read: pyyaml is not installed for this python"
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except Exception as e:  # noqa: BLE001
        return {}, f"{path.name} not read: {type(e).__name__}: {e}"
    dash = raw.get("dashboard") if isinstance(raw, dict) else None
    devops = dash.get("devops") if isinstance(dash, dict) else None
    if devops is None:
        return {}, None
    if not isinstance(devops, dict):
        return {}, "dashboard.devops must be a mapping"
    return devops, None


def normalize_services(raw) -> tuple[list[dict], list[str]]:
    """[{label, kind, target}] plus one note per entry that names no probe."""
    services, notes = [], []
    for i, svc in enumerate(raw or []):
        if not isinstance(svc, dict):
            notes.append(f"services[{i}] is not a mapping")
            continue
        if "tmux_session" in svc and "tmux" not in svc:
            svc = {**svc, "tmux": svc["tmux_session"]}
        kinds = [k for k in PROBE_KINDS if svc.get(k)]
        if len(kinds) != 1:
            notes.append(f"services[{i}] ({svc.get('label', '?')}) needs exactly one of {', '.join(PROBE_KINDS)}")
            continue
        kind = kinds[0]
        services.append({"label": str(svc.get("label") or svc[kind]), "kind": kind, "target": str(svc[kind])})
    return services, notes


# ---- probes --------------------------------------------------------------------

def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def probe_http(url: str) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = resp.read(65536)
    except Exception as e:  # noqa: BLE001
        return False, f"unreachable: {e}"
    try:
        models = json.loads(body).get("models")
        if isinstance(models, list):
            return True, f"serving ({len(models)} models)"
    except (ValueError, AttributeError):
        pass
    return True, "serving"


def probe_tmux(session: str) -> tuple[bool, str]:
    r = _run(["tmux", "has-session", "-t", session], 5)
    return (True, "tmux session active") if r.returncode == 0 else (False, "tmux session missing")


def probe_container(name: str) -> tuple[bool, str]:
    r = _run(["docker", "inspect", name, "--format",
              "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}"], 8)
    if r.returncode != 0:
        return False, "container not found"
    state, health = r.stdout.strip().split("|", 1)
    ok = state == "running" and health in ("healthy", "no-healthcheck")
    if state != "running":
        return ok, f"container {state}"
    if health == "healthy":
        return ok, "running (healthy)"
    if health == "no-healthcheck":
        return ok, "running (no healthcheck defined)"
    return ok, f"running ({health})"


def probe_launchd(label: str) -> tuple[bool, str]:
    r = _run(["launchctl", "list", label], 5)
    if r.returncode != 0:
        return False, "not loaded"
    # `launchctl list <label>` prints a plist, not the tabular form. Read the
    # real keys; the first line is always "{".
    out = r.stdout
    pid_m = re.search(r'"PID"\s*=\s*(\d+)', out)
    exit_m = re.search(r'"LastExitStatus"\s*=\s*(-?\d+)', out)
    last_exit = int(exit_m.group(1)) if exit_m else 0
    if pid_m:
        return True, f"running (pid {pid_m.group(1)})"
    if '"OnDemand" = true' in out:
        # Interval job between runs: idle is normal, only a bad last exit counts.
        return (True, "loaded, idle") if last_exit == 0 else (False, f"loaded, last exit {last_exit}")
    return False, f"loaded but not running (last exit {last_exit})"


PROBES = {"http": probe_http, "tmux": probe_tmux, "container": probe_container, "launchd": probe_launchd}


def check_services(services: list[dict]) -> list[dict]:
    out = []
    for svc in services:
        try:
            ok, status = PROBES[svc["kind"]](svc["target"])
        except FileNotFoundError as e:
            ok, status = False, f"{e.filename or svc['kind']} not installed"
        except Exception as e:  # noqa: BLE001
            ok, status = False, f"error: {e}"
        out.append({"label": svc["label"], "probe": f"{svc['kind']}:{svc['target']}", "ok": ok, "status": status})
    return out


def check_remote(host: dict) -> dict:
    """`docker ps` over ssh on one host, matched against the expected names."""
    ssh = str(host.get("ssh") or "")
    names = [str(c) for c in host.get("containers") or []]
    cmd = ("sudo " if host.get("sudo") else "") + "docker ps --format '{{.Names}}:{{.Status}}'"
    try:
        r = _run(["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", ssh, cmd], 12)
        if r.returncode != 0:
            rows = [{"name": c, "status": "ssh failed", "ok": False} for c in names]
        else:
            running = {}
            for line in r.stdout.strip().splitlines():
                if ":" in line:
                    n, s = line.split(":", 1)
                    running[n.strip()] = s.strip()
            rows = [{"name": c, "status": running.get(c, "not found"),
                     "ok": running.get(c, "").startswith("Up")} for c in names]
    except Exception as e:  # noqa: BLE001
        rows = [{"name": c, "status": f"error: {e}", "ok": False} for c in names]
    return {"host": ssh, "containers": rows}


def load_heartbeats() -> list[dict]:
    try:
        state = json.loads((customer_home() / "scheduled-tasks" / "heartbeat-state.json").read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(state, dict):
        return []
    out = []
    for name, s in sorted(state.items()):
        # The state file can carry flat scalar keys next to the per-heartbeat
        # objects; skip them rather than 500 the page.
        if not isinstance(s, dict):
            continue
        fire_count = s.get("fire_count")
        failures = s.get("consecutive_failures", 0)
        out.append({
            "name": name,
            "last_check": s.get("last_checked") or s.get("last_check") or "never",
            "last_fire": s.get("last_fired") or s.get("last_fire") or "never",
            "failures": failures if isinstance(failures, int) else 0,
            "fire_count": fire_count if fire_count is not None else "-",
        })
    return out


def _human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def scan_backups(cfg: dict) -> list[dict]:
    """One row per subdir of the backups dir: newest file, its age, sizes.

    Every target is expected to refresh daily and goes stale after
    backup_stale_hours (default 48) unless it is listed in
    static_backup_targets. archived_backup_targets are not listed at all.
    """
    rel = cfg.get("backups_dir") or "backups"
    backups_dir = Path(rel) if Path(rel).is_absolute() else customer_home() / rel
    if not backups_dir.is_dir():
        return []
    archived = {str(x) for x in cfg.get("archived_backup_targets") or []}
    static = {str(x) for x in cfg.get("static_backup_targets") or []}
    stale_h = float(cfg.get("backup_stale_hours") or DEFAULT_STALE_HOURS)
    now_ts = datetime.now(timezone.utc).timestamp()
    out = []
    for sub in sorted(backups_dir.iterdir()):
        if not sub.is_dir() or sub.name.startswith(".") or sub.name in archived:
            continue
        try:
            files = [f for f in sub.iterdir() if f.is_file() and not f.name.startswith(".")]
            if files:
                latest = max(files, key=lambda f: f.stat().st_mtime)
                latest_mtime, latest_name, latest_size = latest.stat().st_mtime, latest.name, latest.stat().st_size
            else:
                latest_mtime, latest_name, latest_size = sub.stat().st_mtime, None, 0
            total = sum(f.stat().st_size for f in sub.rglob("*") if f.is_file())
        except OSError:
            continue
        age_h = (now_ts - latest_mtime) / 3600
        is_daily = sub.name not in static
        out.append({
            "name": sub.name,
            "count": len(files),
            "latest_name": latest_name,
            "latest_at": datetime.fromtimestamp(latest_mtime, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M"),
            "age_hours": round(age_h, 1),
            "latest_size_human": _human_bytes(latest_size) if latest_size else "-",
            "total_size_human": _human_bytes(total),
            "ok": (not is_daily) or age_h < stale_h,
            "is_daily": is_daily,
        })
    return out


def build(cfg: dict, notes: list[str]) -> dict:
    reg = current_registry()
    services, svc_notes = normalize_services(cfg.get("services"))
    notes = notes + svc_notes
    remote = [check_remote(h) for h in cfg.get("remote_hosts") or [] if isinstance(h, dict) and h.get("ssh")]
    load_errors = [
        {"label": f"{e.kind} failed to load", "value": e.ident, "status": "fail", "detail": e.error}
        for e in reg.errors
    ]
    return {
        "services": check_services(services),
        "remote": remote,
        "heartbeats": load_heartbeats(),
        "backups": scan_backups(cfg),
        "backups_stale_hours": float(cfg.get("backup_stale_hours") or DEFAULT_STALE_HOURS),
        "disk_enabled": cfg.get("disk_map", True) is not False,
        "disk": load_disk_summary(),
        "panels": run_panels(reg),
        "load_errors": load_errors,
        "notes": notes,
        "configured": bool(cfg),
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


@router.get("")
def devops(request: Request):
    cfg, note = load_config()
    return page(request, "devops.html", build(cfg, [note] if note else []))
