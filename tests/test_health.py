import json
import threading
import urllib.error
import urllib.request

import pytest

from screener.health import build_server


@pytest.fixture
def server_url():
    """A real server on an ephemeral port, in a thread."""
    server = build_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def get(url, cookie=None):
    request = urllib.request.Request(url)
    if cookie:
        request.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_health_answers_without_a_database(server_url, monkeypatch):
    # This is what the container healthcheck hits. If it ever consults
    # Postgres, a database blip restarts a healthy container repeatedly — and
    # since settings() raises without DATABASE_URL, this test is what notices.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert get(server_url + "/health") == (200, {"status": "ok"})


def test_status_is_not_served_without_a_database(server_url, monkeypatch):
    """/status needs a session, and checking one needs the database.

    This used to assert a 200 with the build SHA, on the argument that the
    endpoint naming the running build must not go dark when something is
    wrong. That was a good argument resting on a bad mechanism: it only
    returned 200 because sign-in was unconfigured in this fixture, and the
    check read `config.enabled and login is None` — so the endpoint was open to
    anyone, not merely available to an operator.

    Closing that means /status is unreachable while Postgres is, which is the
    right trade: /health still proves the process is alive, /ready still names
    the database as the fault, and the running build is on the box as an image
    tag. Being told which commit is deployed is not worth an endpoint that
    stops asking who you are.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # Set explicitly, not inherited. Without a session secret the server cannot
    # verify a cookie at all and answers 401 without reaching the database — a
    # real behaviour, but not the one under test, and leaving it to the ambient
    # environment is how this passed locally and failed in CI.
    monkeypatch.setenv("SESSION_SECRET", "a-secret-for-this-test")

    # No cookie: refused before the database is ever consulted, so a stranger
    # gets the same answer whether Postgres is up or not.
    status, body = get(server_url + "/status")
    assert status == 401
    assert "sign in" in body["error"]

    # A cookie that cannot be checked, because the lookup needs the database.
    # Distinct from a 401 on purpose: "I do not know who you are" and "I could
    # not find out" are different problems and want different responses.
    status, body = get(server_url + "/status", cookie="screener_session=whatever")
    assert status == 503
    assert body["error"] == "cannot check the session"


def test_ready_reports_unavailable_without_a_database(server_url, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    status, body = get(server_url + "/ready")
    assert status == 503
    assert body["database"] == "unconfigured"


def test_an_unknown_path_is_a_json_not_found(server_url):
    assert get(server_url + "/admin") == (404, {"error": "not found"})


def test_every_response_carries_a_content_length(server_url):
    # HTTP/1.1 is set so cloudflared can keep the connection alive; a 1.1
    # response with neither a length nor chunked encoding leaves the client
    # waiting for the socket to close.
    with urllib.request.urlopen(server_url + "/health", timeout=5) as response:
        assert response.headers["Content-Length"] == str(len(response.read()))


def test_ready_reports_the_applied_migration_count(server_url, db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url)
    status, body = get(server_url + "/ready")
    assert status == 200
    assert body["database"] in {"ok", "no schema"}
    assert isinstance(body["migrations"], int)
