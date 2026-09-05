import json
import re
from pathlib import Path

import threading

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from screener.playground import (
    MAX_ROWS,
    Misconfigured,
    QueryError,
    Unavailable,
    catalog,
    ensure_password,
    run,
)

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "013_playground.sql"
PASSWORD = "throwaway-for-this-test"

# The grant list is read out of the migration rather than repeated here, so this
# file cannot be the thing that is out of date. What it does hold is the deny
# list, with the reason each one is denied, so a table added by a future
# migration has to be argued about in one of two places before the suite is
# green again.
DENIED = {
    "auth.app_user": "who may sign in",
    "auth.session": "token_hash is authentication material",
    "audit.event": "identities, conversation transcripts and spend",
    # Skybird, all three, and the argument is the same one `audit.event` loses
    # on: a captured transcript is somebody's words, and `requested_by` is a
    # GitHub login. It is also the deciding one — Steven's `sql` tool runs on
    # this role, and he is deliberately built to start and stop captures without
    # being able to read one back. A grant here would hand him that through the
    # side door.
    "skybird.platform": "the schema is denied whole, so its lookup table is too",
    "skybird.stream_session": "requested_by is an identity",
    "skybird.transcript_segment": "captured speech, and what Steven must not read",
}


def granted_in_migration() -> set[str]:
    text = MIGRATION.read_text()
    body = text.split("grant select on", 1)[1].split("to playground", 1)[0]
    body = re.sub(r"--[^\n]*", "", body)
    return {"public." + name.strip() for name in body.split(",") if name.strip()}


# A session secret for this file alone. No other test module is imported to get
# one: `tests` is not a package, so a cross-module import collects locally and
# fails in CI, and every other test file here is self-contained for that reason.
SECRET = "playground-tests-session-secret"


