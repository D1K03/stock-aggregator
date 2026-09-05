"""The HTTP server itself."""

import asyncio
import json
import logging
import signal
import threading
import time
import urllib.parse
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import psycopg

from screener import audit, auth
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

# The login recorded for a local development session. Not a GitHub username,
# and paired with id 0, so it cannot collide with a real account.
LOCAL_LOGIN = "local-dev"

# A dashboard question is a sentence, not an essay. Bounded here so a runaway
# query string cannot become a bill.
MAX_QUESTION = 500

# What the screen description may contribute. Enough for a row and its
# filters, short of anything that could bloat every request.
MAX_CONTEXT = 400

# A pasted analytical query with a couple of CTEs, comfortably. Bounded on the
# Content-Length so an oversized one is refused before any of it is read.
MAX_SQL_BODY = 4_500

# A recording, bounded like the question above and for the same reason, one size
# class up. Two minutes of Opus at the bitrate the browser is told to use is
# comfortably under this.
MAX_AUDIO = 1_000_000

# The class timeout below is five seconds, which is right for a half-open socket
# and wrong for an upload over a mobile link. Raised for the body read alone,
# and put straight back, rather than loosening it for every request served.
BODY_TIMEOUT_SECONDS = 15


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

    def _read_body(self, limit: int) -> bytes | None:
        """The request body, or `None` having already answered.

        Under HTTP/1.1 an unread body is the next request as far as the
        connection is concerned, so every rejection here closes rather than
        leaving a megabyte to be parsed as a request line — the failure that
        keep-alive makes possible and that nothing would explain.

        No chunked encoding. The only client is a browser `fetch` with a Blob,
        and it sends a length.
        """
        raw = self.headers.get("Content-Length")
        if raw is None or not raw.strip().isdigit():
            self.close_connection = True
            self._respond(HTTPStatus.BAD_REQUEST, {"error": "no body"})
            return None
        length = int(raw)
        if length == 0:
            self.close_connection = True
            self._respond(HTTPStatus.BAD_REQUEST, {"error": "no body"})
            return None
        if length > limit:
            # Refused on the header, so the bytes are never read.
            self.close_connection = True
            self._respond(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": f"over {limit} bytes"}
            )
            return None

        self.connection.settimeout(BODY_TIMEOUT_SECONDS)
        try:
            body = self.rfile.read(length)
        except OSError:
            self.close_connection = True
            self._respond(HTTPStatus.BAD_REQUEST, {"error": "could not read the body"})
            return None
        finally:
            self.connection.settimeout(self.timeout)

        if len(body) != length:
            self.close_connection = True
            self._respond(HTTPStatus.BAD_REQUEST, {"error": "the upload ended early"})
            return None
        return body

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

    def do_POST(self) -> None:
        """The one route that needs a body.

        Everything else here is a GET with its argument in the query string, and
        `_ask` says why: a body means Content-Length, a read, and keeping a
        keep-alive connection in step, which is not worth it for a short string.
        Audio is not a short string, so this is the route that earns the handler
        rather than one that ignores the reasons it was avoided.
        """
        route = urllib.parse.urlparse(self.path).path
        config = AuthConfig.from_env()

        if route == "/api/transcribe":
            self._transcribe(config)
        elif route == "/api/playground/query":
            self._playground_query(config)
        else:
            self._respond(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _transcribe(self, config: AuthConfig) -> None:
        """Turn a recording from the dashboard into text.

        A session is required unconditionally, for the reason `_ask` gives at
        length. There is no decorator on these routes and each repeats the same
        six lines, so a route that skipped them would be public in silence.

        A thin authenticated pass-through, and the session is the entire reason
        the hop exists: the transcription container has no route from outside
        and no authentication of its own, so this is what stands in front of it.
        The audio is never written to disk.
        """
        try:
            login = self._current_login(config)
        except auth.SessionLookupFailed as exc:
            self._respond(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "cannot check the session", "database": str(exc)},
            )
            return
        if login is None:
            self._respond(HTTPStatus.UNAUTHORIZED, {"error": "sign in at /auth/login"})
            return

        from screener.bot import budget

        # Before the body is read, let alone forwarded. Transcription is a cost
        # even though it is not a charge, and the point of a cap is that the
        # request over it is never paid for.
        allowance = budget.check(login, "github")
        if not allowance.allowed:
            self._respond(
                HTTPStatus.TOO_MANY_REQUESTS,
                {
                    "error": (
                        "That would go over the daily spend cap — "
                        f"{budget.usd(allowance.spent)} of "
                        f"{budget.usd(allowance.cap)} used in the last 24 hours."
                    )
                },
            )
            return

        audio = self._read_body(MAX_AUDIO)
        if audio is None:
            return

        from screener.transcribe import transcribe

        started = time.monotonic()
        spoken = transcribe(
            audio, content_type=self.headers.get("Content-Type", "")
        )
        if spoken is None:
            self._respond(
                HTTPStatus.BAD_GATEWAY, {"error": "could not reach the transcriber"}
            )
            return

        audit.record(
            kind="agent",
            operation="steven.transcribe",
            actor=login,
            actor_kind="github",
            duration_ms=int((time.monotonic() - started) * 1000),
            # Not a charge, so not a number in the column that records charges.
            cost_usd=0,
            detail={
                "surface": "web",
                "seconds": spoken.seconds,
                "bytes": len(audio),
                # A length, not the words: this row is about what the
                # transcription cost. The text reaches the trail when it is
                # asked as a question, the same way a typed one does.
                "chars": len(spoken.text),
            },
        )
        self._respond(HTTPStatus.OK, {"text": spoken.text, "seconds": spoken.seconds})

    def _playground(self, config: AuthConfig) -> None:
        """What the read-only role may read, and the bounds it runs under.

        Behind the session like everything else here. Switched off is a 200
        saying so rather than an error: a feature deliberately not configured on
        this deployment should render a card explaining that, not a red box
        saying the server broke.
        """
        try:
            login = self._current_login(config)
        except auth.SessionLookupFailed as exc:
            self._respond(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "cannot check the session", "database": str(exc)},
            )
            return
        if login is None:
            self._respond(HTTPStatus.UNAUTHORIZED, {"error": "sign in at /auth/login"})
            return

        from screener import playground

        if not playground.enabled():
            self._respond(
                HTTPStatus.OK,
                {
                    "enabled": False,
                    "schemas": [],
                    "reason": "no read-only database role is configured on this deployment",
                },
            )
            return

        try:
            tables = playground.catalog()
        except Exception as exc:
            logger.warning("could not read the playground catalogue: %s", exc)
            self._respond(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "error": "cannot reach the playground database",
                    "database": type(exc).__name__,
                },
            )
            return

        schemas: dict[str, list[dict[str, Any]]] = {}
        for table in tables:
            schemas.setdefault(table.schema, []).append(
                {
                    "name": table.name,
                    "kind": table.kind,
                    "columns": [
                        {"name": c.name, "type": c.type, "nullable": c.nullable}
                        for c in table.columns
                    ],
                }
            )
        self._respond(
            HTTPStatus.OK,
            {
                "enabled": True,
                "schemas": [
                    {"name": name, "tables": tables_}
                    for name, tables_ in sorted(schemas.items())
                ],
                "limits": {
                    "max_rows": playground.MAX_ROWS,
                    "default_rows": playground.DEFAULT_ROWS,
                    "max_sql": playground.MAX_SQL,
                    "timeout_ms": playground.STATEMENT_TIMEOUT_MS,
                },
            },
        )

    def _playground_query(self, config: AuthConfig) -> None:
        """Run one read-only query.

        A POST rather than a GET, which inverts this server's usual reasoning
        for a reason: `_ask` uses a query string because a question is a short
        string, and a pasted analytical query is routinely past the practical
        URL length. A query in a URL also lands in access logs and browser
        history, and the audit trail is the right place for it.
        """
        try:
            login = self._current_login(config)
        except auth.SessionLookupFailed as exc:
            self._respond(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "cannot check the session", "database": str(exc)},
            )
            return
        if login is None:
            self._respond(HTTPStatus.UNAUTHORIZED, {"error": "sign in at /auth/login"})
            return

        body = self._read_body(MAX_SQL_BODY)
        if body is None:
            return
        try:
            payload = json.loads(body)
            statement = str(payload["sql"])
            limit = int(payload.get("limit") or 0)
        except Exception:
            self._respond(HTTPStatus.BAD_REQUEST, {"error": "expected {\"sql\": ...}"})
            return

        from screener import playground

        try:
            result = playground.run(statement, limit or playground.DEFAULT_ROWS)
        except playground.QueryError as exc:
            # The one place this service returns a database message rather than
            # an exception type. It is about SQL the reader just typed, and
            # "UndefinedColumn" with no position is a riddle.
            self._respond(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": exc.message,
                    "sqlstate": exc.sqlstate,
                    "position": exc.position,
                    "detail": exc.detail,
                    "hint": exc.hint,
                },
            )
            return
        except playground.NotConfigured:
            self._respond(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "the playground is not configured on this deployment"},
            )
            return
        except Exception as exc:
            logger.warning("playground query failed: %s", type(exc).__name__)
            self._respond(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "error": "cannot reach the playground database",
                    "database": type(exc).__name__,
                },
            )
            return

        self._respond(
            HTTPStatus.OK,
            {
                # Arrays rather than objects: `select 1 as a, 2 as a` is legal
                # SQL, and an object would silently lose a column.
                "columns": [{"name": c.name, "type": c.type} for c in result.columns],
                "rows": [list(row) for row in result.rows],
                "row_count": result.row_count,
                "truncated": result.truncated,
                "shortened": result.shortened,
                "ms": result.ms,
                "limit": result.limit,
            },
        )

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

        elif route == "/auth/local":
            self._local_login(config)

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
                "/login",
                [auth.clear_cookie(auth.SESSION_COOKIE, secure=self._secure(config))],
            )

        elif route == "/api/ask":
            self._ask(config, query)

        elif route == "/api/handoff":
            self._handoff(config, query)

        elif route == "/api/audit":
            self._audit(config, query)

        elif route == "/api/playground":
            self._playground(config)

        elif route == "/status":
            try:
                login = self._current_login(config)
            except auth.SessionLookupFailed as exc:
                self._respond(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "cannot check the session", "database": str(exc)},
                )
                return
            if login is None:
                self._respond(
                    HTTPStatus.UNAUTHORIZED,
                    {"error": "sign in at /auth/login"},
                )
                return
            # Process facts only, no queries — but a session had to be
            # checked to get here, and that does query. So /status is
            # unreachable while Postgres is: /health still proves the process
            # is alive and /ready still names the database as the fault, which
            # is a better trade than an endpoint that stops asking who you are
            # whenever its configuration goes missing.
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

    def _ask(self, config: AuthConfig, query: dict[str, list[str]]) -> None:
        """Ask Steven a question from the dashboard.

        A session is required unconditionally. This used to read
        `config.enabled and login is None`, which meant that if GitHub sign-in
        were ever unconfigured — one missing variable, a rotated secret, a
        typo in Infisical — every endpoint behind it opened to the internet
        rather than closing. An authorization check that weakens when its
        configuration goes missing is not one. Local development still works,
        because `/auth/local` issues a real session; if nothing can issue one,
        nobody gets in, which is the right way round.

        A GET with the question in the query string, because this server has no
        POST handler and adding one for a single short string would mean
        reading a body, minding Content-Length and keeping a keep-alive
        connection in step. Questions are a few hundred characters at most.

        The same agent the Discord bot uses, so there is one set of rules about
        what it may claim rather than two that drift.
        """
        try:
            login = self._current_login(config)
        except auth.SessionLookupFailed as exc:
            self._respond(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "cannot check the session", "database": str(exc)},
            )
            return
        if login is None:
            self._respond(HTTPStatus.UNAUTHORIZED, {"error": "sign in at /auth/login"})
            return

        question = (query.get("q") or [""])[0].strip()
        if not question:
            self._respond(HTTPStatus.BAD_REQUEST, {"error": "no question"})
            return
        if len(question) > MAX_QUESTION:
            # Bounded before it reaches the model, because the caller pays for
            # every token of a question nobody meant to send.
            self._respond(
                HTTPStatus.BAD_REQUEST,
                {"error": f"question longer than {MAX_QUESTION} characters"},
            )
            return

        # Bounded like the question: it is assembled by the browser and paid
        # for on every message.
        context = (query.get("context") or [""])[0].strip()[:MAX_CONTEXT]

        # Steven remembers the last couple of exchanges, read back from the
        # trail per person rather than sent up with the question. This flag is
        # the one thing the server cannot know: whether the button pressed was
        # New chat or carry on. Default false, so a dropped parameter loses the
        # memory rather than carrying the wrong conversation into a new thread.
        fresh = (query.get("fresh") or [""])[0] == "1"

        from screener.bot import agent

        try:
            reply = asyncio.run(
                agent.respond(
                    question,
                    actor=login or "local",
                    # A dashboard user signed in through GitHub, so that is the
                    # kind of identity; `surface` records that they were here
                    # rather than in Discord.
                    actor_kind="github",
                    surface="web",
                    context=context,
                    fresh=fresh,
                    # The dashboard can render a chart, so the chart tool is
                    # allowed to draw one. Discord gets the same agent with
                    # this off.
                    can_draw=True,
                    can_table=True,
                )
            )
        except Exception as exc:
            logger.warning("ask failed: %s", exc)
            self._respond(HTTPStatus.BAD_GATEWAY, {"error": "could not reach the model"})
            return

        self._respond(
            HTTPStatus.OK,
            {
                "reply": reply.text,
                "model": agent.agent_model(),
                "tools": [{"name": t.name, "ms": t.ms} for t in reply.tools],
                # Drawn by a tool and passed straight through. None of this was
                # in the conversation, so none of it was paid for per round.
                "charts": [c.payload() for c in reply.charts],
                "rows": [r.payload() for r in reply.rows],
            },
        )

    def _handoff(self, config: AuthConfig, query: dict[str, list[str]]) -> None:
        """Carry the conversation over to Discord.

        Sends the signed-in user a direct message so the thread continues
        somewhere they already get notifications. Which Discord account belongs
        to which login comes from `DISCORD_USER_MAP`, so no account id is in
        the repository.
        """
        try:
            login = self._current_login(config)
        except auth.SessionLookupFailed as exc:
            self._respond(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "cannot check the session", "database": str(exc)},
            )
            return
        if login is None:
            self._respond(HTTPStatus.UNAUTHORIZED, {"error": "sign in at /auth/login"})
            return

        from screener.bot.config import BotConfig
        from screener.bot.handoff import HandoffError, send_dm

        who = login or LOCAL_LOGIN
        seeing = (query.get("context") or [""])[0].strip()[:MAX_CONTEXT]

        # A fixed message rather than a generated one. It is one line, it is
        # the same every time, and paying a model to write it would be silly.
        text = (
            "Carrying on from the dashboard. Ask me here and I will pick it up."
        )
        if seeing:
            text += f"\n-# You were looking at {seeing}"

        try:
            user_id = send_dm(login=who, text=text, config=BotConfig.from_env())
        except HandoffError as exc:
            logger.warning("handoff failed for %s: %s", who, exc)
            self._respond(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
            return

        audit.record(
            kind="agent",
            operation="steven.handoff",
            actor=who,
            actor_kind="github",
            detail={
                "surface": "web",
                "discord_user_id": str(user_id),
                # Kept so the bot can pick the thread up. The message says "ask
                # me here and I will pick it up", and without this it cannot:
                # there is no conversation memory, so the next question in that
                # DM arrives with no antecedent and "can you chart it" gets
                # answered with "which ticker?".
                "context": seeing,
            },
        )
        self._respond(HTTPStatus.OK, {"sent": True})

    def _audit(self, config: AuthConfig, query: dict[str, list[str]]) -> None:
        """The audit trail, paged and filtered.

        Behind the session for the same reason /status is: it carries spend
        figures and the ids of people who used the bot.
        """
        try:
            login = self._current_login(config)
        except auth.SessionLookupFailed as exc:
            self._respond(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "cannot check the session", "database": str(exc)},
            )
            return
        if login is None:
            self._respond(HTTPStatus.UNAUTHORIZED, {"error": "sign in at /auth/login"})
            return

        kind = (query.get("kind") or [""])[0] or None
        operation = (query.get("operation") or [""])[0] or None
        try:
            page_number = max(1, int((query.get("page") or ["1"])[0]))
        except ValueError:
            page_number = 1

        try:
            with psycopg.connect(settings().database_url, connect_timeout=3) as conn:
                events, total = audit.page(
                    conn,
                    kind=kind,
                    operation=operation,
                    offset=(page_number - 1) * audit.PAGE_SIZE,
                )
                totals = audit.spend(conn)
                from screener.bot import budget
                from screener.bot.config import BotConfig

                actors = audit.by_actor(conn)
                # The mapping that already links the two identities for the
                # Discord handoff, read the other way round.
                people = audit.fold(
                    actors,
                    {
                        str(discord_id): login
                        for login, discord_id in BotConfig.from_env().user_map.items()
                    },
                )
                available = audit.operations(conn)
        except Exception as exc:
            logger.warning("could not read the audit trail: %s", exc)
            self._respond(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "cannot read the audit trail", "database": type(exc).__name__},
            )
            return

        self._respond(
            HTTPStatus.OK,
            {
                "events": [
                    {
                        "id": e.id,
                        "occurred_at": e.occurred_at.isoformat(),
                        "kind": e.kind,
                        "operation": e.operation,
                        "actor": e.actor,
                        "actor_kind": e.actor_kind,
                        "outcome": e.outcome,
                        "model": e.model,
                        "tokens": e.total_tokens,
                        # A float, not a Decimal: JSON has no decimal type, and
                        # these are displayed rather than summed again.
                        "cost_usd": float(e.cost_usd),
                        "duration_ms": e.duration_ms,
                        "detail": e.detail,
                    }
                    for e in events
                ],
                "page": page_number,
                "page_size": audit.PAGE_SIZE,
                "total": total,
                "pages": max(1, -(-total // audit.PAGE_SIZE)),
                "spend": {
                    "events": totals.events,
                    "total_cost_usd": float(totals.total_cost),
                    "total_tokens": totals.total_tokens,
                    "events_24h": totals.events_24h,
                    "cost_24h_usd": float(totals.cost_24h),
                    "tokens_24h": totals.tokens_24h,
                },
                # What one person may spend in a day, so the interface can
                # show how close each of them is rather than only the total.
                "daily_cap_usd": float(budget.daily_cap()),
                # Who spent it. Two people share one bill, and a single total
                # says the month was cheap or expensive without saying whose
                # questions made it so. Folded onto one row per human through
                # the same DISCORD_USER_MAP the handoff uses.
                "people": [
                    {
                        "login": p.login,
                        "known": p.known,
                        "avatar": audit.avatar(p),
                        "events": p.events,
                        "cost_usd": float(p.cost),
                        "tokens": p.tokens,
                        "cost_24h_usd": float(p.cost_24h),
                        "last_seen": p.last_seen.isoformat(),
                        "surfaces": [
                            {"kind": kind, "cost_usd": float(cost)}
                            for kind, cost in p.surfaces
                        ],
                    }
                    for p in people
                ],
                "operations": [
                    {"kind": k, "operation": o, "count": c} for k, o, c in available
                ],
            },
        )

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

    def _local_login(self, config: AuthConfig) -> None:
        """Sign in without GitHub. Local development only.

        The gate is that GitHub sign-in is *not* configured, and that is the
        whole safety argument: in production `GITHUB_CLIENT_ID`,
        `GITHUB_CLIENT_SECRET` and `SESSION_SECRET` all arrive from Infisical,
        so `config.enabled` is true and this route refuses. There is no
        separate flag to set wrongly, and no way to have both a working real
        sign-in and a working bypass at the same time.

        It issues a genuine session row rather than only setting a cookie, so
        local behaves the way production does: `/status` reports a login, the
        audit page is reached the same way, and signing out works.
        """
        if config.enabled:
            # Not 403. In production this route does not exist, and saying so
            # is one less thing worth probing.
            self._respond(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        if config.session_secret is None:
            self._respond(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "SESSION_SECRET is not set; see deploy/local.env.example"},
            )
            return

        try:
            with psycopg.connect(settings().database_url, connect_timeout=5) as conn:
                session = auth.create_session(
                    conn,
                    # Zero and a name nobody could hold: a real GitHub account
                    # can never collide with this row, and anyone reading
                    # auth.app_user can see at a glance it was not a sign-in.
                    github_id=0,
                    login=LOCAL_LOGIN,
                    secret=config.session_secret,
                    days=config.session_days,
                    user_agent=self.headers.get("User-Agent"),
                )
        except Exception as exc:
            logger.error("could not record the local session: %s", exc)
            self._respond(
                HTTPStatus.SERVICE_UNAVAILABLE, {"error": "cannot record the session"}
            )
            return

        logger.warning("issued a local development session; GitHub sign-in is off")
        self._redirect(
            "/",
            [
                auth.session_cookie(
                    session, days=config.session_days, secure=self._secure(config)
                )
            ],
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
        # The dashboard, not /status. Both are served from one origin, so a
        # signed-in browser should land on the page a person came for rather
        # than on the JSON a probe came for.
        self._redirect(
            "/",
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
