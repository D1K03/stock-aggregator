"""The HTTP server itself."""

import json
import logging
import signal
import threading
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from screener.health import checks
from screener.provenance import git_sha

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8080

_STARTED_AT = datetime.now(UTC)


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 so cloudflared keeps a connection alive rather than completing a
    # TCP handshake per poll. It obliges every response to carry an accurate
    # Content-Length — `_respond` does — because a 1.1 client given neither a
    # length nor chunked encoding waits for the socket to close.
    protocol_version = "HTTP/1.1"

    # A half-open socket must not pin a worker thread indefinitely.
    timeout = 5

    server_version = "screener"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        """Silenced.

        cloudflared, the container healthcheck and the deploy smoke test all
        poll these endpoints continuously. The default access log would be the
        only thing in `docker logs` and would bury anything that mattered.
        """

    def _respond(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            # Touches nothing, deliberately. This is what the container
            # healthcheck hits: if it consulted Postgres, a database blip
            # would restart a perfectly healthy container repeatedly, and
            # restarting would not fix anything.
            self._respond(HTTPStatus.OK, {"status": "ok"})

        elif self.path == "/ready":
            reason, migrations = checks.database()
            ok = reason in {"ok", "no schema"}
            self._respond(
                HTTPStatus.OK if ok else HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "status": "ok" if ok else "unavailable",
                    "database": reason,
                    "migrations": migrations,
                },
            )

        elif self.path == "/status":
            # Process facts only, no queries. /status has to stay answerable
            # when the database is down, or the one endpoint that can tell you
            # which build is running goes dark exactly when you need it.
            self._respond(
                HTTPStatus.OK,
                {
                    "git_sha": git_sha(),
                    "started_at": _STARTED_AT.isoformat(),
                    "uptime_seconds": int(
                        (datetime.now(UTC) - _STARTED_AT).total_seconds()
                    ),
                },
            )

        else:
            self._respond(HTTPStatus.NOT_FOUND, {"error": "not found"})


def build_server(host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """A server bound to `host:port`, not yet serving.

    Threading is not optional once keep-alive is on: a single-threaded server
    is occupied for the whole life of one connection, so an idle cloudflared
    keep-alive would stall every other request — including the container
    healthcheck — until it timed out. That presents as a crash loop, not as
    slowness. `ThreadingHTTPServer` also sets `daemon_threads`, so a lingering
    handler cannot hold the process open at exit.

    Binds inside the container only. Nothing is published to the host: the
    tunnel dials outward and reaches this over the compose network, so there is
    no new listener on the VPS and no port to collide with the other stack.
    """
    return ThreadingHTTPServer((host, port), Handler)


def serve(host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> None:
    """Serve until SIGTERM or SIGINT."""
    server = build_server(host, port)

    def stop(*_: Any) -> None:
        # `shutdown()` blocks until `serve_forever()` returns, and
        # `serve_forever()` cannot return while the signal handler that called
        # it is still on the stack — calling it inline deadlocks. This process
        # is PID 1 in the container, so handling SIGTERM properly is the
        # difference between a clean `compose down` and a ten-second SIGKILL
        # wait on every single deploy.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    logger.info("serving health endpoints on %s:%d", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        logger.info("health server stopped")
