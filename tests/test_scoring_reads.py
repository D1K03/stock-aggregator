"""What a scoring date is allowed to see.

`observed_at <= as_of + cutoff_offset`, on prices as well as on fundamentals
(spec D10). Live and backfill runs then evaluate the identical expression,
which is the whole reason a later backtest could mean anything.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from screener.scoring import (
    CUTOFF_OFFSET,
    active_securities,
    read_actions,
    read_bars,
    visibility_cutoff,
)

AS_OF = date(2026, 3, 2)


@pytest.fixture
def priced(fresh_db, an_observation):
    """One active security with an observation to hang facts off."""
    security = fresh_db.execute(
        """insert into security
           (name, mic, currency, country, primary_symbol, first_seen)
           values ('Alpha', 'XNAS', 'USD', 'US', 'AAA', '2020-01-01') returning id"""
    ).fetchone()[0]
    return security, an_observation(security)


def _bar(conn, security, observation, day, close, observed_at):
    conn.execute(
        """insert into price_daily
           (security_id, trade_date, open, high, low, close, volume,
            observed_at, ingest_observation_id)
           values (%s, %s, %s, %s, %s, %s, 1, %s, %s)""",
        (security, day, close, close, close, close, observed_at, observation),
    )


def test_the_cutoff_is_the_offset_past_midnight_utc_on_the_scoring_date():
    assert visibility_cutoff(AS_OF, CUTOFF_OFFSET) == datetime(
        2026, 3, 3, 6, 0, tzinfo=timezone.utc
    )


def test_the_default_offset_covers_a_run_at_two_in_the_morning():
    assert CUTOFF_OFFSET == timedelta(days=1, hours=6)


def test_a_bar_observed_after_the_cutoff_is_not_visible(fresh_db, priced):
    security, observation = priced
    inside = datetime(2026, 3, 3, 5, 0, tzinfo=timezone.utc)
    outside = datetime(2026, 3, 3, 7, 0, tzinfo=timezone.utc)
    _bar(fresh_db, security, observation, date(2026, 2, 27), Decimal(100), inside)
    # Same trade_date qualifies; only `observed_at` disqualifies it.
    _bar(fresh_db, security, observation, date(2026, 3, 2), Decimal(200), outside)

    got = read_bars(fresh_db, [security], as_of=AS_OF, cutoff_offset=CUTOFF_OFFSET)

    assert got[security] == [(date(2026, 2, 27), Decimal(100))]


def test_bars_after_the_scoring_date_are_not_read(fresh_db, priced):
    security, observation = priced
    seen = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _bar(fresh_db, security, observation, date(2026, 3, 2), Decimal(100), seen)
    _bar(fresh_db, security, observation, date(2026, 3, 3), Decimal(999), seen)

    got = read_bars(fresh_db, [security], as_of=AS_OF, cutoff_offset=CUTOFF_OFFSET)

    assert [day for day, _ in got[security]] == [date(2026, 3, 2)]


def test_bars_come_back_in_date_order_per_security(fresh_db, priced):
    security, observation = priced
    seen = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for day in (date(2026, 2, 20), date(2026, 1, 5), date(2026, 2, 1)):
        _bar(fresh_db, security, observation, day, Decimal(100), seen)

    got = read_bars(fresh_db, [security], as_of=AS_OF, cutoff_offset=CUTOFF_OFFSET)

    assert [day for day, _ in got[security]] == sorted(day for day, _ in got[security])


def test_a_security_with_no_visible_bars_is_absent_from_the_mapping(fresh_db, priced):
    security, _ = priced

    assert read_bars(fresh_db, [security], as_of=AS_OF, cutoff_offset=CUTOFF_OFFSET) == {}


def test_actions_honour_the_same_cutoff(fresh_db, priced):
    security, observation = priced
    inside = datetime(2026, 3, 3, 5, 0, tzinfo=timezone.utc)
    outside = datetime(2026, 3, 3, 7, 0, tzinfo=timezone.utc)
    for observed_at, ratio in ((inside, 2), (outside, 5)):
        fresh_db.execute(
            """insert into corporate_action
               (security_id, effective_date, action_type, ratio, observed_at,
                ingest_observation_id)
               values (%s, '2026-02-02', 'split', %s, %s, %s)""",
            (security, ratio, observed_at, observation),
        )

    got = read_actions(fresh_db, [security], as_of=AS_OF, cutoff_offset=CUTOFF_OFFSET)

    assert [a.ratio for a in got[security]] == [Decimal(2)]


def test_only_active_securities_are_scored(fresh_db, priced):
    security, _ = priced
    retired = fresh_db.execute(
        """insert into security
           (name, mic, currency, country, primary_symbol, first_seen, is_active)
           values ('Gone', 'XNAS', 'USD', 'US', 'ZZZ', '2020-01-01', false)
           returning id"""
    ).fetchone()[0]

    got = active_securities(fresh_db)

    assert security in got and retired not in got
