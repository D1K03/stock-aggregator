from datetime import date
from decimal import Decimal

from screener.ingest.window import BACKFILL_START, settling_cutoff, windows

TODAY = date(2026, 9, 5)


def a_security(conn, symbol="AAPL"):
    # `security` has six not-null columns beyond the identity key. Inserting
    # fewer fails on the first test, so the fixture carries them all.
    return conn.execute(
        """insert into security
           (name, mic, currency, country, primary_symbol, first_seen)
           values (%s, 'XNAS', 'USD', 'US', %s, '2020-01-01') returning id""",
        (symbol, symbol),
    ).fetchone()[0]


def a_bar(conn, security_id, day, close="100"):
    from conftest import an_observation

    conn.execute(
        """insert into price_daily
           (security_id, trade_date, open, high, low, close, volume,
            observed_at, ingest_observation_id)
           values (%s, %s, %s, %s, %s, %s, %s, now(), %s)""",
        (security_id, day, close, close, close, close, 1,
         an_observation(conn, security_id)),
    )


def test_a_security_with_no_rows_backfills_from_2020(fresh_db):
    sid = a_security(fresh_db)
    assert windows(fresh_db, [sid], today=TODAY) == {sid: BACKFILL_START}


def test_a_security_held_to_yesterday_fetches_only_the_settling_window(fresh_db):
    sid = a_security(fresh_db)
    a_bar(fresh_db, sid, date(2026, 9, 4))
    # Held to yesterday, so held+1 is today; the settling cutoff is earlier and
    # wins, because in-window bars must be re-fetched to be re-settled.
    assert windows(fresh_db, [sid], today=TODAY) == {sid: settling_cutoff(TODAY)}


def test_a_security_three_days_stale_fetches_the_gap_plus_the_window(fresh_db):
    sid = a_security(fresh_db)
    a_bar(fresh_db, sid, date(2026, 9, 1))
    assert windows(fresh_db, [sid], today=TODAY) == {sid: settling_cutoff(TODAY)}


def test_a_security_stale_by_a_month_fetches_back_to_where_it_stopped(fresh_db):
    sid = a_security(fresh_db)
    a_bar(fresh_db, sid, date(2026, 8, 1))
    # The fetch window widens to close the gap. The settling window does not.
    assert windows(fresh_db, [sid], today=TODAY) == {sid: date(2026, 8, 2)}


def test_every_requested_security_gets_an_answer(fresh_db):
    held = a_security(fresh_db, "AAPL")
    fresh = a_security(fresh_db, "MSFT")
    a_bar(fresh_db, held, date(2026, 8, 1))
    got = windows(fresh_db, [held, fresh], today=TODAY)
    assert set(got) == {held, fresh}
    assert got[fresh] == BACKFILL_START


def test_the_settling_cutoff_is_seven_calendar_days():
    assert settling_cutoff(date(2026, 9, 5)) == date(2026, 8, 29)
