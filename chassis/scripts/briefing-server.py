#!/usr/bin/env python3
"""briefing-server.py - starts the chassis dashboard (chassis/dashboard/app.py).

Kept so launchd and systemd units that already run this file keep working
after the dashboard replaced the static briefing server. Same port and bind
as before (BRIEFING_PORT / BRIEFING_BIND still work; DASHBOARD_PORT /
DASHBOARD_BIND win when set), and briefing links the old server handed out
still resolve.

If this python lacks the dashboard's packages (fastapi, uvicorn, jinja2), it
logs one line naming the install command and falls back to the old
read-only static server over the briefings dir, so an update never takes the
briefings URL down. See chassis/docs/dashboard.md.

Environment variables:
    CUSTOMER_HOME    customer state root (falls back to CHASSIS_HOME, then
                     ~/.behalfbot, then /app/customer)
    DASHBOARD_PORT   default BRIEFING_PORT, else 8765
    DASHBOARD_BIND   default BRIEFING_BIND, else 127.0.0.1
"""

import http.server
import os
import socketserver
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent / "dashboard" / "app.py"
REQS = HERE.parent / "dashboard" / "requirements.txt"


def _have_dashboard_deps() -> bool:
    try:
        import fastapi  # noqa: F401
        import jinja2  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        return False
    return True


def _static_fallback() -> None:
    sys.path.insert(0, str(HERE))
    from _paths import customer_home

    port = int(os.environ.get("DASHBOARD_PORT") or os.environ.get("BRIEFING_PORT") or 8765)
    bind = os.environ.get("DASHBOARD_BIND") or os.environ.get("BRIEFING_BIND") or "127.0.0.1"
    root = str(customer_home() / "briefings")
    os.makedirs(root, exist_ok=True)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=root, **kwargs)

        def address_string(self):
            return self.client_address[0]

        def log_message(self, fmt, *args):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with Server((bind, port), Handler) as httpd:
        sys.stderr.write(f"Serving {root} at http://{bind}:{port} (static fallback)\n")
        httpd.serve_forever()


def _run_dashboard() -> bool:
    """Run the dashboard in this process. False if it cannot start here
    (packages missing, or a python too old for them)."""
    if not _have_dashboard_deps():
        sys.stderr.write(
            f"briefing-server: dashboard packages missing for {sys.executable}; serving briefings only. "
            f"Install with: {sys.executable} -m pip install -r {REQS}\n"
        )
        return False
    sys.path.insert(0, str(APP.parent))
    try:
        import app
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"briefing-server: dashboard failed to start ({type(e).__name__}: {e}); serving briefings only\n")
        return False
    app.main()
    return True


if __name__ == "__main__":
    if not _run_dashboard():
        _static_fallback()
