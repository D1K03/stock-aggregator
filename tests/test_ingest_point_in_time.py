"""What a scoring date may see of the fact layer.

The bitemporal claim in one file: `observed_at` bounds visibility, a
restatement is an insert rather than an update, and a fiscal year does not
collapse into its own fourth quarter.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from screener.ingest import Fact, insert_facts, latest_values, metric_ids, read_facts, record_observation
from screener.scoring import CUTOFF_OFFSET

AS_OF = date(2026, 3, 2)


@pytest.fixture
def facts_ctx(fresh_db):
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


def _write(conn, run_id, security_id, facts, at: datetime):
    """Write facts stamped at `at`, the way a night at that moment would."""
    with conn.cursor() as cur:
        cur.execute(
            """insert into ingest_observation
               (ingest_run_id, security_id, fetched_at, content_hash,
                blob_path, is_new_payload, payload_bytes)
               values (%s, %s, %s, %s, 'p', true, 1) returning id""",
            (run_id, security_id, at, b"\x00" * 32),
        )
        observation_id = cur.fetchone()[0]
        ids = metric_ids(cur)
        held = latest_values(cur, security_id)
        insert_facts(cur, security_id, observation_id, at, facts, held, ids)


def _fact(value, code="revenue", period_end=date(2025, 12, 31), period_type="Q"):
    return Fact(code, period_end, period_type, Decimal(value), "USD")


def test_a_restatement_is_visible_only_after_it_was_observed(fresh_db, facts_ctx):
    """The load-bearing test of the cycle.

    Q2 is reported, then revised. A read as-of a date between the two must
    return the original -- what we actually knew then -- and a read as-of today
    must return the revision.
    """
    security, run = facts_ctx
    monday = datetime(2026, 1, 5, 2, 0, tzinfo=timezone.utc)
    friday = datetime(2026, 2, 20, 2, 0, tzinfo=timezone.utc)
    _write(fresh_db, run, security, [_fact("100")], monday)
    _write(fresh_db, run, security, [_fact("115")], friday)

    early = read_facts(
        fresh_db, [security], as_of=date(2026, 1, 20), cutoff_offset=CUTOFF_OFFSET
    )
    late = read_facts(
        fresh_db, [security], as_of=AS_OF, cutoff_offset=CUTOFF_OFFSET
    )

    assert [f.value for f in early[security]] == [Decimal("100")]
    assert [f.value for f in late[security]] == [Decimal("115")]


def test_a_year_does_not_collapse_into_its_own_fourth_quarter(fresh_db, facts_ctx):
    """D6, against the shape that produced it.

    AAPL reports FY2025 revenue and Q4 FY2025 revenue both ending 2025-09-30,
    four-fold apart. `distinct on (security_id, metric_id, period_end)` -- the
    read the schema spec documents -- returns one of them and silently discards
    the other.
    """
    security, run = facts_ctx
    at = datetime(2026, 1, 5, 2, 0, tzinfo=timezone.utc)
    _write(
        fresh_db, run, security,
        [
            _fact("416161", period_end=date(2025, 9, 30), period_type="A"),
            _fact("102466", period_end=date(2025, 9, 30), period_type="Q"),
        ],
        at,
    )

    held = read_facts(
        fresh_db, [security], as_of=AS_OF, cutoff_offset=CUTOFF_OFFSET
    )[security]

    assert len(held) == 2
    by_type = {f.period_type: f.value for f in held}
    assert by_type == {"A": Decimal("416161"), "Q": Decimal("102466")}


def test_a_fact_observed_after_the_cutoff_is_not_visible(fresh_db, facts_ctx):
    security, run = facts_ctx
    inside = datetime(2026, 3, 3, 5, 0, tzinfo=timezone.utc)
    outside = datetime(2026, 3, 3, 7, 0, tzinfo=timezone.utc)
    _write(fresh_db, run, security, [_fact("100")], inside)
    _write(fresh_db, run, security, [_fact("999")], outside)

    held = read_facts(
        fresh_db, [security], as_of=AS_OF, cutoff_offset=CUTOFF_OFFSET
    )[security]

    assert [f.value for f in held] == [Decimal("100")]


def test_a_withdrawn_value_leaves_the_last_reported_figure_standing(
    fresh_db, facts_ctx
):
    # D11's second half. Yahoo reporting nothing for a period it once reported
    # writes no row, so the last figure stays the point-in-time answer. That is
    # intended -- an absence is not a restatement -- and this pins it so the
    # behaviour cannot change silently.
    security, run = facts_ctx
    _write(
        fresh_db, run, security, [_fact("100")],
        datetime(2026, 1, 5, 2, 0, tzinfo=timezone.utc),
    )
    _write(
        fresh_db, run, security, [],
        datetime(2026, 2, 5, 2, 0, tzinfo=timezone.utc),
    )

    held = read_facts(
        fresh_db, [security], as_of=AS_OF, cutoff_offset=CUTOFF_OFFSET
    )[security]

    assert [f.value for f in held] == [Decimal("100")]


def test_every_period_of_a_metric_comes_back(fresh_db, facts_ctx):
    # The read is per period, not "the latest period" -- a TTM computed later
    # needs four consecutive quarters, so it cannot collapse to one row.
    security, run = facts_ctx
    at = datetime(2026, 1, 5, 2, 0, tzinfo=timezone.utc)
    _write(
        fresh_db, run, security,
        [
            _fact("10", period_end=date(2025, 3, 31)),
            _fact("11", period_end=date(2025, 6, 30)),
            _fact("12", period_end=date(2025, 9, 30)),
            _fact("13", period_end=date(2025, 12, 31)),
        ],
        at,
    )

    held = read_facts(
        fresh_db, [security], as_of=AS_OF, cutoff_offset=CUTOFF_OFFSET
    )[security]

    assert len(held) == 4
    assert sorted(f.value for f in held) == [
        Decimal("10"), Decimal("11"), Decimal("12"), Decimal("13")
    ]


def test_a_security_with_no_visible_facts_is_absent_from_the_mapping(
    fresh_db, facts_ctx
):
    security, _ = facts_ctx
    assert read_facts(
        fresh_db, [security], as_of=AS_OF, cutoff_offset=CUTOFF_OFFSET
    ) == {}


def test_asking_about_nothing_returns_nothing(fresh_db):
    assert read_facts(
        fresh_db, [], as_of=AS_OF, cutoff_offset=CUTOFF_OFFSET
    ) == {}
