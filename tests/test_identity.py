import pytest
import psycopg


@pytest.fixture
def security_id(fresh_db):
    with fresh_db.cursor() as cur:
        cur.execute(
            """
            insert into security (name, mic, currency, country, primary_symbol, first_seen)
            values ('Apple Inc', 'XNAS', 'USD', 'US', 'AAPL', '2020-01-01')
            returning id
            """
        )
        return cur.fetchone()[0]


def test_currency_must_be_three_characters(fresh_db):
    with pytest.raises(psycopg.errors.CheckViolation):
        fresh_db.execute(
            """
            insert into security (name, mic, currency, country, primary_symbol, first_seen)
            values ('Bad', 'XNAS', 'DOLLAR', 'US', 'BAD', '2020-01-01')
            """
        )


def test_two_securities_cannot_hold_the_same_current_symbol(fresh_db, security_id):
    fresh_db.execute(
        """
        insert into security_symbol (security_id, symbol, mic, valid_from, source)
        values (%s, 'AAPL', 'XNAS', '2020-01-01', 'yfinance')
        """,
        (security_id,),
    )
    with fresh_db.cursor() as cur:
        cur.execute(
            """
            insert into security (name, mic, currency, country, primary_symbol, first_seen)
            values ('Impostor', 'XNAS', 'USD', 'US', 'AAPL', '2021-01-01')
            returning id
            """
        )
        other = cur.fetchone()[0]

    with pytest.raises(psycopg.errors.UniqueViolation):
        fresh_db.execute(
            """
            insert into security_symbol (security_id, symbol, mic, valid_from, source)
            values (%s, 'AAPL', 'XNAS', '2021-01-01', 'yfinance')
            """,
            (other,),
        )


def test_a_retired_symbol_can_be_reissued_to_another_security(fresh_db, security_id):
    fresh_db.execute(
        """
        insert into security_symbol (security_id, symbol, mic, valid_from, valid_to, source)
        values (%s, 'FB', 'XNAS', '2012-05-18', '2022-06-09', 'yfinance')
        """,
        (security_id,),
    )
    with fresh_db.cursor() as cur:
        cur.execute(
            """
            insert into security (name, mic, currency, country, primary_symbol, first_seen)
            values ('Unrelated Co', 'XNAS', 'USD', 'US', 'FB', '2023-01-01')
            returning id
            """
        )
        other = cur.fetchone()[0]

    fresh_db.execute(
        """
        insert into security_symbol (security_id, symbol, mic, valid_from, source)
        values (%s, 'FB', 'XNAS', '2023-01-01', 'yfinance')
        """,
        (other,),
    )


def test_overlapping_symbol_periods_for_one_security_are_rejected(fresh_db, security_id):
    fresh_db.execute(
        """
        insert into security_symbol (security_id, symbol, mic, valid_from, valid_to, source)
        values (%s, 'FB', 'XNAS', '2012-05-18', '2022-06-09', 'yfinance')
        """,
        (security_id,),
    )
    with pytest.raises(psycopg.errors.ExclusionViolation):
        fresh_db.execute(
            """
            insert into security_symbol (security_id, symbol, mic, valid_from, valid_to, source)
            values (%s, 'META', 'XNAS', '2022-01-01', '2023-01-01', 'yfinance')
            """,
            (security_id,),
        )


def test_adjacent_symbol_periods_are_allowed(fresh_db, security_id):
    fresh_db.execute(
        """
        insert into security_symbol (security_id, symbol, mic, valid_from, valid_to, source)
        values (%s, 'FB', 'XNAS', '2012-05-18', '2022-06-09', 'yfinance')
        """,
        (security_id,),
    )
    fresh_db.execute(
        """
        insert into security_symbol (security_id, symbol, mic, valid_from, source)
        values (%s, 'META', 'XNAS', '2022-06-09', 'yfinance')
        """,
        (security_id,),
    )


def test_overlapping_sector_periods_are_rejected(fresh_db, security_id):
    with fresh_db.cursor() as cur:
        cur.execute("insert into sector_scheme (code, name) values ('yf', 'yf') returning id")
        scheme = cur.fetchone()[0]
        cur.execute(
            """
            insert into sector_node (scheme_id, level, code, name)
            values (%s, 1, 'tech', 'Technology') returning id
            """,
            (scheme,),
        )
        tech = cur.fetchone()[0]
        cur.execute(
            """
            insert into sector_node (scheme_id, level, code, name)
            values (%s, 1, 'comms', 'Communications') returning id
            """,
            (scheme,),
        )
        comms = cur.fetchone()[0]

    fresh_db.execute(
        """
        insert into security_sector (security_id, sector_node_id, valid_from, valid_to, source)
        values (%s, %s, '2020-01-01', '2024-01-01', 'yfinance')
        """,
        (security_id, tech),
    )
    with pytest.raises(psycopg.errors.ExclusionViolation):
        fresh_db.execute(
            """
            insert into security_sector (security_id, sector_node_id, valid_from, source)
            values (%s, %s, '2023-01-01', 'yfinance')
            """,
            (security_id, comms),
        )
