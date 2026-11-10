"""Disk map: an on-demand du scan rendered as a treemap at /devops/disk.

The scan lives in chassis/scripts/disk-map-scan.py so a heartbeat can run it
without the dashboard. A full scan takes a minute or two, so POST
/devops/disk/scan starts it in the background; the page polls
/devops/disk/status and reloads data.json once the pidfile clears. The
pidfile also stops two clicks from starting two du processes.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from tabs import SCRIPTS_DIR, customer_home, page

router = APIRouter()

SCAN_SCRIPT = SCRIPTS_DIR / "disk-map-scan.py"

# The point past which each number is unacceptable. Disk is full at 100%. The
# memory limits are judgment calls: nothing breaks there, but recall gets noisy.
DISK_LIMIT_PCT = 100
MEMORY_LIMIT_MB = 3
MEMORY_LIMIT_FILES = 1000


def _data_file():
    return customer_home() / "state" / "disk-map.json"


def _pid_file():
    return customer_home() / "state" / "disk-map.pid"


def running_pid() -> int | None:
    pid_file = _pid_file()
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        # Reap it if it is our finished child, so a zombie does not read as alive.
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pid_file.unlink(missing_ok=True)
        return None
    except PermissionError:
        pass
    return pid


def gauge_level(value: float, limit: float) -> str:
    """CSS class for a number nearing its limit: yellow within 30%, orange 20%, red 10%."""
    ratio = value / limit
    if ratio >= 0.9:
        return "gauge-red"
    if ratio >= 0.8:
        return "gauge-orange"
    if ratio >= 0.7:
        return "gauge-yellow"
    return ""


def memory_dir() -> pathlib.Path:
    """Claude Code's per-project memory folder for this install."""
    slug = re.sub(r"[/.]", "-", str(customer_home()))
    return pathlib.Path.home() / ".claude" / "projects" / slug / "memory"


def memory_dir_stat(root: pathlib.Path | None = None) -> dict | None:
    """Size and file count of the agent memory dir, recursive, leaving out its .git."""
    root = root or memory_dir()
    if not root.is_dir():
        return None
    files = size = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            try:
                size += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
            files += 1
    mb = size / 1048576
    return {
        "files": files, "mb": mb,
        "files_limit": MEMORY_LIMIT_FILES, "mb_limit": MEMORY_LIMIT_MB,
        "files_level": gauge_level(files, MEMORY_LIMIT_FILES),
        "mb_level": gauge_level(mb, MEMORY_LIMIT_MB),
    }


def load_summary() -> dict | None:
    """Capacity numbers for the /devops panel, without the tree."""
    try:
        data = json.loads(_data_file().read_text())
        gb = lambda kb: kb / 1048576  # noqa: E731
        used_pct = data["used_kb"] / data["total_kb"] * 100
        return {
            "scanned_at": data["scanned_at"],
            "used_gb": gb(data["used_kb"]),
            "total_gb": gb(data["total_kb"]),
            "free_gb": gb(data["total_kb"] - data["used_kb"]),
            "used_pct": used_pct,
            "level": gauge_level(used_pct, DISK_LIMIT_PCT),
            "unscanned_gb": gb(max(data["used_kb"] - data["measured_kb"], 0)),
        }
    except (OSError, ValueError, KeyError, ZeroDivisionError, TypeError):
        return None


@router.get("/disk")
def disk_page(request: Request):
    return page(request, "disk.html", {
        "has_data": _data_file().exists(),
        "scanning": running_pid() is not None,
    })


@router.get("/disk/data.json")
def disk_data():
    try:
        return JSONResponse(json.loads(_data_file().read_text()))
    except (OSError, ValueError):
        return JSONResponse({"error": "no scan yet"}, status_code=404)


@router.get("/disk/status")
def disk_status():
    summary = load_summary()
    return {"scanning": running_pid() is not None,
            "scanned_at": summary["scanned_at"] if summary else None}


@router.post("/disk/scan")
def disk_scan():
    if running_pid() is not None:
        return {"started": False, "scanning": True}
    home = customer_home()
    log_file = home / "logs" / "disk-map-scan.log"
    _pid_file().parent.mkdir(parents=True, exist_ok=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a") as logf:
        proc = subprocess.Popen(
            [sys.executable, str(SCAN_SCRIPT), "--out", str(_data_file())],
            stdout=logf, stderr=subprocess.STDOUT, start_new_session=True,
        )
    _pid_file().write_text(str(proc.pid))
    return {"started": True, "scanning": True}
