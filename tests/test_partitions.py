from decimal import Decimal

from screener.partitions import ensure_partitions, partition_name


def test_partition_name_is_table_and_year():
    assert partition_name("metric_daily", 2027) == "metric_daily_2027"


def test_ensure_partitions_creates_missing_years(fresh_db):
    created = ensure_partitions(fresh_db, through_year=2027)

    assert "metric_daily_2027" in created
    assert "snapshot_daily_2027" in created
    assert "price_daily_2027" in created
    assert "metric_daily_2026" not in created  # created by the migration
    assert "price_daily_2025" not in created  # created by migration 008


def test_ensure_partitions_is_idempotent(fresh_db):
    ensure_partitions(fresh_db, through_year=2027)
    assert ensure_partitions(fresh_db, through_year=2027) == []


def test_rows_route_into_a_newly_created_partition(fresh_db):
    ensure_partitions(fresh_db, through_year=2027)
    with fresh_db.cursor() as cur:
        cur.execute(
            """
            insert into security (name, mic, currency, country, primary_symbol, first_seen)
            values ('Apple Inc', 'XNAS', 'USD', 'US', 'AAPL', '2020-01-01') returning id
            """
        )
        security = cur.fetchone()[0]
        cur.execute("insert into weight_version (code) values ('v1') returning id")
        weight = cur.fetchone()[0]
        cur.execute(
            "insert into scoring_logic_version (description) values ('x') returning id"
        )
        logic = cur.fetchone()[0]
        cur.execute(
            """
            insert into scoring_run (
                as_of_range, cutoff_offset, logic_version_id, weight_version_id,
                status, emits_alerts, git_sha, config_hash, started_at, outcome
            )
            values (daterange('2027-03-01', '2027-03-02', '[)'), '1 day 6 hours', %s, %s,
                    'live', true, 'abc', '\\x00'::bytea, now(), 'ok')
            returning id
            """,
            (logic, weight),
        )
        run = cur.fetchone()[0]
        cur.execute(
            """
            insert into snapshot_daily
                (as_of, scoring_run_id, security_id, blended_score,
                 pillar_agreement, min_coverage, worst_fallback_level)
            values ('2027-03-01', %s, %s, 82, 3, 0.8, 2)
            """,
            (run, security),
        )
        cur.execute("select count(*) from snapshot_daily_2027")
        assert cur.fetchone()[0] == 1


def test_2025_price_row_lands_in_price_daily_2025(fresh_db):
    """Migration 008: price_daily needs history predating the first scoring date."""
    with fresh_db.cursor() as cur:
        cur.execute(
            """
            insert into security (name, mic, currency, country, primary_symbol, first_seen)
            values ('Apple Inc', 'XNAS', 'USD', 'US', 'AAPL', '2020-01-01') returning id
            """
        )
        security = cur.fetchone()[0]
        cur.execute(
            "insert into data_source (code, name) values ('yf', 'yfinance') returning id"
        )
        source = cur.fetchone()[0]
        cur.execute(
            """
            insert into ingest_run
                (source_id, endpoint, started_at, status, securities_requested, securities_ok)
            values (%s, 'prices', now(), 'ok', 1, 1)
            returning id
            """,
            (source,),
        )
        ingest_run = cur.fetchone()[0]
        cur.execute(
            """
            insert into ingest_observation
                (ingest_run_id, security_id, fetched_at, content_hash, blob_path,
                 is_new_payload, payload_bytes)
            values (%s, %s, now(), '\\xab'::bytea, 'blob/1', true, 10)
            returning id
            """,
            (ingest_run, security),
        )
        observation = cur.fetchone()[0]
        cur.execute(
            """
            insert into price_daily
                (security_id, trade_date, open, high, low, close, volume,
                 observed_at, ingest_observation_id)
            values (%s, '2025-06-02', 100, 101, 99, 100.5, 1000000, now(), %s)
            """,
            (security, observation),
        )
        cur.execute("select count(*) from price_daily_2025")
        assert cur.fetchone()[0] == 1


def test_monthly_partitions_prevent_a_later_yearly_partition(fresh_db):
    """Once a year is covered by monthly partitions, ensure_partitions must not
    try to add an overlapping yearly partition for that same year — it should
    neither raise nor create one."""
    fresh_db.execute(
        """
        create table metric_daily_2028_01 partition of metric_daily
            for values from ('2028-01-01') to ('2028-02-01')
        """
    )
    fresh_db.execute(
        """
        create table metric_daily_2028_02 partition of metric_daily
            for values from ('2028-02-01') to ('2028-03-01')
        """
    )
    created = ensure_partitions(fresh_db, through_year=2028)
    assert "metric_daily_2028" not in created
