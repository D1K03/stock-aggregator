from datetime import date, timedelta

import pytest

from screener.scoring.cli import build_parser, main


def test_an_unknown_command_exits_rather_than_guessing():
    with pytest.raises(SystemExit):
        main(["nonsense"])


def test_run_is_the_only_command_so_far(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])

    assert "run" in capsys.readouterr().out


def test_as_of_defaults_to_none_so_the_run_date_is_today():
    assert build_parser().parse_args(["run"]).as_of is None


def test_an_as_of_in_the_past_is_refused_before_anything_is_read(caplog):
    # The one-live-run-per-date exclusion constraint makes a backdated live run
    # a conflict rather than a convenience. Historical runs are
    # `status='backfill'`, which this cycle does not build.
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    assert main(["run", "--as-of", yesterday]) == 1
    assert "in the past" in caplog.text


def test_today_itself_is_accepted_by_the_parser():
    parsed = build_parser().parse_args(["run", "--as-of", date.today().isoformat()])

    assert parsed.as_of == date.today()


def test_a_failed_run_logs_the_recovery_sql(monkeypatch, caplog):
    # The constraint keys on `status`, not `outcome`, so a run that dies after
    # opening its row leaves the date wedged: no live run for that date can be
    # opened again until the stale row is cleared. A failed run wrote nothing
    # (the writes roll back whole), so deleting it is safe -- the CLI names
    # that recovery at the moment of failure rather than leaving an operator
    # at 02:00 to work it out.
    import psycopg

    from screener.scoring import cli as cli_module

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_connect(*args, **kwargs):
        return FakeConn()

    def fake_run_scoring(*args, **kwargs):
        raise cli_module.NoBarsVisible("no bars visible")

    monkeypatch.setattr(cli_module, "load_into_environ", lambda: None)
    monkeypatch.setattr(cli_module, "settings", lambda: type("S", (), {"database_url": "x"})())
    monkeypatch.setattr(psycopg, "connect", fake_connect)
    monkeypatch.setattr(cli_module, "run_scoring", fake_run_scoring)

    assert main(["run"]) == 1
    assert "delete from scoring_run" in caplog.text
    assert date.today().isoformat() in caplog.text
