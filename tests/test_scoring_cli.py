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


def test_a_failed_run_names_the_date_and_says_it_is_scorable_again(monkeypatch, caplog):
    # A run that dies after opening its row is marked `failed`, and after
    # migration 020 that is what stops it holding the date. So the thing worth
    # saying at 02:00 is which date is affected and that re-running is the
    # whole of the fix -- not a `delete` an operator has to paste.
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
    assert "no longer holds the date" in caplog.text
    assert "python -m screener.scoring run --as-of" in caplog.text
    assert date.today().isoformat() in caplog.text


def test_an_exclusion_violation_says_the_standing_run_is_not_to_be_cleared(
    monkeypatch, caplog
):
    # After 020 this no longer means a stale row from a failed night. It means
    # a run that genuinely stands -- finished `ok`, or still in flight -- so
    # the message must NOT tell anyone to clear it or to re-run.
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
        raise psycopg.errors.ExclusionViolation("conflicting key value")

    monkeypatch.setattr(cli_module, "load_into_environ", lambda: None)
    monkeypatch.setattr(cli_module, "settings", lambda: type("S", (), {"database_url": "x"})())
    monkeypatch.setattr(psycopg, "connect", fake_connect)
    monkeypatch.setattr(cli_module, "run_scoring", fake_run_scoring)

    assert main(["run"]) == 1
    assert "has not failed" in caplog.text
    assert date.today().isoformat() in caplog.text
    # The one thing it must not do is invite clearing a run that stands.
    assert "delete from scoring_run" not in caplog.text
    assert "re-run" not in caplog.text


def test_any_other_failure_after_the_run_row_opens_also_names_the_date(
    monkeypatch, caplog
):
    # `open_run` commits before `score()` runs, so anything else `score()`
    # raises -- not one of the named types -- still leaves a row behind. The
    # broad handler is the backstop for that case.
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
        raise KeyError("unexpected")

    monkeypatch.setattr(cli_module, "load_into_environ", lambda: None)
    monkeypatch.setattr(cli_module, "settings", lambda: type("S", (), {"database_url": "x"})())
    monkeypatch.setattr(psycopg, "connect", fake_connect)
    monkeypatch.setattr(cli_module, "run_scoring", fake_run_scoring)

    assert main(["run"]) == 1
    assert "no longer holds the date" in caplog.text
    assert date.today().isoformat() in caplog.text


def test_a_run_refused_for_the_lock_does_not_offer_the_recovery_note(
    monkeypatch, caplog
):
    # No run row was opened and nothing is wedged, so "re-run it" would be
    # wrong advice: someone else is scoring, and the answer is to wait.
    import psycopg

    from screener.scoring import cli as cli_module

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_run_scoring(*args, **kwargs):
        raise cli_module.ScoringInProgress("another scoring run holds the lock")

    monkeypatch.setattr(cli_module, "load_into_environ", lambda: None)
    monkeypatch.setattr(
        cli_module, "settings", lambda: type("S", (), {"database_url": "x"})()
    )
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: FakeConn())
    monkeypatch.setattr(cli_module, "run_scoring", fake_run_scoring)

    assert main(["run"]) == 1
    assert "holds the lock" in caplog.text
    assert "re-run" not in caplog.text
