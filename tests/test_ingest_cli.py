import pytest

from screener.ingest.cli import main


def test_an_unknown_command_exits_rather_than_guessing():
    with pytest.raises(SystemExit):
        main(["nonsense"])


def test_the_commands_are_exactly_prices_and_sweep(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "prices" in out and "sweep" in out


def test_delay_defaults_to_zero_because_nothing_has_hit_a_limit():
    import argparse

    from screener.ingest.cli import build_parser

    parser: argparse.ArgumentParser = build_parser()
    assert parser.parse_args(["prices"]).delay == 0.0


def test_a_today_in_the_past_is_refused_before_anything_is_fetched(caplog):
    # `--today` moves the settling cutoff with it, so a past date would put every
    # bar Yahoo returns on the upsert path and rewrite rows that had long since
    # settled. D3 keeps the permission to overwrite from travelling with the
    # fetch window; a backdated run would hand it over entirely.
    from datetime import date, timedelta

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    assert main(["prices", "--today", yesterday]) == 1
    assert "in the past" in caplog.text


def test_today_itself_is_still_accepted():
    from datetime import date

    from screener.ingest.cli import build_parser

    parsed = build_parser().parse_args(["prices", "--today", date.today().isoformat()])
    assert parsed.today == date.today()
