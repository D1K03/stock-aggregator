from screener.partitions import ensure_partitions, partition_name


def test_partition_name_is_table_and_year():
    assert partition_name("metric_daily", 2027) == "metric_daily_2027"


def test_ensure_partitions_creates_missing_years(fresh_db):
    created = ensure_partitions(fresh_db, through_year=2027)

    assert "metric_daily_2027" in created
    assert "snapshot_daily_2027" in created
    assert "price_daily_2027" in created
    assert "metric_daily_2026" not in created  # created by the migration


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
