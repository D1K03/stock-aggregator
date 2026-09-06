"""JSON-RPC over one HTTP endpoint: the Streamable HTTP transport, by hand.

Roughly two hundred lines, which is why there is no SDK here. The official one
would bring pydantic, starlette and anyio into a project whose three runtime
dependencies are a stated constraint, to replace a dispatch table with five
entries. `screener.blobs` hand-rolls SigV4 on the same reasoning.

**Nothing streams.** The spec lets a server answer a JSON-RPC request with a
single `application/json` object instead of opening an SSE stream, and that is
what this does — `GET` gets a 405. It is not a shortcut: `cloudflared` buffers
server-sent events, so a streaming transport through this deployment's tunnel
would deliver every message at once when the response closed. A transport that
never opens a stream cannot be buffered.

Stateless, too. `Mcp-Session-Id` is a MAY, and with no session there is nothing
to expire, resume, or leak between two containers behind one hostname.
"""

import json
import logging
import threading
import time
from typing import Any

from screener import playground
from screener.audit import record
from screener.mcp import config, tools

logger = logging.getLogger(__name__)

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# In-flight tool calls across every connector at once.
#
# The engine opens a connection per query and the status service runs a thread
# per HTTP connection, so nothing upstream of here limits anything. Claude will
# issue several tool calls in parallel, each of which is three connections —
# session check, query, audit row. Without this the ceiling is Postgres's
# `max_connections`, reached first by an unrelated page failing to load.
#
# Acquired with a timeout rather than blocking: a queue of held threads is the
# failure this exists to prevent, so a caller that cannot get in is told so.
_slots = threading.BoundedSemaphore(config.MAX_CONCURRENT)
_WAIT_SECONDS = 5.0


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def rpc_error(message: str) -> dict[str, Any]:
    """A transport-level refusal, in the envelope a JSON-RPC client can read.

    Used where the failure happens before a request has been parsed and there is
    no id to answer — a missing token, an unreadable body, an unsupported
    protocol version. `id: null` is what the spec says to send when the id is
    unknown.
    """
    return {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": INVALID_REQUEST, "message": message},
    }


def _text(body: str, is_error: bool = False) -> dict[str, Any]:
    """A tool result, in the shape `tools/call` returns."""
    return {"content": [{"type": "text", "text": body}], "isError": is_error}


def handle(raw: bytes, *, actor: str) -> tuple[int, bytes | None]:
    """One JSON-RPC message in, one HTTP status and body out.

    `None` for the body means 202 with nothing, which is what a notification
    gets: the spec asks for an empty 202 rather than a JSON-RPC response, and a
    response to a notification is itself a protocol error.
    """
    try:
        message = json.loads(raw)
    except ValueError:
        return 400, json.dumps(error(None, PARSE_ERROR, "invalid JSON")).encode()

    if isinstance(message, list):
        # Batching was removed in the 2025-06-18 revision. Refusing it plainly
        # beats half-implementing it.
        return 400, json.dumps(
            error(None, INVALID_REQUEST, "batched requests are not supported")
        ).encode()
    if not isinstance(message, dict):
        return 400, json.dumps(
            error(None, INVALID_REQUEST, "expected a JSON-RPC object")
        ).encode()

    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params")
    if not isinstance(params, dict):
        params = {}

    if request_id is None:
        # A notification. `notifications/initialized` is the one that matters
        # and there is nothing to do for it; anything else is acknowledged
        # rather than argued with, because a server that errors on an unknown
        # notification breaks clients that send a harmless new one.
        return 202, None

    if not isinstance(method, str):
        return 200, json.dumps(
            error(request_id, INVALID_REQUEST, "no method")
        ).encode()

    try:
        payload = _dispatch(method, params, request_id, actor)
    except Exception as exc:  # noqa: BLE001 - a protocol error is not a crash
        logger.exception("mcp: %s failed", method)
        payload = error(request_id, INTERNAL_ERROR, type(exc).__name__)

    # 200 even for a JSON-RPC error. The HTTP status is about the transport and
    # the envelope is about the call; conflating them is how a client decides a
    # perfectly delivered "unknown tool" was a network failure.
    return 200, json.dumps(payload).encode()


def _dispatch(
    method: str, params: dict[str, Any], request_id: Any, actor: str
) -> dict[str, Any]:
    if method == "initialize":
        return _result(request_id, _initialize(params))
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": tools.specs()})
    if method == "tools/call":
        return _call(params, request_id, actor)
    return error(request_id, METHOD_NOT_FOUND, f"no method {method!r}")


def _initialize(params: dict[str, Any]) -> dict[str, Any]:
    """Version negotiation, and the one place the client says who it is.

    The client's version is echoed when we speak it, and otherwise it is told
    ours — which the spec says should be the latest we support, leaving the
    client to decide whether to continue. `clientInfo` is logged and nothing
    else: it is unauthenticated, anybody can claim any name, and it must never
    reach an authorization decision.
    """
    asked = params.get("protocolVersion")
    client = params.get("clientInfo") or {}
    logger.info(
        "mcp: initialize from %s %s (protocol %s)",
        client.get("name", "?"),
        client.get("version", "?"),
        asked,
    )
    version = asked if asked in config.VERSIONS else config.DEFAULT_VERSION
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": config.SERVER_NAME, "version": _version()},
        "instructions": (
            "Read-only access to a multi-signal equity screener's database: "
            "daily price bars, company identity and sectors, Reddit posts and "
            "comments, live stream transcripts, and ingest history. "
            "Nothing here is investment advice and none of it is a live quote — "
            "prices are stored end-of-day bars. Check ingest_health before "
            "reading an absence of rows as an absence of events."
        ),
    }


def _call(params: dict[str, Any], request_id: Any, actor: str) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    tool = tools.BY_NAME.get(name) if isinstance(name, str) else None
    if tool is None:
        return error(request_id, INVALID_PARAMS, f"no tool named {name!r}")

    if not _slots.acquire(timeout=_WAIT_SECONDS):
        # Deliberately a tool error rather than a JSON-RPC one: the call was
        # understood and could be retried, and a model reads this and waits.
        return _result(
            request_id,
            _text("The database is busy with other queries. Try again.", True),
        )

    started = time.perf_counter()
    outcome = "ok"
    try:
        # The connector's own role for the length of this call, and only this
        # call. The dashboard's SQL console is served by the same process and
        # must keep its own — `playground.config` explains why that separation
        # is a branch here rather than the credential it is between containers.
        with playground.connecting_as(config.database_url()):
            body = tool.run(arguments)
    except Exception as exc:  # noqa: BLE001 - a failed tool is a result, not a crash
        logger.warning("mcp: tool %s failed: %s", tool.name, type(exc).__name__)
        outcome = "error"
        body = json.dumps({"error": f"{tool.name} failed ({type(exc).__name__})"})
    finally:
        _slots.release()

    duration = int((time.perf_counter() - started) * 1000)
    # Every call, with its arguments, attributed to the GitHub login that
    # authorised the connector. There was no trail of what claude.ai read
    # otherwise, and "which query did it run" is the first question anyone would
    # ask about a connector reading a database.
    record(
        kind="tool",
        operation=f"mcp.{tool.name}",
        actor=actor,
        actor_kind="github",
        outcome=outcome,
        duration_ms=duration,
        detail={"arguments": arguments, "chars": len(body)},
    )
    return _result(request_id, _text(body, outcome == "error"))


def _version() -> str:
    from screener.provenance import git_sha

    return git_sha()[:7] or "dev"
