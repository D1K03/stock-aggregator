"""OAuth 2.1 for the connector: discovery, registration, consent, tokens.

This deployment becomes both the resource server and the authorization server,
which sounds like more than it is. There is no new identity: the consent step is
the GitHub session the dashboard already issues, so "who is this" was answered
before any of this ran, and all that happens here is turning that answer into a
token bound to one client and one audience.

Why OAuth at all, when a pasted API key would be a tenth of the code: claude.ai
does not reliably offer that. Its `static_headers` mode is beta and limited to
selected organisations, and the alternative it *does* support out of the box is
`oauth_dcr` — OAuth 2.0 with dynamic client registration. Authless was the third
option and is not one, because the thing behind this endpoint is a database.

Each function returns a `Response` rather than writing to a socket, so the HTTP
handler stays four lines and every rule here is testable without one.
"""

import base64
import hashlib
import html
import json
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import psycopg

from screener import auth
from screener.config import settings
from screener.mcp import config, store

logger = logging.getLogger(__name__)

# Where Claude's hosted surfaces send an authorization code back to. Documented
# by Anthropic and stable; a registration naming anything else is either Claude
# Code on a loopback port or somebody who should not be here.
CLAUDE_REDIRECTS = ("https://claude.ai/api/mcp/auth_callback",)

# The local sign-in `screener.health` offers when GitHub is not configured. It
# mints a real session without proving anything, which is right for a laptop and
# must never be a way to mint a connector token — so it is refused here by name
# rather than left to depend on the allow-list being sensible.
LOCAL_LOGIN = "local-dev"

# RFC 6749 §4.1.2.1 and §5.2 codes. Claude keys its retry behaviour off these —
# a refresh that has expired must be `invalid_grant` and not a custom string, or
# the client will not know to start a new authorization.
INVALID_REQUEST = "invalid_request"
INVALID_CLIENT = "invalid_client"
INVALID_GRANT = "invalid_grant"
UNSUPPORTED_GRANT = "unsupported_grant_type"


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: bytes
    content_type: str = "application/json"
    headers: tuple[tuple[str, str], ...] = field(default=())


class Unauthorized(RuntimeError):
    """No usable bearer token. Carries the WWW-Authenticate the client needs.

    A 401 from this server is not a dead end; it is the first step of discovery.
    Claude reads `resource_metadata` off the header to find out where to
    authorize, and will not look at the header on any other status — so this
    exists to make sure the pointer travels with the refusal.
    """

    def __init__(self, detail: str = "invalid_token") -> None:
        super().__init__(detail)
        self.detail = detail

    def header(self) -> str:
        return (
            f'Bearer resource_metadata="{config.base_url()}'
            f'{config.PROTECTED_RESOURCE}", error="{self.detail}"'
        )


def _json(payload: dict[str, Any], status: int = 200) -> Response:
    # RFC 6749 §5.1 requires no-store on a token response, and there is nothing
    # here worth caching anyway.
    return Response(
        status,
        json.dumps(payload).encode(),
        headers=(("Cache-Control", "no-store"),),
    )


def _error(code: str, description: str, status: int = 400) -> Response:
    return _json({"error": code, "error_description": description}, status)


def _page(title: str, body: str, status: int = 200) -> Response:
    """A whole HTML page, inline, because there are two of them.

    No template engine and no static assets: this is the only HTML this service
    serves, it is seen once per connector, and a stylesheet would have to be
    routed and cached to make a button look like the dashboard's.
    """
    return Response(
        status,
        (
            "<!doctype html><html lang=en><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title>"
            "<style>"
            "body{background:#0f1012;color:#e8e6e3;font:15px/1.6 ui-sans-serif,system-ui,sans-serif;"
            "display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}"
            "main{max-width:26rem;padding:2rem}"
            "h1{font-size:1.1rem;margin:0 0 .75rem}"
            "p{color:#a8a49e;margin:.5rem 0}"
            "ul{color:#a8a49e;padding-left:1.1rem}"
            "code{color:#c98a5b}"
            "button,a.btn{background:#c98a5b;color:#17181a;border:0;border-radius:6px;"
            "padding:.6rem 1.1rem;font:inherit;font-weight:600;cursor:pointer;"
            "text-decoration:none;display:inline-block;margin-top:1rem}"
            "</style>"
            f"<main>{body}</main></html>"
        ).encode(),
        content_type="text/html; charset=utf-8",
    )


