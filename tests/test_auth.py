import json
import threading
import urllib.error
import urllib.request

import httpx
import pytest

from screener import auth
from screener.auth import AuthConfig, GithubUser, OAuthError
from screener.health import build_server

SECRET = "a-session-secret"


# -- the allow-list -------------------------------------------------------


def test_the_allow_list_is_case_insensitive(monkeypatch):
    # GitHub renders a login however its owner typed it, and treats it as
    # case-insensitive, so an allow-list that did not would refuse the right
    # person for a reason nobody could see.
    monkeypatch.setenv("ALLOWED_GITHUB_LOGINS", "ehewes, D1K03")
    config = AuthConfig.from_env()
    assert config.permits("EHEWES")
    assert config.permits("d1k03")


def test_an_empty_allow_list_permits_nobody(monkeypatch):
    # The other reading, that unset means everyone, turns a forgotten variable
    # into an open door.
    monkeypatch.delenv("ALLOWED_GITHUB_LOGINS", raising=False)
    config = AuthConfig.from_env()
    assert not config.permits("ehewes")


def test_sign_in_is_inert_until_every_credential_is_present(monkeypatch):
    monkeypatch.setenv("GITHUB_CLIENT_ID", "cid")
    monkeypatch.delenv("GITHUB_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    assert AuthConfig.from_env().enabled is False


def test_the_client_secret_is_not_printed(monkeypatch):
    config = AuthConfig(client_id="cid", client_secret="shhh", session_secret="also-shhh")
    assert "shhh" not in repr(config)


# -- sessions -------------------------------------------------------------


def test_a_session_round_trips_and_the_raw_token_is_not_stored(fresh_db):
    token = auth.create_session(
        fresh_db, github_id=42, login="ehewes", secret=SECRET, days=1
    )
    assert auth.resolve_session(fresh_db, token, SECRET) == "ehewes"

    with fresh_db.cursor() as cur:
        cur.execute("select token_hash from auth.session")
        stored = bytes(cur.fetchone()[0])
    assert token.encode() not in stored
    assert stored == auth.token_hash(token, SECRET)


def test_a_session_minted_under_another_secret_does_not_resolve(fresh_db):
    # Rotating SESSION_SECRET is the revocation mechanism, so this is the
    # property that makes it one.
    token = auth.create_session(
        fresh_db, github_id=42, login="ehewes", secret=SECRET, days=1
    )
    assert auth.resolve_session(fresh_db, token, "a-different-secret") is None


def test_an_expired_session_does_not_resolve(fresh_db):
    token = auth.create_session(
        fresh_db, github_id=42, login="ehewes", secret=SECRET, days=1
    )
    with fresh_db.cursor() as cur:
        cur.execute("update auth.session set expires_at = now() - interval '1 second'")
    assert auth.resolve_session(fresh_db, token, SECRET) is None


def test_signing_in_again_updates_the_login_rather_than_duplicating_the_user(fresh_db):
    # A GitHub owner can rename their account, so the numeric id is the identity
    # and the login is an attribute of it.
    auth.create_session(fresh_db, github_id=42, login="old-name", secret=SECRET)
    token = auth.create_session(fresh_db, github_id=42, login="new-name", secret=SECRET)
    with fresh_db.cursor() as cur:
        cur.execute("select count(*), max(login) from auth.app_user")
        count, login = cur.fetchone()
    assert (count, login) == (1, "new-name")
    assert auth.resolve_session(fresh_db, token, SECRET) == "new-name"


def test_signing_out_removes_the_session(fresh_db):
    token = auth.create_session(fresh_db, github_id=42, login="ehewes", secret=SECRET)
    auth.delete_session(fresh_db, token, SECRET)
    assert auth.resolve_session(fresh_db, token, SECRET) is None


def test_the_session_cookie_is_not_readable_by_scripts():
    cookie = auth.session_cookie("t", secure=True)
    assert "HttpOnly" in cookie and "Secure" in cookie
    # Lax, not Strict: the OAuth callback is a top-level navigation from
    # github.com and Strict would withhold the cookie on exactly that.
    assert "SameSite=Lax" in cookie


# -- the GitHub exchange --------------------------------------------------


def test_no_scopes_are_requested():
    # The only question is who you are. An unscoped token still reads the public
    # profile, so asking for more would take access this has no use for.
    url = auth.authorize_url("cid", "state", "https://x.test/auth/callback")
    assert "scope=&" in url or url.endswith("scope=")


def test_an_error_object_in_a_two_hundred_is_still_a_failed_exchange():
    # GitHub reports a reused or expired code this way, so the status code
    # alone does not tell you the exchange worked.
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"error": "bad_verification_code"})
    )
    with pytest.raises(OAuthError, match="bad_verification_code"):
        auth.exchange_code(
            "c",
            client_id="cid",
            client_secret="sec",
            redirect_uri="https://x.test/cb",
            transport=transport,
        )


