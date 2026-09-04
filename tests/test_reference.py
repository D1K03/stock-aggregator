import pytest
import psycopg


def test_btree_gist_extension_is_installed(fresh_db):
    with fresh_db.cursor() as cur:
        cur.execute("select 1 from pg_extension where extname = 'btree_gist'")
        assert cur.fetchone() is not None


def test_metric_cadence_rejects_unknown_value(fresh_db):
    fresh_db.execute(
        "insert into pillar (code, name) values ('quality', 'Quality')"
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        fresh_db.execute(
            """
            insert into metric (code, name, pillar_id, unit, higher_is_better, cadence)
            values ('roic', 'ROIC', (select id from pillar where code = 'quality'),
                    'ratio', true, 'hourly')
            """
        )


def test_sector_node_level_is_constrained_to_sector_or_industry(fresh_db):
    fresh_db.execute("insert into sector_scheme (code, name) values ('yfinance', 'yfinance')")
    with pytest.raises(psycopg.errors.CheckViolation):
        fresh_db.execute(
            """
            insert into sector_node (scheme_id, level, code, name)
            values ((select id from sector_scheme where code = 'yfinance'), 3, 'x', 'X')
            """
        )


def test_sector_node_code_is_unique_within_a_scheme(fresh_db):
    fresh_db.execute("insert into sector_scheme (code, name) values ('yfinance', 'yfinance')")
    fresh_db.execute(
        """
        insert into sector_node (scheme_id, level, code, name)
        values ((select id from sector_scheme where code = 'yfinance'), 1, 'tech', 'Technology')
        """
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        fresh_db.execute(
            """
            insert into sector_node (scheme_id, level, code, name)
            values ((select id from sector_scheme where code = 'yfinance'), 1, 'tech', 'Dup')
            """
        )
