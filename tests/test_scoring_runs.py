import pytest
import psycopg


@pytest.fixture
def versions(fresh_db):
    """The seeded weight version and logic version, to hang runs off.

    Both are seeded by 019 now. `weight_version.code` is unique, so inserting a
    second 'v1' here would raise rather than test anything.
    """
    with fresh_db.cursor() as cur:
        cur.execute("select id from weight_version where code = 'v1'")
        weight = cur.fetchone()[0]
        cur.execute("select id from scoring_logic_version order by id limit 1")
        logic = cur.fetchone()[0]
    return weight, logic


def _insert_run(conn, versions, start, end, status, emits_alerts=False):
    weight, logic = versions
    conn.execute(
        """
        insert into scoring_run (
            as_of_range, cutoff_offset, logic_version_id, weight_version_id,
            status, emits_alerts, git_sha, config_hash, started_at, outcome
        )
        values (daterange(%s, %s, '[)'), '1 day 6 hours', %s, %s,
                %s, %s, 'abc123', '\\x00'::bytea, now(), 'ok')
        """,
        (start, end, logic, weight, status, emits_alerts),
    )


def test_two_overlapping_live_runs_are_rejected(fresh_db, versions):
    _insert_run(fresh_db, versions, "2026-01-01", "2026-01-02", "live")
    with pytest.raises(psycopg.errors.ExclusionViolation):
        _insert_run(fresh_db, versions, "2026-01-01", "2026-01-02", "live")


def test_a_backfill_may_overlap_a_live_run(fresh_db, versions):
    _insert_run(fresh_db, versions, "2026-01-01", "2026-01-02", "live")
    _insert_run(fresh_db, versions, "2026-01-01", "2026-01-02", "backfill")


def test_two_experiments_may_overlap_each_other(fresh_db, versions):
    _insert_run(fresh_db, versions, "2026-01-01", "2026-02-01", "experiment")
    _insert_run(fresh_db, versions, "2026-01-01", "2026-02-01", "experiment")


def test_non_overlapping_live_runs_are_allowed(fresh_db, versions):
    _insert_run(fresh_db, versions, "2026-01-01", "2026-01-02", "live")
    _insert_run(fresh_db, versions, "2026-01-02", "2026-01-03", "live")


def test_status_rejects_unknown_value(fresh_db, versions):
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_run(fresh_db, versions, "2026-01-01", "2026-01-02", "provisional")


def test_pillar_weights_reject_negative_values(fresh_db, versions):
    weight, _ = versions
    with pytest.raises(psycopg.errors.CheckViolation):
        fresh_db.execute(
            """
            insert into pillar_weight (weight_version_id, pillar_id, weight)
            values (%s, (select id from pillar where code = 'quality'), -0.5)
            """,
            (weight,),
        )
