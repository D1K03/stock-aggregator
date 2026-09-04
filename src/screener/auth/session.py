"""Sessions, stored in the database, and the cookie plumbing around them."""

import hmac
import http.cookies
import secrets
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import psycopg

SESSION_COOKIE = "screener_session"
STATE_COOKIE = "screener_oauth_state"


def new_token() -> str:
    """A session token. Random, opaque, and never stored as given."""
    return secrets.token_urlsafe(32)


def new_state() -> str:
    """An unguessable value tying a callback to the login that started it."""
    return secrets.token_urlsafe(24)


def token_hash(token: str, secret: str) -> bytes:
    """What goes in the database.

    HMAC rather than a bare digest so that reading the table is not enough to
    mint a session: an attacker would also need the secret, which lives in the
    environment and not in Postgres.
    """
    return hmac.new(secret.encode(), token.encode(), sha256).digest()


def create_session(
    conn: psycopg.Connection,
    *,
    github_id: int,
    login: str,
    secret: str,
    days: int = 30,
    user_agent: str | None = None,
) -> str:
    """Record the sign-in and return the raw token, the only time it exists."""
    token = new_token()
    expires = datetime.now(UTC) + timedelta(days=days)
    with conn.cursor() as cur:
        # The login is refreshed on every sign-in because GitHub lets an owner
        # change it, and the numeric id is what actually identifies the account.
        cur.execute(
            """
            insert into auth.app_user (github_id, login)
            values (%s, %s)
            on conflict (github_id) do update
                set login = excluded.login, last_seen_at = now()
            returning id
            """,
            (github_id, login),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("could not record the signed-in user")
        cur.execute(
            """
            insert into auth.session (user_id, token_hash, expires_at, user_agent)
            values (%s, %s, %s, %s)
            """,
            (row[0], token_hash(token, secret), expires, user_agent),
        )
    return token


class SessionLookupFailed(RuntimeError):
    """The session could not be checked, as distinct from being invalid.

    Worth its own type so a database outage reports as one rather than as
    "not signed in", which would send someone looking in the wrong place.
    """


def resolve_session(conn: psycopg.Connection, token: str, secret: str) -> str | None:
    """The login this token belongs to, or None if it is not a live session."""
    if not token:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            select u.login
              from auth.session s
              join auth.app_user u on u.id = s.user_id
             where s.token_hash = %s and s.expires_at > now()
            """,
            (token_hash(token, secret),),
        )
        row = cur.fetchone()
    return str(row[0]) if row else None


def delete_session(conn: psycopg.Connection, token: str, secret: str) -> None:
    """Sign out, and sweep anything that has already expired."""
    with conn.cursor() as cur:
        cur.execute("delete from auth.session where token_hash = %s", (token_hash(token, secret),))
        cur.execute("delete from auth.session where expires_at <= now()")


# -- cookies --------------------------------------------------------------


def read_cookie(header: str | None, name: str) -> str | None:
    """One cookie's value out of a raw Cookie header."""
    if not header:
        return None
    jar = http.cookies.SimpleCookie()
    try:
        jar.load(header)
    except http.cookies.CookieError:
        return None
    morsel = jar.get(name)
    return morsel.value if morsel else None


def _set_cookie(name: str, value: str, *, max_age: int, secure: bool) -> str:
    parts = [
        f"{name}={value}",
        "Path=/",
        f"Max-Age={max_age}",
        "HttpOnly",
        # Lax rather than Strict: the OAuth callback is a top-level navigation
        # from github.com, and Strict withholds the cookie on exactly that, so
        # the state check would fail every time.
        "SameSite=Lax",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def session_cookie(token: str, *, days: int = 30, secure: bool = True) -> str:
    return _set_cookie(SESSION_COOKIE, token, max_age=days * 86400, secure=secure)


def state_cookie(state: str, *, secure: bool = True) -> str:
    # Ten minutes is longer than any honest trip through GitHub's consent screen
    # and short enough that a stale value cannot be replayed later.
    return _set_cookie(STATE_COOKIE, state, max_age=600, secure=secure)


def clear_cookie(name: str, *, secure: bool = True) -> str:
    return _set_cookie(name, "", max_age=0, secure=secure)
