"""The `mcp` schema: clients, one-time codes, and tokens.

Never opens a socket and never decides policy — it is handed a connection and a
hash and it reads or writes a row. `oauth.py` owns which of those is allowed.

Every secret arrives here already hashed. The raw token exists once, in the
response that issued it, exactly as `auth.session` arranges for a browser
session, and rotating `SESSION_SECRET` invalidates connectors and browser
sessions together.
"""

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Client:
    client_id: str
    client_name: str | None
    redirect_uris: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Grant:
    """A redeemed authorization code, and everything the token inherits from it."""

    client_id: str
    redirect_uri: str
    code_challenge: str
    login: str
    resource: str
    scope: str


@dataclass(frozen=True, slots=True)
class Token:
    """What a presented bearer token turns out to be."""

    family_id: str
    login: str
    client_id: str
    resource: str
    scope: str


def new_client_id() -> str:
    return secrets.token_urlsafe(18)


def register(
    conn: psycopg.Connection,
    *,
    client_name: str | None,
    redirect_uris: tuple[str, ...],
) -> str:
    """Record a dynamically registered client and return its id."""
    client_id = new_client_id()
    conn.execute(
        "insert into mcp.client (client_id, client_name, redirect_uris) "
        "values (%s, %s, %s)",
        [client_id, client_name, list(redirect_uris)],
    )
    return client_id


def client(conn: psycopg.Connection, client_id: str) -> Client | None:
    row = conn.execute(
        "select client_id, client_name, redirect_uris from mcp.client "
        "where client_id = %s",
        [client_id],
    ).fetchone()
    if row is None:
        return None
    return Client(client_id=row[0], client_name=row[1], redirect_uris=tuple(row[2]))


def save_code(
    conn: psycopg.Connection,
    *,
    code_hash: bytes,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    login: str,
    resource: str,
    scope: str,
    ttl_seconds: int,
) -> None:
    conn.execute(
        "insert into mcp.authorization (code_hash, client_id, redirect_uri, "
        "code_challenge, login, resource, scope, expires_at) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s)",
        [
            code_hash,
            client_id,
            redirect_uri,
            code_challenge,
            login,
            resource,
            scope,
            datetime.now(UTC) + timedelta(seconds=ttl_seconds),
        ],
    )


def claim_code(conn: psycopg.Connection, code_hash: bytes) -> Grant | None:
    """Redeem a code, once, and never twice.

    One statement, not a select followed by an update. The status service runs a
    thread per connection, so two simultaneous redemptions of the same code
    would both pass a separate `used_at is null` check and both be issued a
    token. `update ... where used_at is null returning ...` makes Postgres the
    thing that decides, and exactly one caller gets a row.

    An expired code is left for the same reason a used one is: so it can be
    refused rather than merely missing.
    """
    row = conn.execute(
        """
        update mcp.authorization
           set used_at = now()
         where code_hash = %s and used_at is null and expires_at > now()
        returning client_id, redirect_uri, code_challenge, login, resource, scope
        """,
        [code_hash],
    ).fetchone()
    if row is None:
        return None
    return Grant(
        client_id=row[0],
        redirect_uri=row[1],
        code_challenge=row[2],
        login=row[3],
        resource=row[4],
        scope=row[5],
    )


def new_family_id() -> str:
    return secrets.token_urlsafe(12)


def save_token(
    conn: psycopg.Connection,
    *,
    family_id: str,
    token_hash: bytes,
    refresh_hash: bytes,
    client_id: str,
    login: str,
    resource: str,
    scope: str,
    access_ttl: int,
    refresh_ttl: int,
) -> None:
    now = datetime.now(UTC)
    conn.execute(
        "insert into mcp.token (family_id, token_hash, refresh_hash, client_id, "
        "login, resource, scope, expires_at, refresh_expires_at) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        [
            family_id,
            token_hash,
            refresh_hash,
            client_id,
            login,
            resource,
            scope,
            now + timedelta(seconds=access_ttl),
            now + timedelta(seconds=refresh_ttl),
        ],
    )


def resolve(conn: psycopg.Connection, token_hash: bytes) -> Token | None:
    """Who a live access token belongs to, or None.

    `last_used_at` is stamped on the way through, which is the only thing that
    distinguishes a connector somebody uses from one they added and forgot.
    """
    row = conn.execute(
        """
        update mcp.token set last_used_at = now()
         where token_hash = %s and expires_at > now() and revoked_at is null
        returning family_id, login, client_id, resource, scope
        """,
        [token_hash],
    ).fetchone()
    if row is None:
        return None
    return Token(
        family_id=row[0], login=row[1], client_id=row[2], resource=row[3], scope=row[4]
    )


def claim_refresh(conn: psycopg.Connection, refresh_hash: bytes) -> Token | None:
    """Spend a refresh token once, and revoke the family if it is spent twice.

    Two statements, and the second is the point. The first claims the token —
    one statement, so two simultaneous redemptions cannot both win. If it
    matches nothing, the second asks whether this value was *ever* issued: if it
    was, somebody is replaying a refresh token that has already been rotated,
    and the only safe reading is that the chain is compromised at one end or the
    other. Every token in the family is revoked, which logs the real client out
    and turns a silent theft into a visible reconnect.

    That is why the row is marked rather than deleted. A deleted row cannot tell
    a replay from a value that never existed, and rotation without that
    distinction detects nothing at all.
    """
    row = conn.execute(
        """
        update mcp.token set used_at = now()
         where refresh_hash = %s and used_at is null and revoked_at is null
           and refresh_expires_at > now()
        returning family_id, login, client_id, resource, scope
        """,
        [refresh_hash],
    ).fetchone()
    if row is not None:
        return Token(
            family_id=row[0], login=row[1], client_id=row[2], resource=row[3], scope=row[4]
        )

    revoked = conn.execute(
        """
        update mcp.token set revoked_at = now()
         where revoked_at is null and family_id = (
             select family_id from mcp.token where refresh_hash = %s
         )
        returning id
        """,
        [refresh_hash],
    ).fetchall()
    if revoked:
        logger.warning(
            "mcp: a rotated refresh token was replayed; revoked %d token(s)",
            len(revoked),
        )
    return None


def revoke_login(conn: psycopg.Connection, login: str) -> int:
    """Disconnect every connector belonging to one person."""
    rows = conn.execute(
        "update mcp.token set revoked_at = now() "
        "where login = %s and revoked_at is null returning id",
        [login],
    ).fetchall()
    return len(rows)


def forget_expired(conn: psycopg.Connection) -> None:
    """Sweep dead rows. Cheap, and called where a write already happens.

    Nothing here is worth keeping once it cannot be used: an expired code is not
    evidence and a dead token is not history. What either of them *did* is in the
    audit trail, which is the thing that is meant to last.
    """
    conn.execute("delete from mcp.authorization where expires_at < now()")
    conn.execute(
        "delete from mcp.token where coalesce(refresh_expires_at, expires_at) < now()"
    )
    # A client nothing points at any more. Registration is unauthenticated, so
    # without this the table grows with every reconnection for ever.
    conn.execute(
        "delete from mcp.client c where not exists "
        "(select 1 from mcp.token t where t.client_id = c.client_id) "
        "and not exists (select 1 from mcp.authorization a where a.client_id = c.client_id) "
        "and c.created_at < now() - interval '1 day'"
    )
