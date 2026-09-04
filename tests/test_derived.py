import pytest
import psycopg


def test_metric_daily_is_range_partitioned_on_as_of(fresh_db):
    with fresh_db.cursor() as cur:
        cur.execute(
            """
            select partstrat
            from pg_partitioned_table
            where partrelid = 'metric_daily'::regclass
            """
        )
        assert cur.fetchone()[0] == "r"


def test_yearly_and_monthly_partitions_coexist_on_one_parent(fresh_db):
    """The spec defers monthly partitioning by mixing granularity, never converting."""
    fresh_db.execute(
        """
        create table metric_daily_2027_01 partition of metric_daily
            for values from ('2027-01-01') to ('2027-02-01')
        """
    )
    with fresh_db.cursor() as cur:
        cur.execute(
            """
            select count(*) from pg_inherits where inhparent = 'metric_daily'::regclass
            """
        )
        assert cur.fetchone()[0] >= 2


def test_a_single_column_foreign_key_into_snapshot_daily_is_rejected(fresh_db):
    """Records the invariant: nothing holds an FK into a partitioned table.

    Postgres 12+ permits an FK referencing a partitioned table, but only against a
    unique constraint containing the partition key — and snapshot_daily has no
    surrogate id to point at.
    """
    with pytest.raises(psycopg.errors.InvalidForeignKey):
        fresh_db.execute(
            """
            create table would_break (
                id bigint generated always as identity primary key,
                snapshot_security_id bigint references snapshot_daily(security_id)
            )
            """
        )


@pytest.fixture
def derived_setup(fresh_db):
    """Real rows for every foreign key the derived tables require.

    Without these the inserts below would trip a foreign-key violation, and the
    test would pass for the wrong reason.
    """
    with fresh_db.cursor() as cur:
        cur.execute(
            """
            insert into security (name, mic, currency, country, primary_symbol, first_seen)
            values ('Apple Inc', 'XNAS', 'USD', 'US', 'AAPL', '2020-01-01') returning id
            """
        )
        security = cur.fetchone()[0]
        cur.execute("insert into pillar (code, name) values ('quality', 'Q') returning id")
        pillar = cur.fetchone()[0]
        cur.execute(
            """
            insert into metric (code, name, pillar_id, unit, higher_is_better, cadence)
            values ('roic', 'ROIC', %s, 'ratio', true, 'quarterly') returning id
            """,
            (pillar,),
        )
        metric = cur.fetchone()[0]
        cur.execute("insert into sector_scheme (code, name) values ('yf', 'yf') returning id")
        scheme = cur.fetchone()[0]
        cur.execute(
            """
            insert into peer_group (scheme_id, level, code)
            values (%s, 0, 'market') returning id
            """,
            (scheme,),
        )
        peer_group = cur.fetchone()[0]
        cur.execute("insert into weight_version (code) values ('v1') returning id")
        weight = cur.fetchone()[0]
        cur.execute(
            "insert into scoring_logic_version (description) values ('initial') returning id"
        )
        logic = cur.fetchone()[0]
        cur.execute(
            """
            insert into scoring_run (
                as_of_range, cutoff_offset, logic_version_id, weight_version_id,
                status, emits_alerts, git_sha, config_hash, started_at, outcome
            )
            values (daterange('2026-02-10', '2026-02-11', '[)'), '1 day 6 hours', %s, %s,
                    'live', true, 'abc123', '\\x00'::bytea, now(), 'ok')
            returning id
            """,
            (logic, weight),
        )
        run = cur.fetchone()[0]
    return {
        "security": security,
        "metric": metric,
        "peer_group": peer_group,
        "run": run,
    }


def test_peer_group_stat_requires_eleven_deciles(fresh_db, derived_setup):
    with pytest.raises(psycopg.errors.CheckViolation):
        fresh_db.execute(
            """
            insert into peer_group_stat
                (as_of, scoring_run_id, peer_group_id, metric_id, member_count, deciles)
            values ('2026-02-10', %s, %s, %s, 30, array[1, 2, 3]::numeric[])
            """,
            (derived_setup["run"], derived_setup["peer_group"], derived_setup["metric"]),
        )


def test_eleven_deciles_are_accepted(fresh_db, derived_setup):
    fresh_db.execute(
        """
        insert into peer_group_stat
            (as_of, scoring_run_id, peer_group_id, metric_id, member_count, deciles)
        values ('2026-02-10', %s, %s, %s, 30,
                array[0,1,2,3,4,5,6,7,8,9,10]::numeric[])
        """,
        (derived_setup["run"], derived_setup["peer_group"], derived_setup["metric"]),
    )


def test_percentile_outside_zero_to_one_hundred_is_rejected(fresh_db, derived_setup):
    with pytest.raises(psycopg.errors.CheckViolation):
        fresh_db.execute(
            """
            insert into metric_daily
                (as_of, scoring_run_id, security_id, metric_id, raw_value,
                 percentile, peer_group_id, peer_count, fallback_level)
            values ('2026-02-10', %s, %s, %s, 0.25, 101, %s, 30, 2)
            """,
            (derived_setup["run"], derived_setup["security"],
             derived_setup["metric"], derived_setup["peer_group"]),
        )


def test_snapshot_daily_has_no_weight_version_column(fresh_db):
    """Redundant with scoring_run; a second copy would eventually disagree."""
    with fresh_db.cursor() as cur:
        cur.execute(
            """
            select count(*) from information_schema.columns
            where table_name = 'snapshot_daily' and column_name = 'weight_version_id'
            """
        )
        assert cur.fetchone()[0] == 0
