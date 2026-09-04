"""GitHub sign-in for the status service.

Three endpoints and a session cookie. `/auth/login` sends you to GitHub,
`/auth/callback` checks who came back against `ALLOWED_GITHUB_LOGINS`, and
`/status` then requires the cookie it sets.

Sign-in state lives in its own `auth` schema, not in `public` alongside the
screener's own tables: nothing here is a fact or a score, no scoring query joins
to it, and the two have different lifetimes.
"""

from screener.auth.config import AuthConfig
from screener.auth.github import (
    GithubUser,
    OAuthError,
    authorize_url,
    exchange_code,
    fetch_user,
)
from screener.auth.session import (
    SESSION_COOKIE,
    STATE_COOKIE,
    SessionLookupFailed,
    clear_cookie,
    create_session,
    delete_session,
    new_state,
    new_token,
    read_cookie,
    resolve_session,
    session_cookie,
    state_cookie,
    token_hash,
)

__all__ = [
    "AuthConfig",
    "GithubUser",
    "OAuthError",
    "SESSION_COOKIE",
    "STATE_COOKIE",
    "SessionLookupFailed",
    "authorize_url",
    "clear_cookie",
    "create_session",
    "delete_session",
    "exchange_code",
    "fetch_user",
    "new_state",
    "new_token",
    "read_cookie",
    "resolve_session",
    "session_cookie",
    "state_cookie",
    "token_hash",
]