def test_the_user_lookup_returns_the_login_and_the_numeric_id():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"login": "ehewes", "id": 12345})
    )
    assert auth.fetch_user("tok", transport=transport) == GithubUser("ehewes", 12345)


# -- the endpoints --------------------------------------------------------


@pytest.fixture
def signed_in_server(monkeypatch, db_url, fresh_db):
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("GITHUB_CLIENT_ID", "cid")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "sec")
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    monkeypatch.setenv("ALLOWED_GITHUB_LOGINS", "ehewes,D1K03")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8080")

    server = build_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", fresh_db
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(url, cookie=None, redirect=True, data=None):
    # `data` makes it a POST, which is all urllib needs to be told.
    request = urllib.request.Request(url, data=data)
    if cookie:
        request.add_header("Cookie", cookie)
    opener = urllib.request.build_opener()
    if not redirect:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers


def test_status_is_refused_without_a_session(signed_in_server):
    url, _ = signed_in_server
    status, body, _ = _get(url + "/status")
    assert status == 401
    assert "sign in" in json.loads(body)["error"]


def test_status_is_served_to_a_signed_in_user(signed_in_server):
    url, conn = signed_in_server
    token = auth.create_session(conn, github_id=1, login="ehewes", secret=SECRET)
    status, body, _ = _get(url + "/status", cookie=f"{auth.SESSION_COOKIE}={token}")
    assert status == 200
    assert json.loads(body)["login"] == "ehewes"


def test_the_probes_stay_open_because_nothing_probing_them_can_sign_in(signed_in_server):
    # Docker's healthcheck and the deploy smoke test have no cookie to present.
    url, _ = signed_in_server
    assert _get(url + "/health")[0] == 200
    assert _get(url + "/ready")[0] == 200


def test_login_redirects_to_github_and_sets_a_state_cookie(signed_in_server):
    url, _ = signed_in_server
    status, _, headers = _get(url + "/auth/login", redirect=False)
    assert status == 302
    assert headers["Location"].startswith("https://github.com/login/oauth/authorize")
    assert auth.STATE_COOKIE in headers["Set-Cookie"]


def test_a_callback_whose_state_does_not_match_is_rejected(signed_in_server):
    # Without this a third party could hand someone a callback URL and sign
    # them in as an account they do not control.
    url, _ = signed_in_server
    status, body, _ = _get(
        url + "/auth/callback?code=x&state=forged",
        cookie=f"{auth.STATE_COOKIE}=genuine",
    )
    assert status == 400
    assert json.loads(body)["error"] == "state mismatch"


def test_a_login_outside_the_allow_list_is_refused(signed_in_server, monkeypatch):
    url, conn = signed_in_server
    monkeypatch.setattr(auth, "exchange_code", lambda *a, **k: "token")
    monkeypatch.setattr(auth, "fetch_user", lambda *a, **k: GithubUser("stranger", 99))

    status, body, _ = _get(
        url + "/auth/callback?code=x&state=s", cookie=f"{auth.STATE_COOKIE}=s"
    )
    assert status == 403
    assert json.loads(body)["error"] == "not permitted"
    with conn.cursor() as cur:
        cur.execute("select count(*) from auth.app_user")
        assert cur.fetchone()[0] == 0


