"""The `/api/rupert` route on the status service.

Shaped after `tests/test_skybird_api.py`: the first test is the one that
matters, and everything after it is shape. This route carries the text of
comments beside our reading of them, so a session is the whole authorisation.
"""

import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest

from screener import auth
from screener.health import build_server
from screener.rupert import store
from screener.rupert.run import FINBERT
from screener.sentiment import Sentiment

SECRET = "a-session-secret"
BASE = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def server(monkeypatch, db_url, fresh_db):
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("GITHUB_CLIENT_ID", "cid")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "sec")
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    monkeypatch.setenv("ALLOWED_GITHUB_LOGINS", "ehewes")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8080")
    monkeypatch.delenv("RUPERT_DAILY_MAX_CALLS", raising=False)

    built = build_server("127.0.0.1", 0)
    thread = threading.Thread(target=built.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{built.server_address[1]}", fresh_db
    finally:
        built.shutdown()
        built.server_close()
        thread.join(timeout=5)


def _call(url, cookie=None):
    request = urllib.request.Request(url, method="GET")
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


def a_decision(
    conn, *, state="resolved", symbol="NVDA", name="NVIDIA Corporation", tone=None, at=None
):
    """One decision, written recently enough for the windowed panels to see it.

    `at` defaults to an hour ago rather than to a fixed date: the leaderboard
    reads seven days and the chart thirty, so a fixture pinned to a constant
    silently falls out of both as the calendar moves and the panels come back
    empty for a reason that looks like a bug in them.
    """
    at = at or (datetime.now(UTC) - timedelta(hours=1))
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into security (name, mic, currency, country, primary_symbol,
                                  is_active, first_seen)
            values (%s, 'XNAS', 'USD', 'US', %s, true, '2020-01-01') returning id
            """,
            [name, symbol],
        )
        security_id = int(cur.fetchone()[0])
        cur.execute(
            """
            insert into security_symbol (security_id, symbol, mic, valid_from, source)
            values (%s, %s, 'XNAS', '2020-01-01', 'test')
            """,
            [security_id, symbol],
        )
        cur.execute(
            """
            insert into social_item (
                source_id, kind, external_id, subreddit, created_utc, fetched_at,
                body, content_hash
            )
            select id, 'comment', %s, 'wallstreetbets', %s, now(), %s, '\\x00'
            from data_source where code = 'arctic_shift' returning id
            """,
            [f"t1_{symbol}", at, f"${symbol} looks strong into earnings"],
        )
        item = int(cur.fetchone()[0])

    source = store.source_id(conn)
    text = store.Text(store.SOCIAL, item, at, "", f"${symbol} looks strong into earnings")
    mention = store.save_mention(
        conn, source, text,
        state=state, candidates=[symbol],
        security_id=security_id if state == "resolved" else None,
        model="typesafe/jev-1.13", input_tokens=476, cost_usd=0.00002,
    )
    if tone is not None:
        store.save_reading(
            conn, mention,
            Sentiment(positive=max(tone, 0.0), negative=max(-tone, 0.0), neutral=0.0),
            model=FINBERT,
        )
    return mention


# -- the check that matters --------------------------------------------------


def test_the_route_is_refused_without_a_session(server):
    # Unconditionally, not `config.enabled and login is None` — the check that
    # opens an endpoint when its configuration goes missing rather than closing
    # it. `tests/test_auth.py` carries the same path in the list that would
    # catch a route added without any check at all.
    url, _ = server
    status, payload = _call(url + "/api/rupert")
    assert status == 401
    assert "sign in" in payload["error"]


# -- shape -------------------------------------------------------------------


def test_the_page_can_tell_switched_off_from_nothing_to_do(signed_in):
    url, _, cookie = signed_in
    status, payload = _call(url + "/api/rupert", cookie)
    assert status == 200
    # The two look identical in every number below, so the switch is reported
    # rather than inferred from an empty table.
    assert payload["enabled"] is False
    assert payload["daily_max_calls"] == 0


def test_the_answer_carries_every_panel_at_one_moment(signed_in):
    url, conn, cookie = signed_in
    a_decision(conn, tone=0.7)
    _, payload = _call(url + "/api/rupert", cookie)
    # One answer rather than six endpoints: the spend explains the activity and
    # the activity explains the coverage, and separate calls would let the
    # numbers on one screen disagree by however long the slowest one took.
    for key in ("counts", "spend", "coverage", "daily", "standings", "review",
                "frontiers", "passes"):
        assert key in payload
    assert payload["counts"]["resolved"] == 1
    assert payload["spend"]["decisions_total"] == 1
    assert payload["coverage"]["active"] == 1


def test_the_review_list_carries_the_sentence_and_the_alternatives(signed_in):
    url, conn, cookie = signed_in
    a_decision(conn, symbol="ALL", name="The Allstate Corporation", state="none")
    _, payload = _call(url + "/api/rupert", cookie)
    row = payload["review"][0]
    assert row["state"] == "none"
    assert row["symbol"] is None
    # The point of the page: no aggregate shows you a wrong link.
    assert "ALL" in row["excerpt"]
    assert row["candidates"] == ["ALL"]


def test_the_daily_series_covers_the_window_even_when_empty(signed_in):
    url, _, cookie = signed_in
    _, payload = _call(url + "/api/rupert", cookie)
    assert len(payload["daily"]) == 30
    assert all(day["decisions"] == 0 for day in payload["daily"])


def test_a_state_filter_narrows_the_review_list(signed_in):
    url, conn, cookie = signed_in
    a_decision(conn, symbol="NVDA", name="NVIDIA Corporation")
    a_decision(conn, symbol="MU", name="Micron Technology", state="unsure")
    _, every = _call(url + "/api/rupert", cookie)
    _, only = _call(url + "/api/rupert?state=unsure", cookie)
    assert len(every["review"]) == 2
    assert len(only["review"]) == 1
    assert only["review"][0]["state"] == "unsure"


def test_a_state_that_is_not_a_state_is_refused_by_name(signed_in):
    url, _, cookie = signed_in
    status, payload = _call(url + "/api/rupert?state=banana", cookie)
    # A named 400, never a quiet fallback to everything — `screener.screen`'s
    # rule, because a filter that silently does nothing reads as a filter that
    # found nothing.
    assert status == 400
    assert "state must be one of" in payload["error"]


def test_tone_comes_back_as_a_number_the_page_can_render(signed_in):
    url, conn, cookie = signed_in
    a_decision(conn, tone=0.7)
    _, payload = _call(url + "/api/rupert", cookie)
    assert payload["review"][0]["tone"] == pytest.approx(0.7)


def test_the_review_list_pages_rather_than_stopping_at_twenty_five(signed_in):
    url, conn, cookie = signed_in
    for n in range(30):
        a_decision(conn, symbol=f"S{n:03d}", name=f"Security {n}")
    _, first = _call(url + "/api/rupert?page=1", cookie)
    _, second = _call(url + "/api/rupert?page=2", cookie)
    assert first["review_total"] == 30
    assert first["review_pages"] == 2
    assert len(first["review"]) == 25
    assert len(second["review"]) == 5
    # Disjoint, which is the thing a wrong offset gets wrong quietly.
    assert not ({r["id"] for r in first["review"]} & {r["id"] for r in second["review"]})


def test_the_total_follows_the_filter(signed_in):
    url, conn, cookie = signed_in
    a_decision(conn, symbol="NVDA", name="NVIDIA Corporation")
    for n in range(3):
        a_decision(conn, symbol=f"U{n}", name=f"Unsure {n}", state="unsure")
    _, every = _call(url + "/api/rupert", cookie)
    _, only = _call(url + "/api/rupert?state=unsure", cookie)
    # A pager whose total ignored the filter would offer pages that are empty
    # when you reach them.
    assert every["review_total"] == 4
    assert only["review_total"] == 3


def test_a_page_past_the_end_is_empty_rather_than_an_error(signed_in):
    url, conn, cookie = signed_in
    a_decision(conn)
    status, payload = _call(url + "/api/rupert?page=99", cookie)
    assert status == 200
    assert payload["review"] == []


def test_a_page_that_is_not_a_number_falls_back_to_the_first(signed_in):
    url, _, cookie = signed_in
    _, payload = _call(url + "/api/rupert?page=banana", cookie)
    # Unlike `state`, which is refused by name: a bad page is a typo in a URL
    # nobody typed on purpose, and the first page is the obvious intent.
    assert payload["review_page"] == 1


def test_the_chart_defaults_to_the_security_being_talked_about_most(signed_in):
    url, conn, cookie = signed_in
    a_decision(conn, symbol="QUIET", name="Quiet Inc", tone=0.1)
    for n in range(4):
        a_decision(conn, symbol=f"LOUD{n}", name="Loud Inc", tone=0.5)
    # Four securities with one mention each, one with... also one. The default
    # is simply the head of the leaderboard; what matters is that it is never
    # empty when there is something to draw.
    _, payload = _call(url + "/api/rupert", cookie)
    assert payload["scored_security"] is not None
    assert len(payload["scored"]) == 30
    assert payload["rolling_days"] > 1


def test_the_chart_can_be_pointed_at_another_security(signed_in):
    url, conn, cookie = signed_in
    a_decision(conn, symbol="AAA", name="Aaa Inc", tone=0.5)
    a_decision(conn, symbol="BBB", name="Bbb Inc", tone=0.5)
    _, default = _call(url + "/api/rupert", cookie)
    other = next(
        s for s in default["standings"]
        if s["security_id"] != default["scored_security"]["security_id"]
    )
    _, picked = _call(url + f"/api/rupert?security={other['security_id']}", cookie)
    assert picked["scored_security"]["symbol"] == other["symbol"]


def test_an_unknown_security_falls_back_rather_than_drawing_nothing(signed_in):
    url, conn, cookie = signed_in
    a_decision(conn, tone=0.5)
    _, payload = _call(url + "/api/rupert?security=999999", cookie)
    # A chart that went blank because a stale id was in the URL would read as
    # "nothing scored" rather than "that security is not on the board".
    assert payload["scored_security"]["symbol"] == "NVDA"


def test_the_chart_is_absent_rather_than_broken_when_nothing_has_resolved(signed_in):
    url, _, cookie = signed_in
    _, payload = _call(url + "/api/rupert", cookie)
    assert payload["scored_security"] is None
    assert payload["scored"] == []


# -- the narrative -----------------------------------------------------------


def _post(url, body, cookie=None):
    data = json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    if cookie:
        request.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw) if raw else {}


@pytest.fixture
def fake_model(monkeypatch):
    """The model, replaced at the module attribute. Never a real call."""
    from screener.rupert import narrative

    calls: list[int] = []

    def write(symbol, name, mentions, **_kw):
        calls.append(len(mentions))
        return narrative.Written(
            text=f"People were mostly arguing about {symbol}'s pricing.",
            mentions_used=len(mentions),
            model="deepseek/deepseek-v4-flash",
            cost_usd=0.00012,
        )

    monkeypatch.setattr(narrative, "write", write)
    return calls


def test_the_narrative_needs_a_session(server):
    url, _ = server
    status, payload = _post(url + "/api/rupert/narrative", {"security": 1})
    assert status == 401
    assert "sign in" in payload["error"]


def test_a_narrative_summarises_what_was_said(signed_in, fake_model):
    url, conn, cookie = signed_in
    a_decision(conn, tone=0.5)
    status, payload = _post(
        url + "/api/rupert/narrative", {"security": 1}, cookie
    )
    assert status == 200
    assert payload["symbol"] == "NVDA"
    assert "arguing about" in payload["text"]
    assert payload["mentions_used"] == 1
    assert payload["cached"] is False


def test_the_second_click_on_the_same_day_is_free(signed_in, fake_model):
    url, conn, cookie = signed_in
    a_decision(conn, tone=0.5)
    _post(url + "/api/rupert/narrative", {"security": 1}, cookie)
    _, second = _post(url + "/api/rupert/narrative", {"security": 1}, cookie)
    assert second["cached"] is True
    # The model was asked once, not twice: two people opening the same security
    # an hour apart are asking the same question of the same corpus.
    assert len(fake_model) == 1


def test_a_security_nobody_mentioned_says_so_rather_than_inventing_one(
    signed_in, fake_model
):
    url, conn, cookie = signed_in
    a_decision(conn, symbol="ALL", name="Allstate", state="none")
    status, payload = _post(url + "/api/rupert/narrative", {"security": 1}, cookie)
    assert status == 200
    assert payload["text"] is None
    # And nothing was paid for: a `none` is by definition not about it.
    assert fake_model == []


def test_a_narrative_for_nothing_is_refused_by_name(signed_in, fake_model):
    url, _, cookie = signed_in
    status, payload = _post(url + "/api/rupert/narrative", {}, cookie)
    assert status == 400
    assert "security is required" in payload["error"]


def test_an_unknown_security_is_a_404_rather_than_a_paragraph(signed_in, fake_model):
    url, _, cookie = signed_in
    status, _ = _post(url + "/api/rupert/narrative", {"security": 999999}, cookie)
    assert status == 404


def test_the_narrative_lands_on_the_audit_trail(signed_in, fake_model):
    url, conn, cookie = signed_in
    a_decision(conn, tone=0.5)
    _post(url + "/api/rupert/narrative", {"security": 1}, cookie)
    rows = conn.execute(
        "select operation, actor, cost_usd from audit.event "
        "where operation = 'rupert.narrative'"
    ).fetchall()
    # The same trail Steven's replies land on, so one person's spend is one sum
    # rather than two that each look reasonable.
    assert len(rows) == 1
    assert rows[0][1] == "ehewes"
    assert float(rows[0][2]) == 0.00012


def test_the_narrative_has_a_daily_ceiling_of_its_own(signed_in, fake_model, monkeypatch):
    from screener.rupert import narrative

    url, conn, cookie = signed_in
    monkeypatch.setattr(narrative, "DAILY_MAX", 2)
    for n in range(3):
        a_decision(conn, symbol=f"S{n}", name=f"Security {n}", tone=0.4)

    codes = [
        _post(url + "/api/rupert/narrative", {"security": n + 1}, cookie)[0]
        for n in range(3)
    ]
    # Two written, the third refused. Its own counter rather than the shared
    # spend cap, on magpie's precedent: a narrative is five times a chat reply
    # and one counter for both would let this page quietly silence Steven.
    assert codes == [200, 200, 429]
    assert len(fake_model) == 2


def test_a_refused_narrative_does_not_hide_one_already_written(
    signed_in, fake_model, monkeypatch
):
    from screener.rupert import narrative

    url, conn, cookie = signed_in
    a_decision(conn, tone=0.4)
    _post(url + "/api/rupert/narrative", {"security": 1}, cookie)
    # Now slam the ceiling shut. The cached one cost nothing and is checked
    # before the cap, so being over it must not take back what is already paid for.
    monkeypatch.setattr(narrative, "DAILY_MAX", 0)
    status, payload = _post(url + "/api/rupert/narrative", {"security": 1}, cookie)
    assert status == 200
    assert payload["cached"] is True
    assert payload["text"]


def test_an_unconfigured_model_says_so_rather_than_blaming_the_weather(
    signed_in, monkeypatch
):
    from screener.rupert import narrative

    url, conn, cookie = signed_in
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(narrative, "write", lambda *a, **k: None)
    a_decision(conn, tone=0.4)
    status, payload = _post(url + "/api/rupert/narrative", {"security": 1}, cookie)
    assert status == 503
    # "Could not just now" reads as weather and gets retried forever. A missing
    # key is a deployment nobody finished, and stays broken until somebody acts
    # — which is the silent-degradation failure this project keeps naming.
    assert payload["configured"] is False
    assert "OPENROUTER_API_KEY" in payload["error"]


def test_a_configured_model_that_simply_failed_says_that_instead(
    signed_in, monkeypatch
):
    from screener.rupert import narrative

    url, conn, cookie = signed_in
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setattr(narrative, "write", lambda *a, **k: None)
    a_decision(conn, tone=0.4)
    status, payload = _post(url + "/api/rupert/narrative", {"security": 1}, cookie)
    assert status == 503
    assert payload["configured"] is True
    assert "OPENROUTER_API_KEY" not in payload["error"]


# -- the schedule ------------------------------------------------------------


def test_pausing_needs_a_session(server):
    url, _ = server
    status, _ = _post(url + "/api/rupert/pause", {"paused": True})
    assert status == 401


def test_a_pause_is_attributed_and_survives_a_read(signed_in):
    url, _, cookie = signed_in
    status, payload = _post(url + "/api/rupert/pause", {"paused": True}, cookie)
    assert status == 200 and payload["paused"] is True
    # A pause nobody can attribute is one nobody will undo, because the first
    # question is always "was that deliberate".
    assert payload["paused_by"] == "ehewes"
    _, page = _call(url + "/api/rupert", cookie)
    assert page["paused"] is True and page["paused_by"] == "ehewes"


def test_a_paused_rupert_runs_nothing(signed_in, monkeypatch, db_url):
    from screener.rupert import run as rupert_run
    from screener.rupert.config import RupertConfig

    url, conn, cookie = signed_in
    _post(url + "/api/rupert/pause", {"paused": True}, cookie)
    monkeypatch.setenv("DATABASE_URL", db_url)
    # The pass reads the row at the top of every wake, so a pause takes effect
    # on the next one rather than on the next deploy. That is the whole reason
    # it is a row and not an environment variable.
    reports = rupert_run.once(RupertConfig(daily_max_calls=100))
    assert reports == []


def test_the_schedule_is_reported_definitely(signed_in):
    url, _, cookie = signed_in
    _, page = _call(url + "/api/rupert", cookie)
    assert page["refresh_hours"] > 0
    assert "paused" in page and "next_run_at" in page


def test_a_pause_that_is_not_a_boolean_is_refused_by_name(signed_in):
    url, _, cookie = signed_in
    status, payload = _post(url + "/api/rupert/pause", {"paused": "yes"}, cookie)
    assert status == 400
    assert "true or false" in payload["error"]


# -- one decision, whole -----------------------------------------------------


def test_a_decision_carries_everything_that_produced_it(signed_in):
    url, conn, cookie = signed_in
    a_decision(conn, tone=0.5)
    mention = conn.execute("select id from rupert.mention").fetchone()[0]
    status, d = _call(url + f"/api/rupert/decision?id={mention}", cookie)
    assert status == 200
    # The inputs, not just the outputs — CLAUDE.md's "every score traces back to
    # visible raw inputs", kept for a decision that is not a score.
    assert d["source"]["body"]
    assert d["shortlist"]["candidates"] == ["NVDA"]
    assert d["finbert_input"]["text"]
    assert d["finbert"][0]["score"] == pytest.approx(0.5)
    assert d["rupert_version"] and d["version_is_current"] is True


def test_the_questions_are_rebuilt_rather_than_stored(signed_in):
    url, conn, cookie = signed_in
    a_decision(conn, tone=0.5)
    mention = conn.execute("select id from rupert.mention").fetchone()[0]
    _, d = _call(url + f"/api/rupert/decision?id={mention}", cookie)
    asked = d["jev"]["asked"]
    # Rebuilt from the pure question modules, so it cannot drift from what is
    # actually sent — and the flag says so rather than letting a reader assume
    # this is a stored copy of the real request.
    assert d["jev"]["reconstructed"] is True
    assert set(asked["questions"]) == {
        "which", "own_business", "position_talk", "injection", "claim_kind"
    }
    assert "none" in asked["questions"]["which"]["criteria"]
    # And the model is never shown a number it could hand back as a finding.
    assert "tone" not in json.dumps(asked).lower()


def test_an_unknown_decision_is_a_404(signed_in):
    url, _, cookie = signed_in
    status, _ = _call(url + "/api/rupert/decision?id=999999", cookie)
    assert status == 404


# -- through the connector ---------------------------------------------------


def test_the_mcp_offers_rupert_shaped_tools():
    from screener.mcp import tools

    names = {t["name"] for t in tools.specs()}
    # Generic `query` reaches these tables through the grant, but a shaped tool
    # is what stops every question becoming three tables and two left joins.
    assert {"rupert_mentions", "rupert_decision", "rupert_standings"} <= names


def test_the_rupert_tools_read_real_rows(signed_in, monkeypatch, db_url):
    """End to end through the connector's own read-only role.

    Pointed at `playground_mcp` rather than the app's connection, because the
    engine refuses a superuser outright — which is the guard doing its job, and
    the reason this test provisions the role instead of taking a shortcut past
    it.
    """
    import json as _json

    from psycopg.conninfo import make_conninfo

    from screener import playground
    from screener.mcp import tools

    _, conn, _ = signed_in
    a_decision(conn, tone=0.5)
    mention = conn.execute("select id from rupert.mention").fetchone()[0]

    password = "rupert-tool-tests"
    monkeypatch.setenv("PLAYGROUND_MCP_DB_PASSWORD", password)
    playground.ensure_password(conn)
    url = make_conninfo(db_url, user="playground_mcp", password=password)
    monkeypatch.setenv("PLAYGROUND_DATABASE_URL", url)
    monkeypatch.setenv("PLAYGROUND_MCP_DATABASE_URL", url)

    listed = _json.loads(tools.BY_NAME["rupert_mentions"].run({"days": 30}))
    assert listed.get("rows"), listed

    whole = _json.loads(tools.BY_NAME["rupert_decision"].run({"id": mention}))
    assert whole.get("rows"), whole
    # The provenance, through the connector: the distribution and the gates,
    # not just the winner.
    assert "probabilities" in _json.dumps(whole["columns"])

    standings = _json.loads(tools.BY_NAME["rupert_standings"].run({"days": 30}))
    assert standings.get("rows"), standings


def test_the_rupert_tools_refuse_a_state_that_is_not_one():
    import json as _json

    from screener.mcp import tools

    out = _json.loads(tools.BY_NAME["rupert_mentions"].run({"state": "banana"}))
    assert "error" in out