@pytest.fixture
def signed_in(fresh_db, db_url, monkeypatch):
    """A server with sign-in configured, and a live session cookie for it."""
    from screener import auth
    from screener.health import build_server

    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("GITHUB_CLIENT_ID", "cid")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "sec")
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    monkeypatch.setenv("ALLOWED_GITHUB_LOGINS", "ellis")

    server = build_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    token = auth.create_session(fresh_db, github_id=1, login="ellis", secret=SECRET)
    try:
        yield (
            f"http://127.0.0.1:{server.server_address[1]}",
            f"{auth.SESSION_COOKIE}={token}",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def playground(fresh_db, db_url, monkeypatch):
    """The read-only role, provisioned the way a deployment provisions it.

    Through `ensure_password` rather than a hand-built role, so what is tested
    is the thing that ships and not something that resembles it.
    """
    monkeypatch.setenv("PLAYGROUND_DB_PASSWORD", PASSWORD)
    ensure_password(fresh_db)
    url = make_conninfo(db_url, user="playground", password=PASSWORD)
    monkeypatch.setenv("PLAYGROUND_DATABASE_URL", url)
    return url


def refuse(sql, limit=200):
    with pytest.raises(QueryError) as caught:
        run(sql, limit)
    return caught.value


# -- the role is the enforcement --------------------------------------------


@pytest.mark.parametrize("table", sorted(DENIED))
def test_the_playground_cannot_read_what_it_was_never_granted(playground, table):
    # Not a filter that has to be right, and not a parser that has to be
    # complete: the role holds no `usage` on those schemas, so there is no
    # spelling of a query that reaches them.
    assert "permission denied" in refuse(f"select * from {table}").message


def test_skybird_stays_denied_even_where_the_conditional_grant_fired(
    playground, fresh_db
):
    # The test above passes for skybird by accident of ordering. Migration 013
    # grants the schema when it already exists, and on a fresh database it does
    # not exist yet -- 014 creates it afterwards -- so the grant never runs and
    # nothing has to be taken back. A machine that had skybird before the
    # playground landed is the other case, and there the grant did fire.
    #
    # So put the database in that state on purpose and check 016 undoes it.
    # Without this, whether Steven's `sql` tool can read a transcript depends on
    # the order two migrations happened to arrive in, which is not a thing
    # anyone would think to check.
    fresh_db.execute("grant usage on schema skybird to playground")
    fresh_db.execute("grant select on all tables in schema skybird to playground")
    assert "permission denied" not in _tried("select * from skybird.stream_session")

    revoke = MIGRATION.parent / "016_skybird_out_of_the_playground.sql"
    for statement in revoke.read_text().split(";"):
        if statement.strip() and not statement.strip().startswith("--"):
            fresh_db.execute(statement)

    for table in ("platform", "stream_session", "transcript_segment"):
        assert "permission denied" in refuse(f"select * from skybird.{table}").message


def _tried(sql: str) -> str:
    """The error a query produced, or "" if it succeeded."""
    try:
        run(sql, 200)
    except QueryError as exc:
        return exc.message
    return ""


def test_the_playground_is_not_a_superuser_and_cannot_read_a_file(playground):
    # The application's own connection *can* do this — it is the cluster
    # superuser — which is the entire reason this feature connects as something
    # else rather than reusing the connection that was already open.
    assert "permission denied" in refuse("select pg_read_file('/etc/hostname')").message
    with psycopg.connect(playground) as conn:
        row = conn.execute(
            "select usesuper from pg_user where usename = current_user"
        ).fetchone()
    assert row is not None and row[0] is False


def test_a_write_is_refused_and_changes_nothing(playground, fresh_db):
    fresh_db.execute("insert into data_source (code, name) values ('probe', 'p')")
    before = fresh_db.execute("select count(*) from data_source").fetchone()[0]
    refuse("insert into data_source (code, name) values ('x', 'y')")
    refuse("delete from data_source")
    refuse("drop table data_source")
    assert fresh_db.execute("select count(*) from data_source").fetchone()[0] == before


def test_a_url_pointing_at_a_superuser_is_refused_rather_than_served(
    playground, db_url, monkeypatch
):
    # The one hole the design otherwise has: nothing stops someone setting this
    # to the application's own connection, and every other test here would still
    # pass because they all build their own URL.
    monkeypatch.setenv("PLAYGROUND_DATABASE_URL", db_url)
    with pytest.raises(Misconfigured):
        run("select 1")


def test_the_migration_applies_twice_because_the_role_outlives_the_schema(
    empty_db, db_url
):
    # A role is a cluster object and survives `drop schema cascade`, so a bare
    # `create role` fails on the second test in a run. This is why migration 013
    # carries the first plpgsql block in the directory.
    from screener.migrate import apply_migrations

    apply_migrations(empty_db, MIGRATION.parent)
    for statement in ("drop schema public cascade", "drop schema auth cascade",
                      "drop schema audit cascade", "drop schema skybird cascade",
                      "create schema public"):
        empty_db.execute(statement)
    apply_migrations(empty_db, MIGRATION.parent)  # must not raise


# -- one statement, and only a select ---------------------------------------


def test_a_second_statement_after_a_semicolon_is_refused(playground, fresh_db):
    # psycopg uses the *simple* protocol when a query has no parameters, and a
    # playground query never has any, so a plain execute would run both halves
    # of this. The server-side cursor is what stops it.
    refuse("select 1; drop table security")
    assert fresh_db.execute("select to_regclass('security')").fetchone()[0] is not None


def test_a_data_modifying_cte_is_refused(playground):
    error = refuse("with d as (delete from data_source returning 1) select * from d")
    assert "data-modifying" in error.message


def test_a_trailing_semicolon_is_fine_because_that_is_how_a_query_is_pasted(playground):
    assert run("select 1 as n;").row_count == 1


def test_a_leading_comment_does_not_swallow_the_query(playground):
    # The text is wrapped in `DECLARE ... CURSOR FOR `, so without the newline
    # this module prepends, a first-line `--` would comment out the select.
    assert run("-- what is this\nselect 2 as n").rows == ((2,),)


# -- bounds -----------------------------------------------------------------


def test_a_limit_over_the_ceiling_is_clamped_rather_than_refused(playground):
    result = run("select generate_series(1, 5000) as n", limit=99_999)
    assert result.limit == MAX_ROWS
    assert result.row_count == MAX_ROWS
    assert result.truncated is True


def test_more_rows_than_asked_for_come_back_truncated(playground):
    # One row over the limit is fetched and dropped, so `truncated` is a fact
    # rather than a guess, and the rows past it never cross the wire.
    result = run("select generate_series(1, 50) as n", limit=10)
    assert result.row_count == 10 and result.truncated is True

    exact = run("select generate_series(1, 10) as n", limit=10)
    assert exact.row_count == 10 and exact.truncated is False


def test_a_query_longer_than_the_bound_never_reaches_postgres(playground):
    assert "longer than" in refuse("select 1 " + "-- pad\n" * 3000).message


def test_a_query_that_will_not_finish_is_cut_and_says_so(playground, monkeypatch):
    from screener.playground import engine

    monkeypatch.setattr(engine, "_OPTIONS", "-c statement_timeout=250")
    assert "timeout" in refuse("select pg_sleep(5)").message


def test_a_very_long_value_is_shortened_and_the_response_says_so(playground):
    result = run("select repeat('x', 100000) as wide")
    assert result.shortened == 1
    assert len(result.rows[0][0]) < 400


# -- errors -----------------------------------------------------------------


def test_a_query_error_carries_the_message_postgres_wrote(playground):
    # `UndefinedColumn` with no position is a riddle; this is the entire value
    # of a SQL box, and it is safe because it is about text just typed.
    error = refuse("select tickr from security")
    assert error.sqlstate == "42703"
    assert 'column "tickr" does not exist' in error.message


def test_the_reported_position_points_at_the_character_the_reader_typed(playground):
    # Postgres counts within `DECLARE "playground" CURSOR FOR \n<query>`, so both
    # prefixes come off before the caret can be drawn under the right character.
    assert refuse("select tickr from security").position == 7
    assert refuse("-- note\nselect nope from security").position == 15


def test_a_connection_failure_is_named_by_type_and_never_by_message(monkeypatch):
    # psycopg embeds the host and usually the username in connection errors,
    # which is why `screener.health.checks` reports type names. That convention
    # holds here; only *query* errors get their message shown.
    monkeypatch.setenv(
        "PLAYGROUND_DATABASE_URL", "postgresql://nobody:hunter2@127.0.0.1:1/none"
    )
    with pytest.raises(Unavailable) as caught:
        run("select 1")
    assert "hunter2" not in str(caught.value) and "127.0.0.1" not in str(caught.value)


# -- serialisation ----------------------------------------------------------


def test_every_value_survives_json_dumps_without_a_default(playground):
    # The invariant that protects `_respond`, which deliberately has no
    # `default=`: the conversion lives in the engine rather than becoming a
    # licence for any endpoint to emit a Decimal.
    result = run(
        "select now() as ts, current_date as d, 1.5::numeric as num, "
        "'\\x4142'::bytea as b, null as nothing, '{\"a\":1}'::jsonb as j, "
        "'[2026-01-01,2026-02-01)'::daterange as rng, interval '1 day' as iv, "
        "'nan'::float8 as nan, gen_random_uuid() as id"
    )
    json.dumps({"rows": [list(r) for r in result.rows]})


def test_a_numeric_survives_as_a_string_rather_than_becoming_a_float(playground):
    # A price that changed in its seventh digit because it was rendered is the
    # quiet wrongness this project spends its comments avoiding.
    assert run("select 0.1234567890123456789::numeric as n").rows[0][0] == (
        "0.1234567890123456789"
    )


def test_a_not_a_number_does_not_make_the_response_invalid_json(playground):
    # `json.dumps` writes a bare NaN, which `JSON.parse` rejects, so one cell
    # would otherwise fail the whole response.
    assert json.loads(json.dumps(run("select 'nan'::float8 as n").rows[0][0])) == "nan"


# -- the catalogue is a rendering of the grant -------------------------------


def test_the_catalogue_lists_exactly_what_the_migration_grants(playground):
    assert {f"{t.schema}.{t.name}" for t in catalog()} == granted_in_migration()


def test_the_catalogue_never_mentions_sign_in_or_the_audit_trail(playground):
    listed = {f"{t.schema}.{t.name}" for t in catalog()}
    assert listed.isdisjoint(DENIED)


def test_a_partitioned_table_appears_once_rather_than_once_per_year(playground):
    names = [t.name for t in catalog()]
    assert names.count("price_daily") == 1
    assert not [n for n in names if n.startswith("price_daily_")]


def test_a_partitioned_table_can_still_be_read_through_its_parent(playground):
    # The claim the grant rests on: children inherit no ACL, but reading through
    # the parent needs no grant on them. Proven rather than trusted.
    assert run("select count(*) from price_daily").row_count == 1
    assert "permission denied" in refuse("select * from price_daily_2026").message


def test_every_table_is_either_granted_or_deliberately_denied(playground, fresh_db):
    # So a table added by a future migration fails this suite until someone has
    # decided which side of the line it is on.
    rows = fresh_db.execute(
        """
        select n.nspname || '.' || c.relname
        from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where c.relkind in ('r', 'p') and not c.relispartition
          and n.nspname not in ('pg_catalog', 'information_schema')
          and n.nspname not like 'pg\\_%'
        """
    ).fetchall()
    assert {r[0] for r in rows} == granted_in_migration() | set(DENIED)


# -- over HTTP ---------------------------------------------------------------


def http(server_url, path, cookie=None, data=None):
    import urllib.error
    import urllib.request

    request = urllib.request.Request(server_url + path, data=data)
    if cookie:
        request.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_the_page_is_told_the_playground_is_off_rather_than_shown_an_error(
    signed_in, monkeypatch
):
    # A feature deliberately not configured here should render a card saying so,
    # not a red box saying the server broke.
    monkeypatch.delenv("PLAYGROUND_DATABASE_URL", raising=False)
    url, cookie = signed_in
    status, body = http(url, "/api/playground", cookie)
    assert status == 200 and body["enabled"] is False and body["schemas"] == []


def test_a_bad_body_is_a_bad_request_rather_than_a_traceback(signed_in):
    url, cookie = signed_in
    status, _ = http(url, "/api/playground/query", cookie, b"not json")
    assert status == 400