def test_a_permitted_login_is_given_a_session(signed_in_server, monkeypatch):
    url, conn = signed_in_server
    monkeypatch.setattr(auth, "exchange_code", lambda *a, **k: "token")
    monkeypatch.setattr(auth, "fetch_user", lambda *a, **k: GithubUser("D1K03", 7))

    status, _, headers = _get(
        url + "/auth/callback?code=x&state=s",
        cookie=f"{auth.STATE_COOKIE}=s",
        redirect=False,
    )
    assert status == 302
    assert headers["Location"] == "/"
    with conn.cursor() as cur:
        cur.execute("select login, github_id from auth.app_user")
        assert cur.fetchone() == ("D1K03", 7)


# -- the local development bypass ------------------------------------------


@pytest.fixture
def local_server(monkeypatch, db_url, fresh_db):
    """A server with GitHub sign-in deliberately not configured."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    for name in ("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8080")

    server = build_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", fresh_db
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_local_bypass_does_not_exist_once_github_is_configured(signed_in_server):
    # The safety argument in one test. In production the GitHub credentials
    # come from Infisical, so config.enabled is true and this route is gone.
    # There is no separate flag that could be set wrongly.
    url, _ = signed_in_server
    status, body, _ = _get(url + "/auth/local", redirect=False)
    assert status == 404
    assert json.loads(body)["error"] == "not found"


def test_the_local_bypass_issues_a_real_session(local_server):
    # A genuine session row rather than only a cookie, so local behaves the way
    # production does: /status reports a login and signing out works.
    url, conn = local_server
    status, _, headers = _get(url + "/auth/local", redirect=False)
    assert status == 302
    assert headers["Location"] == "/"

    cookie = headers["Set-Cookie"]
    assert auth.SESSION_COOKIE in cookie

    token = cookie.split("=", 1)[1].split(";", 1)[0]
    assert auth.resolve_session(conn, token, SECRET) == "local-dev"


def test_the_local_user_cannot_collide_with_a_real_github_account(local_server):
    # id 0 and a name that is not a GitHub username, so anyone reading
    # auth.app_user can see at a glance that it was not a sign-in.
    url, conn = local_server
    _get(url + "/auth/local", redirect=False)
    with conn.cursor() as cur:
        cur.execute("select github_id, login from auth.app_user")
        assert cur.fetchone() == (0, "local-dev")


def test_the_local_bypass_refuses_without_a_session_secret(local_server, monkeypatch):
    # Without it there is nothing to sign the session with, and a cookie that
    # never resolves would look like the bypass silently not working.
    url, _ = local_server
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    status, body, _ = _get(url + "/auth/local", redirect=False)
    assert status == 503
    assert "SESSION_SECRET" in json.loads(body)["error"]


# -- authorization does not weaken when its configuration goes missing ------


@pytest.fixture()
def unconfigured_server(monkeypatch, db_url, fresh_db):
    """The server with GitHub sign-in not configured.

    A rotated secret, a typo in Infisical, or a variable that failed to load
    all land here. What must not happen is that the endpoints behind sign-in
    open to the internet because the thing guarding them went missing.
    """
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.delenv("GITHUB_CLIENT_ID", raising=False)
    monkeypatch.delenv("GITHUB_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8080")

    server = build_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", fresh_db
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    "path,data",
    [
        ("/status", None),
        ("/api/ask?q=hi", None),
        ("/api/audit", None),
        ("/api/handoff", None),
        # There is no decorator on these routes — each repeats the same six
        # lines, and forgetting them makes a route public in silence. A POST
        # checked only by a test of its own would be one list away from being
        # the route nobody checked.
        ("/api/transcribe", b"not audio"),
    ],
)
def test_nothing_opens_up_when_sign_in_is_unconfigured(unconfigured_server, path, data):
    # This is the regression that matters. The check used to read
    # `config.enabled and login is None`, so unconfigured sign-in did not
    # refuse anyone — it stopped asking. One missing variable would have put
    # the agent, the spend figures and the Discord ids of everyone who used the
    # bot on the public internet.
    url, _ = unconfigured_server
    status, body, _ = _get(url + path, data=data)
    assert status == 401
    assert "sign in" in json.loads(body)["error"]


def test_a_session_still_works_without_github(unconfigured_server):
    # Local development signs in through /auth/local, so closing the hole must
    # not close the door: a real session is still a real session.
    url, conn = unconfigured_server
    token = auth.create_session(conn, github_id=1, login="local-dev", secret=SECRET)
    status, body, _ = _get(url + "/status", cookie=f"{auth.SESSION_COOKIE}={token}")
    assert status == 200
    assert json.loads(body)["login"] == "local-dev"


# -- uploading a recording --------------------------------------------------


def test_an_upload_over_the_limit_is_refused_before_it_is_forwarded(
    signed_in_server, monkeypatch
):
    # Refused on the Content-Length alone: the body is never sent here and the
    # 413 comes back anyway, which is the proof that nothing was read and the
    # transcriber was never reached. urllib cannot express this — it insists on
    # writing the body it declared, and the server has closed by then.
    import socket

    from screener.health import server as health_server

    url, conn = signed_in_server
    reached: list[bytes] = []
    monkeypatch.setattr(
        "screener.transcribe.transcribe", lambda *a, **k: reached.append(b"") or None
    )
    token = auth.create_session(conn, github_id=1, login="ellis", secret=SECRET)

    host, port = url.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=10) as sock:
        sock.sendall(
            f"POST /api/transcribe HTTP/1.1\r\nHost: {host}\r\n"
            f"Cookie: {auth.SESSION_COOKIE}={token}\r\n"
            f"Content-Length: {health_server.MAX_AUDIO + 1}\r\n\r\n".encode()
        )
        sock.shutdown(socket.SHUT_WR)
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                break
            head += chunk

    assert head.split(b" ")[1] == b"413"
    assert reached == []


def test_a_post_with_no_body_is_a_bad_request_not_a_hang(signed_in_server):
    url, conn = signed_in_server
    token = auth.create_session(conn, github_id=1, login="ellis", secret=SECRET)
    status, body, _ = _get(
        url + "/api/transcribe", cookie=f"{auth.SESSION_COOKIE}={token}", data=b""
    )
    assert status == 400
    assert json.loads(body)["error"] == "no body"


def test_an_unknown_post_path_is_a_json_not_found(signed_in_server):
    url, _ = signed_in_server
    status, body, _ = _get(url + "/api/nowhere", data=b"hi")
    assert status == 404
    assert json.loads(body)["error"] == "not found"


def test_a_recording_comes_back_as_text(signed_in_server, monkeypatch):
    from screener.transcribe import Transcript

    url, conn = signed_in_server
    monkeypatch.setattr(
        "screener.transcribe.transcribe",
        lambda *a, **k: Transcript(text="chart nvidia", seconds=2.0),
    )
    token = auth.create_session(conn, github_id=1, login="ellis", secret=SECRET)
    status, body, _ = _get(
        url + "/api/transcribe",
        cookie=f"{auth.SESSION_COOKIE}={token}",
        data=b"OggS" + b"\x00" * 64,
    )
    assert status == 200
    assert json.loads(body) == {"text": "chart nvidia", "seconds": 2.0}


def test_no_transcript_is_written_into_the_audit_trail(signed_in_server, monkeypatch):
    # Audio is more sensitive than typed text, not less, so the trail records
    # that something was said and how long it took, never what it was.
    from screener.transcribe import Transcript

    url, conn = signed_in_server
    monkeypatch.setattr(
        "screener.transcribe.transcribe",
        lambda *a, **k: Transcript(text="a secret ticker", seconds=2.0),
    )
    token = auth.create_session(conn, github_id=1, login="ellis", secret=SECRET)
    _get(
        url + "/api/transcribe",
        cookie=f"{auth.SESSION_COOKIE}={token}",
        data=b"OggS" + b"\x00" * 64,
    )
    with conn.cursor() as cur:
        cur.execute(
            "select detail::text from audit.event where operation = 'steven.transcribe'"
        )
        rows = cur.fetchall()
    assert rows, "the transcription was not recorded at all"
    assert all("a secret ticker" not in row[0] for row in rows)
