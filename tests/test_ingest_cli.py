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
