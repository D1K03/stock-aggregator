"""The `/api/skybird/*` routes on the status service.

The tests that matter most are the first ones: every route requires a session
unconditionally. Everything else here is shape.
"""

import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime

import pytest

from screener import auth
from screener.health import build_server
from screener.skybird import store

SECRET = "a-session-secret"
LIVE = "https://www.youtube.com/watch?v=jNQXAC9IVRw"

ROUTES = [
    ("GET", "/api/skybird", None),
    ("GET", "/api/skybird/transcript?session=1", None),
    ("POST", "/api/skybird/start", {"url": LIVE}),
    ("POST", "/api/skybird/stop", {"id": 1}),
    ("POST", "/api/skybird/pause", {"id": 1}),
    ("POST", "/api/skybird/resume", {"id": 1}),
    ("POST", "/api/skybird/delete", {"id": 1}),
]


@pytest.fixture
def server(monkeypatch, db_url, fresh_db):
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("GITHUB_CLIENT_ID", "cid")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "sec")
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    monkeypatch.setenv("ALLOWED_GITHUB_LOGINS", "ehewes")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8080")
    monkeypatch.delenv("SKYBIRD_MAX_SESSIONS", raising=False)
    monkeypatch.delenv("SKYBIRD_CHUNK_SECONDS", raising=False)

    built = build_server("127.0.0.1", 0)
    thread = threading.Thread(target=built.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{built.server_address[1]}", fresh_db
    finally:
        built.shutdown()
        built.server_close()
        thread.join(timeout=5)


def _call(url, method="GET", body=None, cookie=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if cookie:
        request.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw) if raw else {}


@pytest.fixture
def signed_in(server):
    url, conn = server
    token = auth.create_session(conn, github_id=1, login="ehewes", secret=SECRET)
    return url, conn, f"{auth.SESSION_COOKIE}={token}"


# -- the check that matters -------------------------------------------------


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_every_route_is_refused_without_a_session(server, method, path, body):
    # Unconditionally, not `config.enabled and login is None` — the check that
    # opens an endpoint when its configuration goes missing rather than closing
    # it. There is no decorator here, so this list is the only thing standing
    # between a new route and being public in silence.
    url, _ = server
    status, payload = _call(url + path, method, body)
    assert status == 401
    assert "sign in" in payload["error"]


# -- starting ---------------------------------------------------------------


def test_starting_a_capture_returns_the_row_the_supervisor_will_pick_up(signed_in):
    url, conn, cookie = signed_in
    status, payload = _call(
        url + "/api/skybird/start", "POST", {"url": LIVE}, cookie
    )
    assert status == 201
    session = payload["session"]
    assert session["state"] == "requested"
    assert session["platform"] == "youtube"
    assert session["requested_by"] == "ehewes"
    assert session["embed_url"].startswith("https://www.youtube.com/embed/")
    assert store.get(conn, session["id"]) is not None


def test_starting_a_capture_reaches_the_audit_trail(signed_in):
    # Start and delete are audited; stop is not, because the session row
    # already carries stopped_at and stop_reason.
    url, conn, cookie = signed_in
    _call(url + "/api/skybird/start", "POST", {"url": LIVE}, cookie)
    with conn.cursor() as cur:
        cur.execute(
            "select operation, actor, detail from audit.event "
            "where operation = 'skybird.start'"
        )
        row = cur.fetchone()
    assert row is not None
    assert row[1] == "ehewes"
    assert row[2]["platform"] == "youtube"


def test_a_url_from_a_platform_we_do_not_have_says_what_is_supported(signed_in):
    url, _, cookie = signed_in
    status, payload = _call(
        url + "/api/skybird/start", "POST", {"url": "https://vimeo.com/1"}, cookie
    )
    assert status == 400
    assert "YouTube" in payload["error"]


def test_a_missing_url_is_refused_before_anything_is_resolved(signed_in):
    url, _, cookie = signed_in
    status, payload = _call(url + "/api/skybird/start", "POST", {}, cookie)
    assert status == 400
    assert "URL" in payload["error"]


def test_a_body_that_is_not_json_is_refused(signed_in):
    url, _, cookie = signed_in
    request = urllib.request.Request(
        url + "/api/skybird/start", data=b"not json", method="POST"
    )
    request.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    assert status == 400


def test_the_same_stream_twice_is_a_conflict_rather_than_a_second_capture(signed_in):
    url, _, cookie = signed_in
    _call(url + "/api/skybird/start", "POST", {"url": LIVE}, cookie)
    status, payload = _call(
        # A different URL shape for the same video, which is the realistic way
        # this happens.
        url + "/api/skybird/start", "POST", {"url": "https://youtu.be/jNQXAC9IVRw"},
        cookie,
    )
    assert status == 409
    assert "already" in payload["error"]


def test_the_session_cap_refuses_the_next_one_with_something_to_do_about_it(
    signed_in, monkeypatch
):
    url, _, cookie = signed_in
    monkeypatch.setenv("SKYBIRD_MAX_SESSIONS", "1")
    _call(url + "/api/skybird/start", "POST", {"url": LIVE}, cookie)
    status, payload = _call(
        url + "/api/skybird/start", "POST",
        {"url": "https://www.twitch.tv/somestreamer"}, cookie,
    )
    assert status == 429
    assert "Stop one first" in payload["error"]


# -- listing and transcript -------------------------------------------------


def test_the_listing_names_the_platforms_it_accepts(signed_in):
    # So the interface does not hold its own copy of the list, which would go
    # stale the day an adapter is added.
    url, _, cookie = signed_in
    status, payload = _call(url + "/api/skybird", cookie=cookie)
    assert status == 200
    assert {p["code"] for p in payload["platforms"]} == {"youtube", "twitch"}
    assert payload["chunk_seconds"] == 15


def test_the_transcript_poll_returns_the_state_beside_the_lines(signed_in):
    # Both in one answer: a transcript that stopped growing and a capture that
    # failed look identical until the state is beside it.
    url, conn, cookie = signed_in
    _, created = _call(url + "/api/skybird/start", "POST", {"url": LIVE}, cookie)
    session_id = created["session"]["id"]
    store.append_segments(
        conn,
        session_id,
        [
            (1, 0, _now(), 0.0, 2.0, "good morning"),
            (2, 0, _now(), 2.0, 2.0, "markets are open"),
        ],
    )

    status, payload = _call(
        url + f"/api/skybird/transcript?session={session_id}", cookie=cookie
    )
    assert status == 200
    assert [s["text"] for s in payload["segments"]] == [
        "good morning", "markets are open"
    ]
    assert payload["session"]["state"] == "requested"

    _, later = _call(
        url + f"/api/skybird/transcript?session={session_id}&after=1", cookie=cookie
    )
    assert [s["seq"] for s in later["segments"]] == [2]


def test_a_nonsense_after_value_re_sends_rather_than_failing_the_poll(signed_in):
    url, conn, cookie = signed_in
    _, created = _call(url + "/api/skybird/start", "POST", {"url": LIVE}, cookie)
    session_id = created["session"]["id"]
    store.append_segments(conn, session_id, [(1, 0, _now(), 0.0, 1.0, "hello")])
    status, payload = _call(
        url + f"/api/skybird/transcript?session={session_id}&after=banana",
        cookie=cookie,
    )
    assert status == 200
    assert len(payload["segments"]) == 1


def test_a_transcript_for_a_capture_that_is_not_there(signed_in):
    url, _, cookie = signed_in
    status, _ = _call(url + "/api/skybird/transcript?session=999", cookie=cookie)
    assert status == 404


def test_a_transcript_request_naming_no_session(signed_in):
    url, _, cookie = signed_in
    status, _ = _call(url + "/api/skybird/transcript", cookie=cookie)
    assert status == 400


# -- stopping and deleting --------------------------------------------------


def test_stopping_a_requested_capture_ends_it(signed_in):
    url, _, cookie = signed_in
    _, created = _call(url + "/api/skybird/start", "POST", {"url": LIVE}, cookie)
    status, payload = _call(
        url + "/api/skybird/stop", "POST", {"id": created["session"]["id"]}, cookie
    )
    assert status == 200
    assert payload["session"]["state"] == "stopped"


def test_stopping_something_already_finished_is_a_conflict(signed_in):
    url, _, cookie = signed_in
    _, created = _call(url + "/api/skybird/start", "POST", {"url": LIVE}, cookie)
    session_id = created["session"]["id"]
    _call(url + "/api/skybird/stop", "POST", {"id": session_id}, cookie)
    status, payload = _call(
        url + "/api/skybird/stop", "POST", {"id": session_id}, cookie
    )
    assert status == 409
    assert "stopped" in payload["error"]


def test_stopping_a_capture_that_does_not_exist(signed_in):
    url, _, cookie = signed_in
    status, _ = _call(url + "/api/skybird/stop", "POST", {"id": 999}, cookie)
    assert status == 404


def test_deleting_a_capture_removes_the_transcript_with_it(signed_in):
    url, conn, cookie = signed_in
    _, created = _call(url + "/api/skybird/start", "POST", {"url": LIVE}, cookie)
    session_id = created["session"]["id"]
    store.append_segments(conn, session_id, [(1, 0, _now(), 0.0, 1.0, "hello")])

    status, payload = _call(
        url + "/api/skybird/delete", "POST", {"id": session_id}, cookie
    )
    assert status == 200
    assert payload["deleted"] == session_id

    with conn.cursor() as cur:
        cur.execute("select count(*) from skybird.transcript_segment")
        assert cur.fetchone()[0] == 0
    assert store.get(conn, session_id) is None


def test_deleting_records_a_count_rather_than_the_words(signed_in):
    # The trail records that a transcript went, not what was in it. Same rule
    # the transcription row follows.
    url, conn, cookie = signed_in
    _, created = _call(url + "/api/skybird/start", "POST", {"url": LIVE}, cookie)
    session_id = created["session"]["id"]
    store.append_segments(
        conn, session_id, [(1, 0, _now(), 0.0, 1.0, "a very memorable phrase")]
    )
    _call(url + "/api/skybird/delete", "POST", {"id": session_id}, cookie)

    with conn.cursor() as cur:
        cur.execute(
            "select detail from audit.event where operation = 'skybird.delete'"
        )
        detail = cur.fetchone()[0]
    assert detail["segments"] == 1
    assert "memorable" not in json.dumps(detail)


def test_deleting_a_capture_that_does_not_exist(signed_in):
    url, _, cookie = signed_in
    status, _ = _call(url + "/api/skybird/delete", "POST", {"id": 999}, cookie)
    assert status == 404


@pytest.mark.parametrize("body", [{}, {"id": "three"}, {"id": True}])
def test_a_bad_session_id_is_refused(signed_in, body):
    # `bool` is an `int` in Python, so `{"id": true}` reaching a query as 1 is
    # the kind of thing only ever found the hard way.
    url, _, cookie = signed_in
    status, _ = _call(url + "/api/skybird/stop", "POST", body, cookie)
    assert status == 400


def _now():
    return datetime.now(UTC)


# -- pausing ----------------------------------------------------------------


def test_pausing_and_resuming_a_capture(signed_in):
    url, _, cookie = signed_in
    _, created = _call(url + "/api/skybird/start", "POST", {"url": LIVE}, cookie)
    session_id = created["session"]["id"]

    status, paused = _call(
        url + "/api/skybird/pause", "POST", {"id": session_id}, cookie
    )
    assert status == 200
    assert paused["session"]["state"] == "paused"
    # Still live: it holds its stream, so it stays above history in the list.
    assert paused["session"]["live"] is True

    status, resumed = _call(
        url + "/api/skybird/resume", "POST", {"id": session_id}, cookie
    )
    assert status == 200
    # Back through the queue rather than straight to running, because the
    # manifest it had has expired and the cap has to apply again.
    assert resumed["session"]["state"] == "requested"


def test_a_paused_capture_does_not_count_against_the_session_cap(
    signed_in, monkeypatch
):
    # Which is the point of pause: put something else on without giving up the
    # first stream.
    url, _, cookie = signed_in
    monkeypatch.setenv("SKYBIRD_MAX_SESSIONS", "1")
    _, created = _call(url + "/api/skybird/start", "POST", {"url": LIVE}, cookie)
    _call(url + "/api/skybird/pause", "POST", {"id": created["session"]["id"]}, cookie)

    status, _ = _call(
        url + "/api/skybird/start", "POST",
        {"url": "https://www.twitch.tv/somestreamer"}, cookie,
    )
    assert status == 201


def test_a_paused_capture_still_holds_its_stream(signed_in):
    url, _, cookie = signed_in
    _, created = _call(url + "/api/skybird/start", "POST", {"url": LIVE}, cookie)
    _call(url + "/api/skybird/pause", "POST", {"id": created["session"]["id"]}, cookie)

    status, payload = _call(
        url + "/api/skybird/start", "POST", {"url": LIVE}, cookie
    )
    assert status == 409
    assert "already" in payload["error"]


@pytest.mark.parametrize(
    "action,expected", [("pause", "cannot pause"), ("resume", "cannot resume")]
)
def test_a_transition_that_does_not_apply_says_which_and_why(
    signed_in, action, expected
):
    url, _, cookie = signed_in
    _, created = _call(url + "/api/skybird/start", "POST", {"url": LIVE}, cookie)
    session_id = created["session"]["id"]
    _call(url + "/api/skybird/stop", "POST", {"id": session_id}, cookie)

    status, payload = _call(
        url + f"/api/skybird/{action}", "POST", {"id": session_id}, cookie
    )
    assert status == 409
    assert expected in payload["error"] and "stopped" in payload["error"]


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_a_transition_on_a_capture_that_does_not_exist(signed_in, action):
    url, _, cookie = signed_in
    status, _ = _call(url + f"/api/skybird/{action}", "POST", {"id": 999}, cookie)
    assert status == 404
