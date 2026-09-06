"""A failed night does not wedge its own date.

Three parts, and all three are needed. The constraint stops counting runs that
failed; a catchable failure marks itself failed on the way out; and a death too
abrupt to record anything is settled by the next run, which is the only thing
that can tell the difference between "still going" and "gone" -- it holds the
lock the dead one would have held.
"""

from datetime import date, timedelta

import psycopg
import pytest

from screener.scoring import (
    SCORING_LOCK_ID,
    ScoringInProgress,
    reconcile,
    run_scoring,
)

AS_OF = date(2026, 3, 2)


@pytest.fixture
def versions(fresh_db):
    """The seeded logic and weight versions, to hang runs off."""
    with fresh_db.cursor() as cur:
        cur.execute("select id from scoring_logic_version order by id limit 1")
        logic = cur.fetchone()[0]
        cur.execute("select id from weight_version where code = 'v1'")
        weight = cur.fetchone()[0]
    return logic, weight


def _run_row(conn, versions, day, outcome, status="live"):
    logic, weight = versions
    return conn.execute(
        """insert into scoring_run
           (as_of_range, cutoff_offset, logic_version_id, weight_version_id,
            status, emits_alerts, git_sha, config_hash, started_at, outcome)
           values (daterange(%s, %s, '[)'), '1 day 6 hours', %s, %s,
                   %s, false, 'abc123', '\\x00'::bytea, now(), %s)
           returning id""",
        (day, day + timedelta(days=1), logic, weight, status, outcome),
    ).fetchone()[0]


def test_a_failed_run_no_longer_holds_its_date(fresh_db, versions):
    # The whole point of migration 020. Before it, this second insert raised.
    _run_row(fresh_db, versions, AS_OF, "failed")

    assert _run_row(fresh_db, versions, AS_OF, "running") is not None


def test_a_run_still_standing_holds_its_date(fresh_db, versions):
    # 004's rule survives 020 intact: the constraint stopped counting failures,
    # it did not stop counting.
    _run_row(fresh_db, versions, AS_OF, "ok")

    with pytest.raises(psycopg.errors.ExclusionViolation):
        _run_row(fresh_db, versions, AS_OF, "running")


def test_a_run_that_never_reported_back_holds_its_date_until_reconciled(
    fresh_db, versions
):
    # 'running' is not 'failed': a night still in flight must keep its date, or
    # two runs would score the same day at once.
    _run_row(fresh_db, versions, AS_OF, "running")

    with pytest.raises(psycopg.errors.ExclusionViolation):
        _run_row(fresh_db, versions, AS_OF, "running")


def test_reconcile_settles_a_run_left_behind_by_a_process_that_died(
    fresh_db, versions
):
    run_id = _run_row(fresh_db, versions, AS_OF, "running")

    assert reconcile(fresh_db) == 1

    outcome, finished = fresh_db.execute(
        "select outcome, finished_at from scoring_run where id = %s", (run_id,)
    ).fetchone()
    assert outcome == "failed"
    # Stamped, because a row saying it ran from Tuesday to never is worse than
    # one that admits when we noticed.
    assert finished is not None


def test_reconcile_leaves_a_finished_run_alone(fresh_db, versions):
    run_id = _run_row(fresh_db, versions, AS_OF, "ok")

    assert reconcile(fresh_db) == 0
    assert (
        fresh_db.execute(
            "select outcome from scoring_run where id = %s", (run_id,)
        ).fetchone()[0]
        == "ok"
    )


def test_a_second_scoring_process_refuses_rather_than_scoring_the_same_night_twice(
    fresh_db, versions, db_url
):
    # The lock is what makes `reconcile` safe: without it, a run in flight is
    # indistinguishable from one whose process is gone, and reconciling would
    # declare a healthy night dead.
    with psycopg.connect(db_url, autocommit=True) as other:
        other.execute("select pg_advisory_lock(%s)", (SCORING_LOCK_ID,))
        try:
            with pytest.raises(ScoringInProgress):
                run_scoring(fresh_db, as_of=AS_OF)
        finally:
            other.execute("select pg_advisory_unlock(%s)", (SCORING_LOCK_ID,))


def test_the_lock_is_released_after_a_run_so_the_next_night_can_take_it(
    fresh_db, versions, db_url
):
    # A session-scoped lock never released would wedge the date as surely as
    # the constraint once did, just for a different reason.
    with pytest.raises(Exception):
        # No securities and no bars, so this fails -- which is the interesting
        # path: the lock has to come back even when the night does not.
        run_scoring(fresh_db, as_of=AS_OF)

    with psycopg.connect(db_url, autocommit=True) as other:
        row = other.execute(
            "select pg_try_advisory_lock(%s)", (SCORING_LOCK_ID,)
        ).fetchone()
        assert row is not None
        taken = row[0]
        if taken:
            other.execute("select pg_advisory_unlock(%s)", (SCORING_LOCK_ID,))
    assert taken, "run_scoring did not release the advisory lock"
