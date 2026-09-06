"""Where this server lives, what it calls itself, and every bound on it.

Its own reader rather than `screener.config.settings`, on the same terms as
`playground.config`: absence is a state and not a fault. With no password for
`playground_mcp` there is no connector, and the right answer to a request is
"not configured here" rather than a 500.
"""

from screener.config import env

# The single MCP endpoint. Both verbs, one path, as the Streamable HTTP
# transport requires.
PATH = "/mcp"

# RFC 9728 and RFC 8414. Claude probes the suffixed protected-resource document
# first and falls back to the bare one, so both are served.
PROTECTED_RESOURCE = "/.well-known/oauth-protected-resource"
AUTHORIZATION_SERVER = "/.well-known/oauth-authorization-server"

REGISTER_PATH = "/oauth/register"
AUTHORIZE_PATH = "/oauth/authorize"
TOKEN_PATH = "/oauth/token"

# One scope. There is one thing this server does and no second level of it, and
# a scope list that never varies is a menu with one item on it.
SCOPE = "screener:read"

# Protocol versions this server will speak. A client's requested version is
# echoed back when it is one of these; otherwise it is answered with the first,
# which the spec says should be the latest we support. A tools-only server is
# unaffected by what separates them.
VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
DEFAULT_VERSION = VERSIONS[1]

SERVER_NAME = "screener"

# Token lifetimes. Claude refreshes reactively on a 401 and proactively up to
# five minutes before expiry, so a short access token costs a round trip rather
# than an interruption; the refresh token is what actually keeps the connector
# alive, and it rotates on every use.
ACCESS_TTL_SECONDS = 3600
REFRESH_TTL_SECONDS = 30 * 24 * 3600

# An authorization code is exchanged within seconds of the consent screen.
# Anything older is a replay, and the spec asks for a short life here.
CODE_TTL_SECONDS = 300

# The JSON-RPC body. Smaller than the console's, because a tools/call carries
# one query and its arguments rather than a pasted analytical statement, and
# `playground.MAX_SQL` still bounds the SQL itself.
MAX_BODY = 16_000
MAX_REGISTER_BODY = 8_000
MAX_FORM_BODY = 4_000

# In-flight tool calls, across every connector session at once.
#
# The engine is connection-per-call with no pool, the status service is a
# ThreadingHTTPServer, and Claude will happily issue several tool calls in
# parallel. Without this the ceiling is Postgres `max_connections`, discovered in
# production as an opaque 503 on an unrelated page. Four is chosen to be
# obviously safe rather than tuned.
MAX_CONCURRENT = 4

# What a tool may hand back to the model in one call. The engine's own
# MAX_PAYLOAD is 1.5MB, which is right for a browser table and far too much for
# a context window.
MAX_TOOL_CHARS = 40_000


def base_url() -> str:
    """The public origin, which is also the OAuth issuer.

    `APP_BASE_URL` is the same variable `screener.auth` builds its redirect from,
    read here rather than imported so this package does not need an AuthConfig to
    answer a metadata request.
    """
    return env.text("APP_BASE_URL", "http://localhost:8080").rstrip("/")


def resource() -> str:
    """The canonical MCP server URI, and the audience every token is bound to.

    RFC 8707 canonical form: lowercase scheme and host, no trailing slash, no
    fragment, and the path included. It has to equal the URL as typed into
    Claude, because that is what Claude sends as `resource` and what this server
    checks a token against.
    """
    return f"{base_url()}{PATH}"


def database_url() -> str | None:
    """The connector's own read-only role, or None when it is switched off.

    A second variable rather than the console's, because this process serves
    both and a single one would make them the same role. `playground_mcp` is
    what `018_mcp.sql` grants, and it is the grant list that decides what
    claude.ai can see.
    """
    return env.optional("PLAYGROUND_MCP_DATABASE_URL")


def enabled() -> bool:
    """The switch, and it is the same one the console has, one role along.

    No password for `playground_mcp` means no URL, which means the endpoint
    reports itself off rather than pretending or erroring.
    """
    return database_url() is not None
