import pytest
import psycopg

from screener.migrate import applied_versions, apply_migrations


def test_apply_migrations_runs_files_in_order(empty_db, tmp_path):
    (tmp_path / "001_first.sql").write_text("create table a (id int);")
    (tmp_path / "002_second.sql").write_text("create table b (id int);")

    applied = apply_migrations(empty_db, tmp_path)

    assert applied == ["001_first", "002_second"]
    assert applied_versions(empty_db) == {"001_first", "002_second"}


def test_apply_migrations_is_idempotent(empty_db, tmp_path):
    (tmp_path / "001_first.sql").write_text("create table a (id int);")

    assert apply_migrations(empty_db, tmp_path) == ["001_first"]
    assert apply_migrations(empty_db, tmp_path) == []


def test_failing_migration_rolls_back_and_is_not_recorded(empty_db, tmp_path):
    (tmp_path / "001_first.sql").write_text("create table a (id int);")
    (tmp_path / "002_bad.sql").write_text("this is not valid sql;")

    with pytest.raises(psycopg.Error):
        apply_migrations(empty_db, tmp_path)

    assert applied_versions(empty_db) == {"001_first"}
    with empty_db.cursor() as cur:
        cur.execute("select to_regclass('public.a'), to_regclass('public.b')")
        assert cur.fetchone() == ("a", None)
