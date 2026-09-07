"""Writing facts: the clock, and what counts as new.

The two rules this file exists to hold: every fact from one payload carries its
observation's `fetched_at`, and a fact is inserted only when its value differs
from the latest already held.
"""

from datetime import date
from decimal import Decimal

import pytest

from screener.ingest import Fact, insert_facts, latest_values, metric_ids, record_observation


@pytest.fixture
def ctx(fresh_db):
    """One security, and the run it will hang observations from."""
    security = fresh_db.execute(
        """insert into security
           (name, mic, currency, country, primary_symbol, first_seen)
           values ('Alpha', 'XNAS', 'USD', 'US', 'AAA', '2020-01-01')
           returning id"""
    ).fetchone()[0]
    source = fresh_db.execute(
        "insert into data_source (code, name) values ('yahoo', 'Yahoo') "
        "on conflict (code) do update set name = excluded.name returning id"
    ).fetchone()[0]
    run = fresh_db.execute(
        "insert into ingest_run (source_id, endpoint, started_at, status) "
        "values (%s, 'timeseries', now(), 'running') returning id",
        (source,),
    ).fetchone()[0]
    return security, run


def _observe(conn, run_id, security_id, digest=b"\x01" * 32):
    with conn.cursor() as cur:
        return record_observation(
            cur,
            ingest_run_id=run_id,
            security_id=security_id,
            content_hash=digest,
            blob_path="yahoo/timeseries/2026-09-06/1.json.gz",
            is_new_payload=True,
            payload_bytes=100,
        )


def _fact(code="revenue", period_end=date(2025, 9, 30), period_type="A",
          value="100", currency: str | None = "USD"):
    return Fact(code, period_end, period_type, Decimal(value), currency)


def test_metric_ids_returns_only_inputs(fresh_db):
    with fresh_db.cursor() as cur:
        ids = metric_ids(cur)
    assert "revenue" in ids and "ret_12m" not in ids
    assert len(ids) == 28


def test_record_observation_returns_its_fetched_at(fresh_db, ctx):
    security, run = ctx
    observation_id, fetched_at = _observe(fresh_db, run, security)

    stored = fresh_db.execute(
        "select fetched_at from ingest_observation where id = %s",
        (observation_id,),
    ).fetchone()[0]
    assert fetched_at == stored


def test_every_fact_from_one_payload_shares_the_observation_clock(fresh_db, ctx):
    # D4. A per-row clock would make two facts from one response sort
    # non-deterministically and let restates_id point at a later row than the
    # one superseding it.
    security, run = ctx
    observation_id, fetched_at = _observe(fresh_db, run, security)
    facts = [
        _fact("revenue", date(2025, 9, 30), "A", "100"),
        _fact("net_income", date(2025, 9, 30), "A", "10"),
        _fact("revenue", date(2025, 6, 30), "Q", "25"),
    ]
    with fresh_db.cursor() as cur:
        ids = metric_ids(cur)
        assert insert_facts(cur, security, observation_id, fetched_at, facts, {}, ids) == 3

    stamps = fresh_db.execute(
        "select distinct observed_at from fundamental_fact"
    ).fetchall()
    assert stamps == [(fetched_at,)]


def test_an_unchanged_value_inserts_nothing(fresh_db, ctx):
    security, run = ctx
    first_id, first_at = _observe(fresh_db, run, security)
    with fresh_db.cursor() as cur:
        ids = metric_ids(cur)
        insert_facts(cur, security, first_id, first_at, [_fact()], {}, ids)

    second_id, second_at = _observe(fresh_db, run, security)
    with fresh_db.cursor() as cur:
        held = latest_values(cur, security)
        inserted = insert_facts(
            cur, security, second_id, second_at, [_fact()], held, ids
        )

    assert inserted == 0
    assert fresh_db.execute(
        "select count(*) from fundamental_fact"
    ).fetchone()[0] == 1


