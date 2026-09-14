"""A row parses to the same record whichever connection it came through (ui-swap F12).

The status service's own cursor returns `Decimal`, `date` and `datetime`;
`playground.select` returns the same cells made JSON-safe -- decimals as strings,
dates and times as ISO strings. These build the second form exactly as
`playground.engine._cell` writes it.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from screener.playground import Result
from screener.screen import BarRow, RunRow, ScreenRow, parse, parse_all

STARTED = datetime(2026, 9, 13, 23, 0, 4, 120000, tzinfo=timezone.utc)


def _result(*rows: tuple) -> Result:
    return Result(
        columns=(), rows=rows, row_count=len(rows), truncated=False, shortened=0, ms=0,
        limit=len(rows),
    )


def test_a_run_parses_the_same_from_a_cursor_and_from_the_playground():
    native = (7, date(2026, 9, 13), STARTED, None, "6b1c111", "9f3a71c2", "v2", 3, 108000,
              "v2 momentum", False)
    played = (7, "2026-09-13", STARTED.isoformat(), None, "6b1c111", "9f3a71c2", "v2", 3,
              108000, "v2 momentum", False)

    from_playground = parse(RunRow, _result(played).rows[0])

    assert parse(RunRow, native) == from_playground
    assert isinstance(from_playground.as_of, date)
    assert from_playground.started_at == STARTED


def test_decimals_come_back_as_decimals_and_nulls_stay_null():
    native = (11, "GEF", "Greif", "consumer-cyclical", "Consumer Cyclical", Decimal("84.8"),
              None, None, None, Decimal("79.0"), Decimal("0.75"), Decimal("91.0"),
              Decimal("1"), 2, Decimal("0"))
    played = (11, "GEF", "Greif", "consumer-cyclical", "Consumer Cyclical", "84.8",
              None, None, None, "79.0", "0.75", "91.0", "1", 2, "0")

    from_playground = parse(ScreenRow, played)

    assert parse(ScreenRow, native) == from_playground
    assert isinstance(from_playground.q_coverage, Decimal)
    assert from_playground.v_score is None


def test_a_bar_keeps_its_fetch_time_exactly():
    # `adjusted_closes` decides per bar whether a split still applies by comparing
    # `observed_at` with the split's date, so a fetch time must survive the trip.
    rows = parse_all(BarRow, _result((4, "2026-03-02", "41.20", STARTED.isoformat())).rows)

    assert rows == [BarRow(4, date(2026, 3, 2), Decimal("41.20"), STARTED)]


def test_a_row_of_the_wrong_width_is_refused():
    with pytest.raises(ValueError, match="BarRow takes 4 cells, got 3"):
        parse(BarRow, (4, date(2026, 3, 2), Decimal("1")))
