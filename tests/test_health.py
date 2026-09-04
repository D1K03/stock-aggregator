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


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_health_answers_without_a_database(server_url, monkeypatch):
    # This is what the container healthcheck hits. If it ever consults
    # Postgres, a database blip restarts a healthy container repeatedly — and
    # since settings() raises without DATABASE_URL, this test is what notices.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert get(server_url + "/health") == (200, {"status": "ok"})


def test_status_answers_when_the_database_is_unreachable(server_url, monkeypatch):
    # The one endpoint that can say which build is running must not go dark
    # exactly when something is wrong.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    status, body = get(server_url + "/status")
    assert status == 200
    assert "git_sha" in body
    assert body["uptime_seconds"] >= 0


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
