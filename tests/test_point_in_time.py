import pytest
import psycopg
from decimal import Decimal

PIT_QUERY = """
select distinct on (security_id, metric_id, period_end) value
from fundamental_fact
where security_id = %s
  and observed_at <= (%s::date + %s::interval)
order by security_id, metric_id, period_end, observed_at desc
"""


@pytest.fixture
def fact_setup(fresh_db):
    """One security, one metric, one ingest observation to hang facts off."""
    with fresh_db.cursor() as cur:
        cur.execute(
            """
            insert into security (name, mic, currency, country, primary_symbol, first_seen)
            values ('Apple Inc', 'XNAS', 'USD', 'US', 'AAPL', '2020-01-01') returning id
            """
        )
        security = cur.fetchone()[0]
        cur.execute("select id from pillar where code = 'quality'")
        pillar = cur.fetchone()[0]
        cur.execute(
            """
            insert into metric (code, name, pillar_id, unit, higher_is_better, cadence)
            values ('roic', 'ROIC', %s, 'ratio', true, 'quarterly') returning id
            """,
            (pillar,),
        )
        metric = cur.fetchone()[0]
        cur.execute("insert into data_source (code, name) values ('yf', 'yfinance') returning id")
        source = cur.fetchone()[0]
        cur.execute(
            """
            insert into ingest_run (source_id, endpoint, started_at, status)
            values (%s, 'fundamentals', now(), 'ok') returning id
            """,
            (source,),
        )
        run = cur.fetchone()[0]
        cur.execute(
            """
            insert into ingest_observation
                (ingest_run_id, security_id, fetched_at, content_hash, blob_path, is_new_payload)
            values (%s, %s, now(), '\\x00'::bytea, 'yf/fundamentals/2026-01-01/1.json.gz', true)
            returning id
            """,
            (run, security),
        )
        observation = cur.fetchone()[0]
    return {"security": security, "metric": metric, "observation": observation}


def _insert_fact(conn, setup, value, observed_at, period_end="2025-12-31"):
    conn.execute(
        """
        insert into fundamental_fact
            (security_id, metric_id, period_end, period_type, value,
             observed_at, ingest_observation_id)
        values (%s, %s, %s, 'Q', %s, %s, %s)
        """,
        (setup["security"], setup["metric"], period_end, value,
         observed_at, setup["observation"]),
    )


def test_a_bare_date_cutoff_excludes_same_day_observations(fresh_db, fact_setup):
    """The defect the spec's cutoff_offset exists to prevent.

    Comparing observed_at against a bare date casts it to midnight at the START
    of that day, so a fact learned at 02:00 on D is invisible when scoring D.
    """
    _insert_fact(fresh_db, fact_setup, 0.25, "2026-02-10 02:00+00")
    with fresh_db.cursor() as cur:
        cur.execute(
            "select count(*) from fundamental_fact where observed_at <= %s::date",
            ("2026-02-10",),
        )
        assert cur.fetchone()[0] == 0


def test_cutoff_offset_includes_the_overnight_fetch(fresh_db, fact_setup):
    _insert_fact(fresh_db, fact_setup, 0.25, "2026-02-10 02:00+00")
    with fresh_db.cursor() as cur:
        cur.execute(PIT_QUERY, (fact_setup["security"], "2026-02-10", "1 day 6 hours"))
        assert [row[0] for row in cur.fetchall()] == [Decimal('0.25')]


def test_point_in_time_read_returns_the_latest_observation_at_the_cutoff(fresh_db, fact_setup):
    _insert_fact(fresh_db, fact_setup, 0.25, "2026-02-10 02:00+00")
    _insert_fact(fresh_db, fact_setup, 0.31, "2026-03-15 02:00+00")  # restatement

    with fresh_db.cursor() as cur:
        cur.execute(PIT_QUERY, (fact_setup["security"], "2026-02-10", "1 day 6 hours"))
        assert [row[0] for row in cur.fetchall()] == [Decimal('0.25')]

        cur.execute(PIT_QUERY, (fact_setup["security"], "2026-03-20", "1 day 6 hours"))
        assert [row[0] for row in cur.fetchall()] == [Decimal('0.31')]


def test_a_restatement_does_not_overwrite_the_original(fresh_db, fact_setup):
    _insert_fact(fresh_db, fact_setup, 0.25, "2026-02-10 02:00+00")
    _insert_fact(fresh_db, fact_setup, 0.31, "2026-03-15 02:00+00")
    with fresh_db.cursor() as cur:
        cur.execute("select count(*) from fundamental_fact")
        assert cur.fetchone()[0] == 2


def test_the_same_fact_cannot_be_recorded_twice_at_one_instant(fresh_db, fact_setup):
    _insert_fact(fresh_db, fact_setup, 0.25, "2026-02-10 02:00+00")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_fact(fresh_db, fact_setup, 0.25, "2026-02-10 02:00+00")


def test_price_rows_route_to_the_correct_yearly_partition(fresh_db, fact_setup):
    fresh_db.execute(
        """
        insert into price_daily
            (security_id, trade_date, open, high, low, close, volume,
             observed_at, ingest_observation_id)
        values (%s, '2026-02-10', 1, 2, 0.5, 1.5, 1000, now(), %s)
        """,
        (fact_setup["security"], fact_setup["observation"]),
    )
    with fresh_db.cursor() as cur:
        cur.execute("select count(*) from price_daily_2026")
        assert cur.fetchone()[0] == 1
