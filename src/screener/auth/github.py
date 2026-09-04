"""The GitHub end of the OAuth exchange."""

import urllib.parse
from dataclasses import dataclass

import httpx

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"

TIMEOUT = 15.0


class OAuthError(RuntimeError):
    """The exchange failed, or GitHub answered with something unusable."""


@dataclass(frozen=True, slots=True)
class GithubUser:
    login: str
    user_id: int


def authorize_url(client_id: str, state: str, redirect_uri: str) -> str:
    """Where to send someone to sign in.

    No scopes are requested. The only question being asked is who you are, and
    an unscoped token still reads the public profile, so asking for more would
    be taking access this has no use for.
    """
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": "",
            "allow_signup": "false",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def exchange_code(
    code: str,
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Trade the callback code for an access token."""
    try:
        with httpx.Client(timeout=TIMEOUT, transport=transport) as client:
            response = client.post(
                TOKEN_URL,
                headers={"Accept": "application/json"},
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
            )
    except httpx.HTTPError as exc:
        raise OAuthError(f"token exchange failed: {exc}") from exc

    if response.status_code >= 400:
        raise OAuthError(f"token exchange returned HTTP {response.status_code}")

    body = response.json()
    if "error" in body:
        # GitHub reports a bad or reused code as a 200 with an error object, so
        # the status alone does not tell you the exchange worked.
        raise OAuthError(f"token exchange rejected: {body['error']}")
    token = body.get("access_token")
    if not token:
        raise OAuthError("token exchange returned no access token")
    return str(token)


def fetch_user(
    access_token: str, *, transport: httpx.BaseTransport | None = None
) -> GithubUser:
    """Who the token belongs to."""
    try:
        with httpx.Client(timeout=TIMEOUT, transport=transport) as client:
            response = client.get(
                USER_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                },
            )
    except httpx.HTTPError as exc:
        raise OAuthError(f"user lookup failed: {exc}") from exc

    if response.status_code >= 400:
        raise OAuthError(f"user lookup returned HTTP {response.status_code}")

    body = response.json()
    login, user_id = body.get("login"), body.get("id")
    if not login or user_id is None:
        raise OAuthError("user lookup returned no login")
    return GithubUser(login=str(login), user_id=int(user_id))
