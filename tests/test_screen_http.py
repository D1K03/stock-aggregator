"""The screen endpoints over HTTP: who may ask, and every refusal (ui-swap spec §7)."""

import json
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

import psycopg
import pytest

from screener import auth
from screener.health import build_server
from screener.scoring import LOGIC_DESCRIPTION

SECRET = "a-session-secret"
AS_OF = date(2026, 3, 2)


@pytest.fixture
def server(monkeypatch, db_url, fresh_db):
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


@pytest.fixture
def cookie(server) -> str:
    _, conn = server
    token = auth.create_session(conn, github_id=1, login="ehewes", secret=SECRET)
    return f"{auth.SESSION_COOKIE}={token}"


def get(url: str, cookie: str | None = None) -> tuple[int, Any]:
    request = urllib.request.Request(url)
    if cookie:
        request.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _listed(conn: Any, symbol: str, mic: str = "XNAS") -> int:
    security_id = conn.execute(
        """insert into security (name, mic, currency, country, primary_symbol, first_seen)
           values (%s, %s, 'USD', 'US', %s, '2020-01-01') returning id""",
        (f"{symbol} {mic}", mic, symbol),
    ).fetchone()[0]
    conn.execute(
        """insert into security_symbol (security_id, symbol, mic, valid_from, source)
           values (%s, %s, %s, '2020-01-01', 'test')""",
        (security_id, symbol, mic),
    )
    return security_id


def _night(conn: Any) -> int:
    """One v2 night with one unclassified security, JPM, scored on Momentum."""
    scheme = conn.execute(
        "insert into sector_scheme (code, name) values ('yfinance', 'yfinance') returning id"
    ).fetchone()[0]
    conn.execute(
        "insert into peer_group (scheme_id, sector_node_id, level, code)"
        " values (%s, null, 0, 'market')",
        (scheme,),
    )
    security_id = _listed(conn, "JPM")
    started = datetime.combine(AS_OF, time(23), tzinfo=timezone.utc)
    run_id = conn.execute(
        """insert into scoring_run
           (as_of_range, cutoff_offset, logic_version_id, weight_version_id, status,
            emits_alerts, git_sha, config_hash, started_at, finished_at, outcome)
           select daterange(%(as_of)s, %(next)s, '[)'), interval '30 hours', l.id, w.id,
                  'live', false, 'abc1234', %(hash)s, %(started)s, %(started)s, 'ok'
             from scoring_logic_version l, weight_version w
            where l.description = %(logic)s and w.code = 'v2'
           returning id""",
        {"as_of": AS_OF, "next": AS_OF + timedelta(days=1), "hash": b"\x9f",
         "started": started, "logic": LOGIC_DESCRIPTION},
    ).fetchone()[0]
    conn.execute(
        """insert into snapshot_daily
           (as_of, scoring_run_id, security_id, blended_score, pillar_agreement,
            min_coverage, worst_fallback_level)
           values (%s, %s, %s, %s, 1, 1, 0)""",
        (AS_OF, run_id, security_id, Decimal("61.5")),
    )
    conn.execute(
        """insert into pillar_score_daily
           (as_of, scoring_run_id, security_id, pillar_id, score, metric_count, coverage)
           select %s, %s, %s, p.id, %s, 4, 1 from pillar p where p.code = 'momentum'""",
        (AS_OF, run_id, security_id, Decimal("61.5")),
    )
    return run_id


@pytest.mark.parametrize("path", ["/api/screen", "/api/screen/security?symbol=JPM"])
def test_the_screen_is_refused_without_a_session(server, path):
    url, _ = server

    status, body = get(url + path)

    assert status == 401
    assert "sign in" in body["error"]


def test_a_bad_parameter_is_refused_by_name(server, cookie):
    url, _ = server

    assert get(url + "/api/screen?sort=price", cookie) == (
        400, {"error": "sort must be one of score, delta, V, Q, M", "parameter": "sort"},
    )
    assert get(url + "/api/screen?colour=red", cookie)[1]["parameter"] == "colour"
    assert get(url + "/api/screen/security", cookie) == (
        400, {"error": "symbol is required", "parameter": "symbol"},
    )


def test_before_the_first_night_both_endpoints_say_so(server, cookie):
    url, _ = server

    for path in ("/api/screen", "/api/screen/security?symbol=JPM"):
        assert get(url + path, cookie) == (200, {"state": "awaiting_first_night"})


def test_the_screen_answers_with_every_top_level_key(server, cookie):
    url, conn = server
    run_id = _night(conn)

    status, body = get(url + "/api/screen", cookie)

    assert status == 200
    assert set(body) == {
        "state", "latest", "run", "previous_as_of", "tiles", "sectors", "total", "rows",
    }
    assert body["run"]["id"] == run_id
    assert [row["symbol"] for row in body["rows"]] == ["JPM"]


def test_a_security_answers_with_every_top_level_key(server, cookie):
    url, conn = server
    _night(conn)

    status, body = get(url + "/api/screen/security?symbol=jpm", cookie)

    assert status == 200
    assert set(body) == {
        "scored", "run_id", "latest", "symbol", "name", "sector", "industry", "active",
        "score", "agreement", "partial", "pillars", "reproduction", "closes",
    }


def test_an_unknown_symbol_is_404(server, cookie):
    url, conn = server
    _night(conn)

    assert get(url + "/api/screen/security?symbol=ZZZZ", cookie) == (
        404, {"error": "unknown_symbol", "symbol": "ZZZZ"},
    )


def test_a_symbol_on_two_exchanges_is_409(server, cookie):
    url, conn = server
    _night(conn)
    _listed(conn, "JPM", "XNYS")

    assert get(url + "/api/screen/security?symbol=JPM", cookie) == (
        409, {"error": "ambiguous_symbol", "symbol": "JPM", "exchanges": ["XNAS", "XNYS"]},
    )


def test_a_pinned_run_that_does_not_qualify_is_409(server, cookie):
    url, conn = server
    _night(conn)

    for path in ("/api/screen?run=999999", "/api/screen/security?symbol=JPM&run=999999"):
        assert get(url + path, cookie) == (409, {"error": "run_changed", "run": 999999})


def test_an_unreachable_database_is_503_naming_only_the_error_type(server, cookie, monkeypatch):
    url, _ = server

    def unreachable(*args: object) -> None:
        raise psycopg.OperationalError("connection to 10.0.0.5 as user screener failed")

    monkeypatch.setattr("screener.screen.read_screen", unreachable)

    assert get(url + "/api/screen", cookie) == (
        503, {"error": "cannot read the screen", "database": "OperationalError"},
    )


def test_an_unexpected_error_is_500_with_no_exception_detail(server, cookie, monkeypatch):
    url, _ = server

    def broken(*args: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("screener.screen.read_screen", broken)

    assert get(url + "/api/screen", cookie) == (500, {"error": "could not build the screen"})
