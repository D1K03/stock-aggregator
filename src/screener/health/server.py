"""The HTTP server itself."""

import json
import logging
import signal
import threading
import urllib.parse
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import psycopg

from screener import auth
from screener.auth.config import AuthConfig
from screener.auth.session import state_cookie
from screener.config import settings
from screener.health import checks
from screener.provenance import git_sha

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8080

_STARTED_AT = datetime.now(UTC)

# Liveness and readiness stay open. Both are probed by things that cannot hold a
# session: Docker's healthcheck and the deploy's smoke test. They report whether
# the process is up and whether Postgres answers, which is not worth protecting.
PUBLIC_PATHS = frozenset({"/health", "/ready"})


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

    # -- plumbing ---------------------------------------------------------

    def _send(
        self, status: HTTPStatus, body: bytes, headers: list[tuple[str, str]]
    ) -> None:
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond(
        self,
        status: HTTPStatus,
        payload: dict[str, Any],
        cookies: list[str] | None = None,
    ) -> None:
        headers = [("Content-Type", "application/json"), ("Cache-Control", "no-store")]
        headers += [("Set-Cookie", c) for c in cookies or []]
        self._send(status, json.dumps(payload).encode(), headers)

    def _redirect(self, location: str, cookies: list[str] | None = None) -> None:
        headers = [("Location", location), ("Cache-Control", "no-store")]
        headers += [("Set-Cookie", c) for c in cookies or []]
        self._send(HTTPStatus.FOUND, b"", headers)

    def _session_token(self) -> str | None:
        return auth.read_cookie(self.headers.get("Cookie"), auth.SESSION_COOKIE)

    def _current_login(self, config: AuthConfig) -> str | None:
        """Who is signed in, or None.

        Raises `SessionLookupFailed` when the database cannot be reached, which
        the caller reports separately. Collapsing that into "not signed in"
        would send someone hunting for an expired cookie during an outage.
        """
        token = self._session_token()
        if config.session_secret is None or not token:
            return None
        try:
            with psycopg.connect(settings().database_url, connect_timeout=3) as conn:
                return auth.resolve_session(conn, token, config.session_secret)
        except Exception as exc:
            raise auth.SessionLookupFailed(type(exc).__name__) from exc

    # -- routes -----------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        route, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        config = AuthConfig.from_env()

        if route == "/health":
            # Touches nothing, deliberately. This is what the container
            # healthcheck hits: if it consulted Postgres, a database blip would
            # restart a perfectly healthy container repeatedly, and restarting
            # would not fix anything.
            self._respond(HTTPStatus.OK, {"status": "ok"})

        elif route == "/ready":
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

        elif route == "/auth/login":
            self._login(config)

        elif route == "/auth/callback":
            self._callback(config, query)

        elif route == "/auth/logout":
            token = self._session_token()
            if token and config.session_secret is not None:
                try:
                    with psycopg.connect(
                        settings().database_url, connect_timeout=3
                    ) as conn:
                        auth.delete_session(conn, token, config.session_secret)
                except Exception as exc:
                    # The cookie is cleared regardless. Leaving a stale row
                    # behind is untidy; refusing to sign someone out because
                    # Postgres blinked is worse.
                    logger.warning("could not delete the session row: %s", exc)
            self._redirect(
                "/status",
                [auth.clear_cookie(auth.SESSION_COOKIE, secure=self._secure(config))],
            )

        elif route == "/status":
            try:
                login = self._current_login(config)
            except auth.SessionLookupFailed as exc:
                self._respond(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "cannot check the session", "database": str(exc)},
                )
                return
            if config.enabled and login is None:
                self._respond(
                    HTTPStatus.UNAUTHORIZED,
                    {"error": "sign in at /auth/login"},
                )
                return
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
                    "login": login,
                },
            )

        else:
            self._respond(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _secure(self, config: AuthConfig) -> bool:
        """Whether cookies may carry the Secure flag.

        Set whenever the app is reached over https. Marking a cookie Secure on a
        plain-http development server means the browser silently drops it, and
        sign-in then fails in a way that looks like the session was rejected.
        """
        return config.base_url.startswith("https://")

    def _login(self, config: AuthConfig) -> None:
        if not config.enabled or config.client_id is None:
            self._respond(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "GitHub sign-in is not configured"},
            )
            return
        state = auth.new_state()
        self._redirect(
            auth.authorize_url(config.client_id, state, config.redirect_uri),
            [state_cookie(state, secure=self._secure(config))],
        )

    def _callback(self, config: AuthConfig, query: dict[str, list[str]]) -> None:
        if not config.enabled or config.client_id is None or config.client_secret is None:
            self._respond(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "GitHub sign-in is not configured"},
            )
            return

        expected = auth.read_cookie(self.headers.get("Cookie"), auth.STATE_COOKIE)
        supplied = (query.get("state") or [""])[0]
        import hmac as _hmac

        if not expected or not _hmac.compare_digest(expected, supplied):
            # Without this a third party could hand someone a callback URL and
            # sign them in as an account they do not control.
            self._respond(HTTPStatus.BAD_REQUEST, {"error": "state mismatch"})
            return

        code = (query.get("code") or [""])[0]
        if not code:
            self._respond(HTTPStatus.BAD_REQUEST, {"error": "no code"})
            return

        try:
            token = auth.exchange_code(
                code,
                client_id=config.client_id,
                client_secret=config.client_secret,
                redirect_uri=config.redirect_uri,
            )
            user = auth.fetch_user(token)
        except auth.OAuthError as exc:
            logger.warning("sign-in failed: %s", exc)
            self._respond(HTTPStatus.BAD_GATEWAY, {"error": "sign-in failed"})
            return

        if not config.permits(user.login):
            logger.warning("refused sign-in for %s", user.login)
            self._respond(HTTPStatus.FORBIDDEN, {"error": "not permitted"})
            return

        assert config.session_secret is not None  # implied by config.enabled
        try:
            with psycopg.connect(settings().database_url, connect_timeout=5) as conn:
                session = auth.create_session(
                    conn,
                    github_id=user.user_id,
                    login=user.login,
                    secret=config.session_secret,
                    days=config.session_days,
                    user_agent=self.headers.get("User-Agent"),
                )
        except Exception as exc:
            logger.error("could not record the session: %s", exc)
            self._respond(
                HTTPStatus.SERVICE_UNAVAILABLE, {"error": "cannot record the session"}
            )
            return
        logger.info("signed in %s", user.login)
        secure = self._secure(config)
        self._redirect(
            "/status",
            [
                auth.session_cookie(session, days=config.session_days, secure=secure),
                auth.clear_cookie(auth.STATE_COOKIE, secure=secure),
            ],
        )


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

    config = AuthConfig.from_env()
    logger.info(
        "serving on %s:%d, GitHub sign-in %s",
        host,
        port,
        "enabled for " + ", ".join(sorted(config.allowed_logins))
        if config.enabled
        else "not configured",
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        logger.info("health server stopped")
