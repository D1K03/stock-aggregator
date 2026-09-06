from decimal import Decimal

import pytest
import psycopg


@pytest.fixture
def alert_setup(fresh_db):
    with fresh_db.cursor() as cur:
        cur.execute(
            """
            insert into security (name, mic, currency, country, primary_symbol, first_seen)
            values ('Apple Inc', 'XNAS', 'USD', 'US', 'AAPL', '2020-01-01') returning id
            """
        )
        security = cur.fetchone()[0]
        cur.execute("select id from weight_version where code = 'v1'")
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
        cur.execute(
            """
            insert into alert_rule (code, name, condition_type, params, cooldown_days, min_coverage)
            values ('cross80', 'Score crosses 80', 'score_crossing',
                    '{"threshold": 80}'::jsonb, 5, 0.6)
            returning id
            """
        )
        rule = cur.fetchone()[0]
    return {"security": security, "run": run, "rule": rule}


def _fire(conn, setup, as_of="2026-02-10"):
    conn.execute(
        """
        insert into alert_event
            (alert_rule_id, security_id, as_of, scoring_run_id, fired_at,
             blended_score, previous_blended_score, pillar_scores, raw_inputs,
             driver, delivery_status)
        values (%s, %s, %s, %s, now(), 82, 68,
                '{"quality": 91}'::jsonb, '{"roic": 0.25}'::jsonb,
                'three upward revisions in 5 days', 'pending')
        """,
        (setup["rule"], setup["security"], as_of, setup["run"]),
    )


def test_the_same_alert_cannot_fire_twice_for_one_date(fresh_db, alert_setup):
    """Makes a re-run of the daily job idempotent rather than double-posting."""
    _fire(fresh_db, alert_setup)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _fire(fresh_db, alert_setup)


def test_previous_blended_score_may_be_null_for_non_crossing_rules(fresh_db, alert_setup):
    fresh_db.execute(
        """
        insert into alert_event
            (alert_rule_id, security_id, as_of, scoring_run_id, fired_at,
             blended_score, pillar_scores, raw_inputs, driver, delivery_status)
        values (%s, %s, '2026-02-10', %s, now(), 82,
                '{"insider": 77}'::jsonb, '{"net_buying": 1200000}'::jsonb,
                'insider buying appeared', 'pending')
        """,
        (alert_setup["rule"], alert_setup["security"], alert_setup["run"]),
    )
    with fresh_db.cursor() as cur:
        cur.execute(
            """
            select blended_score, previous_blended_score, driver, delivery_status
            from alert_event
            where alert_rule_id = %s and security_id = %s and as_of = '2026-02-10'
            """,
            (alert_setup["rule"], alert_setup["security"]),
        )
        blended_score, previous_blended_score, driver, delivery_status = cur.fetchone()
        assert blended_score == Decimal(82)
        assert previous_blended_score is None
        assert driver == "insider buying appeared"
        assert delivery_status == "pending"


def test_undelivered_alerts_are_findable(fresh_db, alert_setup):
    _fire(fresh_db, alert_setup, as_of="2026-02-10")
    _fire(fresh_db, alert_setup, as_of="2026-02-11")
    with fresh_db.cursor() as cur:
        cur.execute(
            "update alert_event set delivery_status = 'sent' where as_of = '2026-02-10'"
        )
        cur.execute(
            "select as_of from alert_event where delivery_status <> 'sent'"
        )
        rows = cur.fetchall()
        assert [r[0].isoformat() for r in rows] == ["2026-02-11"]


def test_alert_state_direction_is_constrained(fresh_db, alert_setup):
    with pytest.raises(psycopg.errors.CheckViolation):
        fresh_db.execute(
            """
            insert into alert_state
                (alert_rule_id, security_id, last_fired_at, last_fired_as_of,
                 cooldown_until, last_direction)
            values (%s, %s, now(), '2026-02-10', '2026-02-15', 0)
            """,
            (alert_setup["rule"], alert_setup["security"]),
        )


def test_alert_state_holds_one_row_per_rule_and_security(fresh_db, alert_setup):
    fresh_db.execute(
        """
        insert into alert_state
            (alert_rule_id, security_id, last_fired_at, last_fired_as_of,
             cooldown_until, last_direction)
        values (%s, %s, now(), '2026-02-10', '2026-02-15', 1)
        """,
        (alert_setup["rule"], alert_setup["security"]),
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        fresh_db.execute(
            """
            insert into alert_state
                (alert_rule_id, security_id, last_fired_at, last_fired_as_of,
                 cooldown_until, last_direction)
            values (%s, %s, now(), '2026-02-11', '2026-02-16', -1)
            """,
            (alert_setup["rule"], alert_setup["security"]),
        )