def _redirect(location: str) -> Response:
    return Response(302, b"", headers=(("Location", location),))


def _connect() -> psycopg.Connection:
    return psycopg.connect(settings().database_url, connect_timeout=3, autocommit=True)


# -- discovery ---------------------------------------------------------------


def protected_resource_document() -> Response:
    """RFC 9728. Where a client learns which authorization server to ask.

    `resource` must equal the MCP URL exactly as it is typed into Claude, path
    and all, because that same string is what Claude sends as the `resource`
    parameter and what every token here is checked against.
    """
    return _json(
        {
            "resource": config.resource(),
            "authorization_servers": [config.base_url()],
            "scopes_supported": [config.SCOPE],
            "bearer_methods_supported": ["header"],
        }
    )


def authorization_server_document() -> Response:
    """RFC 8414.

    Two entries are load-bearing rather than decorative. `S256` in
    `code_challenge_methods_supported`, because Claude sends a PKCE challenge on
    every authorization request and checks first that we advertise support. And
    `none` in `token_endpoint_auth_methods_supported`, because a dynamically
    registered client here is a public client with no secret to authenticate
    with.
    """
    base = config.base_url()
    return _json(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}{config.AUTHORIZE_PATH}",
            "token_endpoint": f"{base}{config.TOKEN_PATH}",
            "registration_endpoint": f"{base}{config.REGISTER_PATH}",
            "scopes_supported": [config.SCOPE],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
        }
    )


# -- registration ------------------------------------------------------------


def _redirect_allowed(uri: str) -> bool:
    """Whether a client may register this callback address.

    **This function is the whole security of the authorization endpoint**, and it
    is worth spelling out the attack it stops, because the obvious design does
    not stop it.

    Registration is unauthenticated — it has to be, that is what dynamic client
    registration means. The tempting conclusion is that registration is harmless
    because a client is inert until somebody approves it. It is not. An attacker
    registers a client called "Claude" whose redirect is their own server, sends
    Ellis a link to `/oauth/authorize`, and because the session cookie is
    `SameSite=Lax` a top-level navigation carries it: he is signed in, he is on
    the allow-list, and he sees a consent page that says Claude. One click and
    the authorization code is delivered to the attacker. PKCE does not help,
    because the attacker chose the challenge.

    So the redirect address is checked against a list here rather than merely
    matched against whatever the client registered for itself. Anything else is
    a self-signed permission slip.
    """
    if uri in CLAUDE_REDIRECTS:
        return True
    # RFC 8252 loopback, for Claude Code and the MCP Inspector, which bind an
    # ephemeral port. The port is ignored by design; the host is not.
    parsed = urlparse(uri)
    return (
        parsed.scheme == "http"
        and parsed.hostname in ("localhost", "127.0.0.1", "::1")
        and not parsed.query
        and not parsed.fragment
    )


