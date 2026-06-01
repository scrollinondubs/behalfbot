#!/usr/bin/env python3
"""briefing-server.py — serves the briefings/ directory over HTTP.

Read-only static-file server for an installer's daily briefing HTML files.
Listens on localhost only by default; expose to the rest of your tailnet
or LAN by binding to 0.0.0.0 OR by reverse-proxying via Tailscale Funnel,
nginx, Caddy, etc.

Usage:
    CHASSIS_HOME=/path/to/chassis python3 chassis/scripts/briefing-server.py
    CHASSIS_HOME=/path/to/chassis BRIEFING_PORT=9000 python3 chassis/scripts/briefing-server.py

Environment variables:
    CHASSIS_HOME     (required) — installer chassis root
    BRIEFING_PORT    (optional, default 8765) — port to bind
    BRIEFING_BIND    (optional, default 127.0.0.1) — bind address
"""

import http.server
import os
import socketserver
import sys

CHASSIS_HOME = os.environ.get("CHASSIS_HOME")
if not CHASSIS_HOME:
    sys.stderr.write("ERROR: CHASSIS_HOME must be set in env\n")
    sys.exit(2)

PORT = int(os.environ.get("BRIEFING_PORT", "8765"))
BIND = os.environ.get("BRIEFING_BIND", "127.0.0.1")
ROOT = os.path.join(CHASSIS_HOME, "briefings")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def address_string(self):
        return self.client_address[0]

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    if not os.path.isdir(ROOT):
        os.makedirs(ROOT, exist_ok=True)
    with Server((BIND, PORT), Handler) as httpd:
        sys.stderr.write(f"Serving {ROOT} at http://{BIND}:{PORT}\n")
        httpd.serve_forever()
