"""The MCP connector: claude.ai reading this screener's data, over OAuth.

A remote MCP server, addable to Claude as a custom connector, exposing the same
read-only engine the dashboard's SQL console uses. What it is *for* is the thing
a screener cannot do on its own: hold a week of Reddit, a month of price bars and
a live transcript in one context and be asked what it makes of them.

Three decisions shape everything here, and each is a constraint rather than a
preference.

**OAuth, because there is no simpler option.** Claude's `static_headers` mode —
a pasted API key — is beta and limited to selected organisations. Authless would
leave a database open to whoever learned the URL. `oauth_dcr` is what is
supported out of the box, so `oauth.py` is an OAuth 2.1 authorization server;
its consent step is the GitHub sign-in that already exists, so there is no
second identity to keep.

**Nothing streams.** The transport answers a POST with one JSON object rather
than opening an SSE stream. `cloudflared` buffers server-sent events, so a
streaming transport behind this deployment's tunnel would arrive all at once at
the end — the plainest reading of an earlier MCP attempt that did not work here.

**A role of its own.** `playground_mcp` (migration 018) rather than the
console's role or Steven's, because this caller differs from both in the way
that matters: they run inside this deployment and what this one reads leaves the
box. It is turned on by `PLAYGROUND_MCP_DB_PASSWORD` and is off without it.

Nothing here writes to the screener's data, and every tool call lands in the
audit trail attributed to the GitHub login that authorised the connector.
"""

from screener.mcp.config import (
    AUTHORIZATION_SERVER,
    AUTHORIZE_PATH,
    MAX_BODY,
    MAX_FORM_BODY,
    MAX_REGISTER_BODY,
    PATH,
    PROTECTED_RESOURCE,
    REGISTER_PATH,
    TOKEN_PATH,
    VERSIONS,
    enabled,
    resource,
)
from screener.mcp.oauth import (
    Response,
    Unauthorized,
    approve,
    authorization_server_document,
    authorize,
    bearer,
    protected_resource_document,
    register,
    token,
)
from screener.mcp.protocol import handle, rpc_error

__all__ = [
    "AUTHORIZATION_SERVER",
    "AUTHORIZE_PATH",
    "MAX_BODY",
    "MAX_FORM_BODY",
    "MAX_REGISTER_BODY",
    "PATH",
    "PROTECTED_RESOURCE",
    "REGISTER_PATH",
    "TOKEN_PATH",
    "VERSIONS",
    "Response",
    "Unauthorized",
    "approve",
    "authorization_server_document",
    "authorize",
    "bearer",
    "enabled",
    "handle",
    "protected_resource_document",
    "register",
    "resource",
    "rpc_error",
    "token",
]
