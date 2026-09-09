"""The scraper as a service: a URL in, a document out.

One POST and one GET, on the compose network only. Stdlib `http.server` for the
reason `screener.transcribe.server` and `screener.health` both are: a route that
takes two fields and answers with a dozen does not justify an async framework in
an image whose point is a dependency list you can read aloud.

**No authentication, by construction rather than omission.** This container
publishes no port and Caddy has no route to it. The session that guards the
browser's path lives in the status service, which is why the browser asks that
and it asks this.

The proxy credentials live here and nowhere else, which is the quiet benefit of
the split: the only process that can spend money on a fetch is the one whose job
is fetching.
"""

import json
import logging
import os
import signal
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8082

# A page is bounded, and so is what anyone may ask us to fetch.
MAX_BODY = 8192


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("%s %s", self.address_string(), format % args)

    def _respond(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._respond(HTTPStatus.OK, {"status": "ok"})
        else:
            self._respond(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/scrape":
            self._respond(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._respond(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "too long"})
            return
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._respond(HTTPStatus.BAD_REQUEST, {"error": "not json"})
            return

        url = str(payload.get("url") or "").strip()
        if not url:
            self._respond(HTTPStatus.BAD_REQUEST, {"error": "no url"})
            return

        # Imported here, not at module scope: this is the only process that
        # holds trafilatura, and keeping the import inside the handler means the
        # module can be imported by a test that has no extras installed.
        from screener.magpie.run import scrape

        try:
            outcome = scrape(url, requested_by=payload.get("requested_by"))
        except Exception as exc:
            # `scrape` is documented never to raise, and this is here for the
            # day that stops being true. Without it the handler dies mid-request
            # and the caller sees "server disconnected without sending a
            # response", which says nothing about what went wrong — it is how
            # a blob store the container could not write to looked.
            logger.exception("scrape failed for %s", url[:120])
            self._respond(
                HTTPStatus.OK,
                {"reason": "failed", "detail": f"the scraper errored: {type(exc).__name__}"},
            )
            return
        if outcome.document is not None:
            document = outcome.document
            self._respond(
                HTTPStatus.OK,
                {
                    "document": {
                        "id": document.id,
                        "url": document.url,
                        "canonical_url": document.canonical_url,
                        "host": document.host,
                        "title": document.title,
                        "author": document.author,
                        "published": document.published.isoformat() if document.published else None,
                        "word_count": document.word_count,
                        "strategy": document.strategy,
                        "attempts": list(document.attempts),
                        "cost_usd": document.cost_usd,
                        "stored": document.stored,
                        "fetched_at": document.fetched_at.isoformat(),
                        # A lead, never the article. The whole text is in
                        # `magpie.document` and on /playground.
                        "lead": document.text[:400],
                    }
                },
            )
            return

        refused = outcome.refused
        assert refused is not None
        self._respond(
            HTTPStatus.OK,
            {
                "reason": refused.reason,
                "detail": refused.detail,
                "attempts": list(refused.attempts),
            },
        )


def build_server(host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def _settle_what_was_left() -> None:
    """Close out attempts a previous process died holding. Never raises.

    Before the first request, so an attempt still reading 'running' can only
    belong to a process that is gone. Failing to reconcile is not a reason to
    refuse to start — the rows are untidy, not dangerous.
    """
    try:
        import psycopg

        from screener.config import settings
        from screener.magpie import store

        with psycopg.connect(
            settings().database_url, connect_timeout=5, autocommit=True
        ) as conn:
            settled = store.reconcile(conn)
        if settled:
            logger.info("settled %d attempt(s) left by a previous process", settled)
    except Exception as exc:
        logger.warning("could not settle previous attempts: %s", exc)


def serve() -> int:
    """Run until SIGTERM. PID 1 in its container, so the signal is handled."""
    _settle_what_was_left()
    port = int(os.environ.get("MAGPIE_PORT") or DEFAULT_PORT)
    server = build_server(port=port)

    stopping = threading.Event()

    def stop(*_: Any) -> None:
        stopping.set()
        # From another thread, because `shutdown` blocks until `serve_forever`
        # returns and calling it from the handler would deadlock.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    logger.info("magpie serving on 0.0.0.0:%d", port)
    server.serve_forever()
    server.server_close()
    return 0
