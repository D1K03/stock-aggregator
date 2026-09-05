from pathlib import Path

import pytest

from screener.universe.rows import (
    FIELDNAMES,
    UniverseRow,
    normalise_symbol,
    read_rows,
    slugify,
    write_rows,
)


def row(symbol: str, **over) -> UniverseRow:
    base = dict(
        symbol=symbol, name=f"{symbol} Inc", index_name="sp500", mic="XNAS",
        currency="USD", cik="0000320193", yf_sector="Technology",
        yf_industry="Consumer Electronics", gics_sector="Information Technology",
    )
    base.update(over)
    return UniverseRow(**base)


def test_fieldnames_order_is_the_csv_contract():
    assert FIELDNAMES == (
        "symbol", "name", "index_name", "mic", "currency",
        "cik", "yf_sector", "yf_industry", "gics_sector",
    )


def test_rows_are_written_sorted_by_symbol(tmp_path: Path):
    out = tmp_path / "u.csv"
    write_rows(out, [row("MSFT"), row("AAPL"), row("ZTS")])
    written = [r.symbol for r in read_rows(out)]
    assert written == ["AAPL", "MSFT", "ZTS"]


def test_write_then_read_round_trips(tmp_path: Path):
    out = tmp_path / "u.csv"
    original = [row("AAPL"), row("MSFT", cik="")]
    write_rows(out, original)
    assert read_rows(out) == sorted(original, key=lambda r: r.symbol)


def test_write_returns_the_count(tmp_path: Path):
    assert write_rows(tmp_path / "u.csv", [row("A"), row("B")]) == 2


def test_normalise_symbol_maps_dots_to_dashes():
    assert normalise_symbol(" brk.b ") == "BRK-B"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Financial Services", "financial-services"),
        ("Oil & Gas Integrated", "oil-gas-integrated"),
        ("REIT - Specialty", "reit-specialty"),
    ],
)
def test_slugify(raw, expected):
    assert slugify(raw) == expected