def test_a_changed_value_inserts_and_points_at_what_it_supersedes(fresh_db, ctx):
    security, run = ctx
    first_id, first_at = _observe(fresh_db, run, security)
    with fresh_db.cursor() as cur:
        ids = metric_ids(cur)
        insert_facts(cur, security, first_id, first_at, [_fact(value="100")], {}, ids)
        original = cur.execute(
            "select id from fundamental_fact"
        ).fetchone()[0]

    second_id, second_at = _observe(fresh_db, run, security, digest=b"\x02" * 32)
    with fresh_db.cursor() as cur:
        held = latest_values(cur, security)
        assert insert_facts(
            cur, security, second_id, second_at, [_fact(value="110")], held, ids
        ) == 1

    rows = fresh_db.execute(
        "select value, restates_id from fundamental_fact order by observed_at"
    ).fetchall()
    assert [r[0] for r in rows] == [Decimal("100"), Decimal("110")]
    assert rows[0][1] is None
    assert rows[1][1] == original


def test_a_value_that_reverts_inserts_a_third_row(fresh_db, ctx):
    # A -> B -> A. The comparison is against the latest observation, not
    # against every value ever seen, so Wednesday must insert.
    security, run = ctx
    with fresh_db.cursor() as cur:
        ids = metric_ids(cur)
    for value in ("100", "110", "100"):
        obs_id, obs_at = _observe(fresh_db, run, security)
        with fresh_db.cursor() as cur:
            held = latest_values(cur, security)
            insert_facts(
                cur, security, obs_id, obs_at, [_fact(value=value)], held, ids
            )

    rows = fresh_db.execute(
        "select value from fundamental_fact order by observed_at, id"
    ).fetchall()
    assert [r[0] for r in rows] == [Decimal("100"), Decimal("110"), Decimal("100")]


def test_a_quarter_and_a_year_on_one_date_are_two_facts(fresh_db, ctx):
    # F5, at the write layer: they differ only in period_type, and the key
    # `latest_values` uses has to keep them apart.
    security, run = ctx
    obs_id, obs_at = _observe(fresh_db, run, security)
    with fresh_db.cursor() as cur:
        ids = metric_ids(cur)
        inserted = insert_facts(
            cur, security, obs_id, obs_at,
            [
                _fact("revenue", date(2025, 9, 30), "A", "416161"),
                _fact("revenue", date(2025, 9, 30), "Q", "102466"),
            ],
            {}, ids,
        )
    assert inserted == 2

    with fresh_db.cursor() as cur:
        held = latest_values(cur, security)
    assert len(held) == 2
    assert held[("revenue", date(2025, 9, 30), "A")].value == Decimal("416161")
    assert held[("revenue", date(2025, 9, 30), "Q")].value == Decimal("102466")


def test_the_currency_is_stored_as_given(fresh_db, ctx):
    security, run = ctx
    obs_id, obs_at = _observe(fresh_db, run, security)
    with fresh_db.cursor() as cur:
        ids = metric_ids(cur)
        insert_facts(
            cur, security, obs_id, obs_at,
            [_fact(currency="GBP"), _fact("net_income", currency=None)],
            {}, ids,
        )

    rows = fresh_db.execute(
        "select currency from fundamental_fact order by currency nulls last"
    ).fetchall()
    assert rows == [("GBP",), (None,)]


def test_a_fact_naming_an_unknown_metric_is_skipped(fresh_db, ctx):
    # The parser only emits codes from SERIES, so this cannot happen from a
    # real payload -- but a not-null foreign key failing mid-night would take
    # the security down, and skipping is the cheaper contract.
    security, run = ctx
    obs_id, obs_at = _observe(fresh_db, run, security)
    with fresh_db.cursor() as cur:
        ids = metric_ids(cur)
        assert insert_facts(
            cur, security, obs_id, obs_at, [_fact("no_such_metric")], {}, ids
        ) == 0
