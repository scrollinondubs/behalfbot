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
import subprocess
import sys

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from tabs import SCRIPTS_DIR, customer_home, page

router = APIRouter()

SCAN_SCRIPT = SCRIPTS_DIR / "disk-map-scan.py"


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


def load_summary() -> dict | None:
    """Capacity numbers for the /devops panel, without the tree."""
    try:
        data = json.loads(_data_file().read_text())
        gb = lambda kb: kb / 1048576  # noqa: E731
        return {
            "scanned_at": data["scanned_at"],
            "used_gb": gb(data["used_kb"]),
            "total_gb": gb(data["total_kb"]),
            "free_gb": gb(data["total_kb"] - data["used_kb"]),
            "used_pct": data["used_kb"] / data["total_kb"] * 100,
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
