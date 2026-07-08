"""Minimal HTTP health-check server.

A long-polling Telegram bot makes only *outbound* connections and has no HTTP
server of its own. Hosting platforms such as Fly.io route through a proxy that
expects the machine to accept TCP connections on an internal port; when nothing
listens there the platform reports errors like
``failed to connect to machine`` (Fly error PM05).

This tiny server answers those health checks on ``/`` and ``/health`` so the
platform considers the machine healthy, while the bot keeps polling Telegram in
the main thread. It uses only the standard library.
"""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger("attendance_bot.health")


class _HealthHandler(BaseHTTPRequestHandler):
    def _ok(self, body: bytes = b"ok") -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        body = b"ok"
        self._ok(body)
        self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802
        self._ok()

    def log_message(self, *args, **kwargs) -> None:  # noqa: D401
        # Silence the default per-request stderr logging (health checks are
        # frequent and would flood the logs).
        return


def start_health_server(port: int) -> ThreadingHTTPServer:
    """Start the health server on ``0.0.0.0:port`` in a daemon thread."""
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(
        target=server.serve_forever, name="health-server", daemon=True
    )
    thread.start()
    logger.info("Health-check server listening on 0.0.0.0:%d", port)
    return server