def register(body: bytes) -> Response:
    """RFC 7591 dynamic client registration.

    Open by necessity, bounded by `_redirect_allowed`. Claude registers a fresh
    client on every new connection, so this cannot ask who is calling — but it
    can refuse to remember an address nobody legitimate would use, which is the
    part that matters.

    Note the content type. This endpoint takes JSON per RFC 7591 §3.1 while the
    token endpoint takes form encoding per RFC 6749 §4.1.3, which is a genuine
    inconsistency in the specs and a common way to get a 415.
    """
    try:
        payload = json.loads(body)
        uris = tuple(str(u) for u in payload["redirect_uris"])
    except Exception:
        return _error(INVALID_REQUEST, "redirect_uris is required")
    if not uris:
        return _error(INVALID_REQUEST, "redirect_uris is required")
    refused = [u for u in uris if not _redirect_allowed(u)]
    if refused:
        logger.warning("mcp: refused registration for redirect %r", refused[0])
        return _error(
            INVALID_REQUEST,
            "redirect_uris must be a Claude callback or a loopback address",
        )

    name = payload.get("client_name")
    with _connect() as conn:
        store.forget_expired(conn)
        client_id = store.register(
            conn, client_name=str(name) if name else None, redirect_uris=uris
        )
    logger.info("mcp: registered client %s (%s)", client_id, name)
    return _json(
        {
            "client_id": client_id,
            "client_name": name,
            "redirect_uris": list(uris),
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        201,
    )


# -- authorization -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Ask:
    client: store.Client
    redirect_uri: str
    state: str
    challenge: str
    scope: str
    resource: str


def _read_ask(params: dict[str, list[str]], conn: psycopg.Connection) -> _Ask | Response:
    """Validate an authorization request, or say why not.

    The order matters and is the one place this file follows the spec to the
    letter. Until the client and its redirect URI are known good, an error is
    *rendered*, never redirected — bouncing to an unverified redirect URI is how
    an open redirect turns into a stolen authorization code. Afterwards, errors
    go back to the client as the spec expects.
    """
    one = {k: v[0] for k, v in params.items() if v}
    client_id = one.get("client_id", "")
    redirect_uri = one.get("redirect_uri", "")

    client = store.client(conn, client_id) if client_id else None
    if client is None:
        return _page("Unknown client", "<h1>Unknown client</h1><p>Register first.</p>", 400)
    if redirect_uri not in client.redirect_uris:
        return _page(
            "Bad redirect",
            "<h1>That redirect address is not registered</h1>"
            "<p>Nothing was sent to it.</p>",
            400,
        )

    state = one.get("state", "")
    if one.get("response_type") != "code":
        return _bounce(redirect_uri, state, INVALID_REQUEST, "response_type must be code")
    if one.get("code_challenge_method") != "S256":
        return _bounce(redirect_uri, state, INVALID_REQUEST, "PKCE S256 is required")
    challenge = one.get("code_challenge", "")
    if not challenge:
        return _bounce(redirect_uri, state, INVALID_REQUEST, "code_challenge is required")

    return _Ask(
        client=client,
        redirect_uri=redirect_uri,
        state=state,
        challenge=challenge,
        scope=one.get("scope") or config.SCOPE,
        # RFC 8707. Claude always sends it; a client that does not gets this
        # server's own canonical URI, which is the only audience it would be
        # issued a token for anyway.
        resource=one.get("resource") or config.resource(),
    )


def _bounce(redirect_uri: str, state: str, code: str, description: str) -> Response:
    query = {"error": code, "error_description": description}
    if state:
        query["state"] = state
    joiner = "&" if "?" in redirect_uri else "?"
    return _redirect(f"{redirect_uri}{joiner}{urlencode(query)}")


def authorize(
    query: str, login: str | None, permitted: bool, *, secret: str | None
) -> Response:
    """The consent screen, or the reason there is not one.

    Signed out is a redirect into the existing GitHub flow carrying `next`, so
    the round trip ends back here rather than on the dashboard home page. Signed
    in but not on `ALLOWED_GITHUB_LOGINS` is a flat refusal: this is the same
    allow-list that guards every other page, and a connector must not be a way
    around it.
    """
    if secret is None:
        return _page(
            "Not configured",
            "<h1>Sign-in is not configured here</h1>"
            "<p>This deployment cannot issue a connector token.</p>",
            503,
        )
    if login is None:
        target = f"{config.AUTHORIZE_PATH}?{query}"
        return _redirect(f"/auth/login?{urlencode({'next': target})}")
    if not permitted or login == LOCAL_LOGIN:
        return _page(
            "Not permitted",
            f"<h1>{html.escape(login)} is not on the allow-list</h1>"
            "<p>Ask for access, then try again.</p>",
            403,
        )

    with _connect() as conn:
        ask = _read_ask(parse_qs(query), conn)
    if isinstance(ask, Response):
        return ask

    # The name is the client's own claim about itself and anybody can register
    # one; the origin is the only thing on this page that was checked. Both are
    # shown, with the address given the weight, so "Claude" over an address
    # that is not claude.ai reads as the contradiction it is.
    name = html.escape(ask.client.client_name or "An application")
    where = html.escape(urlparse(ask.redirect_uri).netloc or ask.redirect_uri)
    return _page(
        "Connect to the screener",
        f"<h1>{name} wants to read your screener data</h1>"
        f"<p>It will receive the authorization at <code>{where}</code>. "
        "If you do not recognise that address, close this page.</p>"
        f"<p>Signed in as <code>{html.escape(login)}</code>.</p>"
        "<p>If you approve, it will be able to:</p>"
        "<ul><li>run read-only SQL over prices, fundamentals, scores and alerts</li>"
        "<li>read Reddit posts and comments the screener has collected</li>"
        "<li>read live stream transcripts</li></ul>"
        "<p>It cannot write anything, and it cannot see sign-in or the audit trail. "
        "Every query it runs is recorded.</p>"
        f"<form method=post action='{html.escape(config.AUTHORIZE_PATH)}'>"
        f"<input type=hidden name=payload value='{html.escape(query)}'>"
        "<button type=submit>Approve</button></form>",
    )


def approve(form: dict[str, list[str]], login: str, permitted: bool) -> Response:
    """Issue the one-time code, having been told yes.

    No CSRF token, and the reason is the session cookie rather than an omission:
    it is `SameSite=Lax`, which browsers do not attach to a cross-site POST. A
    forged consent therefore arrives with no session, `login` is None, and this
    refuses. The check below is what makes that true rather than assumed.
    """
    if not login or not permitted or login == LOCAL_LOGIN:
        return _page("Not permitted", "<h1>Not permitted</h1>", 403)

    payload = (form.get("payload") or [""])[0]
    code = secrets.token_urlsafe(32)
    with _connect() as conn:
        ask = _read_ask(parse_qs(payload), conn)
        if isinstance(ask, Response):
            return ask
        store.save_code(
            conn,
            code_hash=_hash(code, CODE),
            client_id=ask.client.client_id,
            redirect_uri=ask.redirect_uri,
            code_challenge=ask.challenge,
            login=login,
            resource=ask.resource,
            scope=ask.scope,
            ttl_seconds=config.CODE_TTL_SECONDS,
        )

    query = {"code": code}
    if ask.state:
        query["state"] = ask.state
    joiner = "&" if "?" in ask.redirect_uri else "?"
    logger.info("mcp: issued a code to %s for %s", ask.client.client_id, login)
    return _redirect(f"{ask.redirect_uri}{joiner}{urlencode(query)}")


# -- tokens ------------------------------------------------------------------


def token(body: bytes) -> Response:
    """The token endpoint. Form-encoded, per RFC 6749 §4.1.3.

    Both grants issue a *new* refresh token and invalidate the old one, which
    OAuth 2.1 requires for a public client. A refresh token here works exactly
    once.
    """
    try:
        form = parse_qs(body.decode())
    except UnicodeDecodeError:
        return _error(INVALID_REQUEST, "body was not form-encoded")
    one = {k: v[0] for k, v in form.items() if v}
    grant_type = one.get("grant_type")

    if grant_type == "authorization_code":
        return _exchange_code(one)
    if grant_type == "refresh_token":
        return _exchange_refresh(one)
    return _error(UNSUPPORTED_GRANT, "authorization_code or refresh_token", 400)


def _exchange_code(one: dict[str, str]) -> Response:
    code = one.get("code", "")
    verifier = one.get("code_verifier", "")
    if not code or not verifier:
        return _error(INVALID_REQUEST, "code and code_verifier are required")

    with _connect() as conn:
        grant = store.claim_code(conn, _hash(code, CODE))
        if grant is None:
            # Used, expired or never existed — all one answer, because telling
            # them apart tells an attacker which codes were real.
            return _error(INVALID_GRANT, "that code is not usable")
        if one.get("client_id") and one["client_id"] != grant.client_id:
            return _error(INVALID_CLIENT, "wrong client for that code")
        if one.get("redirect_uri") and one["redirect_uri"] != grant.redirect_uri:
            return _error(INVALID_GRANT, "redirect_uri does not match")
        if not _pkce_ok(verifier, grant.code_challenge):
            return _error(INVALID_GRANT, "PKCE verification failed")
        return _issue(conn, grant.client_id, grant.login, grant.resource, grant.scope)


def _exchange_refresh(one: dict[str, str]) -> Response:
    presented = one.get("refresh_token", "")
    if not presented:
        return _error(INVALID_REQUEST, "refresh_token is required")
    with _connect() as conn:
        old = store.claim_refresh(conn, _hash(presented, REFRESH))
        if old is None:
            return _error(INVALID_GRANT, "that refresh token is not usable")
        if one.get("client_id") and one["client_id"] != old.client_id:
            return _error(INVALID_CLIENT, "wrong client for that refresh token")
        # Same family, so a later replay of any link in the chain revokes it all.
        return _issue(
            conn, old.client_id, old.login, old.resource, old.scope, old.family_id
        )


def _issue(
    conn: psycopg.Connection,
    client_id: str,
    login: str,
    resource: str,
    scope: str,
    family_id: str = "",
) -> Response:
    access = secrets.token_urlsafe(32)
    refresh = secrets.token_urlsafe(32)
    store.save_token(
        conn,
        family_id=family_id or store.new_family_id(),
        token_hash=_hash(access, ACCESS),
        refresh_hash=_hash(refresh, REFRESH),
        client_id=client_id,
        login=login,
        resource=resource,
        scope=scope,
        access_ttl=config.ACCESS_TTL_SECONDS,
        refresh_ttl=config.REFRESH_TTL_SECONDS,
    )
    return _json(
        {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": config.ACCESS_TTL_SECONDS,
            "refresh_token": refresh,
            "scope": scope,
        }
    )


def bearer(header: str | None) -> store.Token:
    """Who is calling, or `Unauthorized`.

    The audience check is the part that is easy to leave out and must not be.
    Without it this server would accept any token this server ever issued for
    any resource, which is the "token passthrough" the MCP spec forbids by name.
    A token says which audience it was minted for, and that has to be us.
    """
    if not header or not header.lower().startswith("bearer "):
        raise Unauthorized("no bearer token")
    presented = header[7:].strip()
    if not presented:
        raise Unauthorized("no bearer token")

    with _connect() as conn:
        found = store.resolve(conn, _hash(presented, ACCESS))
    if found is None:
        raise Unauthorized("expired or unknown token")
    if found.resource != config.resource():
        raise Unauthorized("token was issued for another resource")

    # Re-checked here, not trusted from the moment it was issued. A token
    # outlives a session — thirty days of refreshes — so without this, taking
    # somebody off `ALLOWED_GITHUB_LOGINS` would revoke their dashboard and
    # leave their connector reading the database. The allow-list has to mean the
    # same thing on every surface or it does not mean much on any of them.
    config_ = auth.AuthConfig.from_env()
    if found.login == LOCAL_LOGIN or not config_.permits(found.login):
        raise Unauthorized("that login is no longer permitted")
    return found


# -- helpers -----------------------------------------------------------------


# Domain separation for the shared secret. Without these labels an access token
# hashes to exactly what a browser session would, and a row moved between the
# two tables would be a working credential in the other place.
ACCESS = "mcp-access"
REFRESH = "mcp-refresh"
CODE = "mcp-code"


def _hash(value: str, purpose: str) -> bytes:
    """HMAC under SESSION_SECRET, the same way `auth.session` hashes a session.

    One secret rather than two, deliberately. Rotating it already logs everybody
    out of the dashboard; having it also disconnect every connector is the
    behaviour somebody rotating a leaked secret wants, and two secrets that can
    drift apart is a state where half the credentials are still live.
    """
    secret = auth.AuthConfig.from_env().session_secret
    if secret is None:
        raise Unauthorized("sign-in is not configured")
    return auth.token_hash(value, secret, purpose=purpose)


def _pkce_ok(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return secrets.compare_digest(expected, challenge)
