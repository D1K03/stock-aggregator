"""The chart tool, and the concept data it draws from.

Two things are defended here. That the annotation Steven puts on a chart is
computed from the series rather than chosen by a model — a marker in the wrong
place is a lie told precisely. And that the Python copy of the concept data
still agrees with the TypeScript one the dashboard renders, because they are
separate files in separate build contexts and nothing but this test stops them
drifting apart.
"""

import json
import re
from pathlib import Path

import pytest

from screener import concept
from screener.bot.tools import TOOLS, dispatch
from screener.bot.tools.charts import MARKS, collecting

DATA_TS = Path(__file__).resolve().parent.parent / "web" / "lib" / "data.ts"


def _series(symbol: str) -> list[float]:
    """The series for a symbol that is expected to exist."""
    row = concept.find(symbol)
    assert row is not None, symbol
    return concept.series(row)


# -- the concept data mirrors the dashboard's ------------------------------


def _parse_rows() -> list[tuple[str, int, int]]:
    """(symbol, score, prev) out of the TypeScript ROWS table."""
    source = DATA_TS.read_text()
    body = source.split("export const ROWS: Row[] = [", 1)[1].split("];", 1)[0]
    return [
        (m["sym"], int(m["score"]), int(m["prev"]))
        for m in re.finditer(
            r'sym:\s*"(?P<sym>[A-Z.]+)".*?score:\s*(?P<score>\d+),\s*prev:\s*(?P<prev>\d+)',
            body,
        )
    ]


@pytest.mark.skipif(not DATA_TS.exists(), reason="the web app is not in this checkout")
def test_the_python_concept_rows_match_the_dashboards():
    # Two copies of the same invented data, one per language. A chart in chat
    # that disagreed with the chart on the page would read as a broken screener
    # rather than as two files that drifted.
    assert [(r.sym, r.score, r.prev) for r in concept.ROWS] == _parse_rows()


@pytest.mark.skipif(not DATA_TS.exists(), reason="the web app is not in this checkout")
def test_the_alert_threshold_matches_the_dashboards():
    found = re.search(r"export const THRESHOLD = (\d+)", DATA_TS.read_text())
    assert found and int(found[1]) == concept.THRESHOLD


def test_the_series_reproduces_the_dashboards_walk_exactly():
    # Values captured from the TypeScript `history()`. The generator is 32-bit
    # integer arithmetic kept inside float64 in both languages, so this is an
    # exact match rather than a close one; anything else means the port drifted.
    nvda = concept.series(concept.ROWS[1])
    assert concept.ROWS[1].sym == "NVDA"
    assert len(nvda) == concept.SPAN
    assert nvda[0] == 53.23316271053627
    assert nvda[30] == 66.15803773103495
    # The walk arrives at yesterday and today is appended, so the last step is
    # the move an alert would have fired on.
    assert nvda[-2] == 68.0
    assert nvda[-1] == 82.0


def test_a_ticker_is_found_by_symbol_or_name():
    # Asked to "chart Nvidia", a symbol-only lookup would fail and the model
    # would spend another paid round guessing.
    assert concept.find("nvda") is concept.find("$NVDA") is concept.find("NVIDIA")
    assert concept.find("Eli Lilly") is not None
    assert concept.find("TSLA") is None
    assert concept.find("") is None


# -- the tool --------------------------------------------------------------


def test_the_chart_tool_is_registered_with_its_marks_in_the_description():
    # The model picks a mark from this string. If a mark is added and the
    # description is not, it can only be reached by guessing.
    assert "chart" in TOOLS
    for mark in MARKS:
        assert mark in TOOLS["chart"].description


def test_a_chart_is_collected_rather_than_returned_to_the_model():
    # Sixty points would exceed the whole tool-result budget and be re-sent on
    # every following round. The model gets a sentence; the chart goes around.
    with collecting() as drawn:
        result = dispatch("chart", {"ticker": "NVDA", "mark": "peak"})
    assert len(drawn) == 1
    assert drawn[0].ticker == "NVDA"
    assert len(drawn[0].series) == concept.SPAN
    assert "53.23" not in result and "66.15" not in result


def test_the_marked_point_is_computed_from_the_series_not_supplied():
    with collecting() as drawn:
        dispatch("chart", {"ticker": "MSFT", "mark": "peak"})
    values = _series("MSFT")
    mark = drawn[0].marks[0]
    assert mark.kind == "point"
    assert values[mark.index] == max(values)


def test_a_surge_spans_the_steepest_run_and_says_how_long_it_took():
    with collecting() as drawn:
        dispatch("chart", {"ticker": "CAT", "mark": "surge"})
    mark = drawn[0].marks[0]
    values = _series("CAT")
    assert mark.kind == "span" and mark.end is not None
    assert mark.end > mark.index
    assert values[mark.end] > values[mark.index]
    # The label carries the size and the length, so the chart answers "when was
    # its biggest surge" without the reply having to restate it.
    assert f"over {mark.end - mark.index}d" in mark.label


def test_a_crossing_is_marked_where_the_line_changes_sides():
    with collecting() as drawn:
        dispatch("chart", {"ticker": "NVDA", "mark": "crossing"})
    mark = drawn[0].marks[0]
    values = _series("NVDA")
    below_before = values[mark.index - 1] < concept.THRESHOLD
    below_after = values[mark.index] < concept.THRESHOLD
    assert below_before != below_after


def test_a_ticker_that_does_not_exist_names_the_ones_that_do():
    # Otherwise the model spends a whole paid round guessing another symbol.
    with collecting() as drawn:
        result = dispatch("chart", {"ticker": "TSLA"})
    assert drawn == []
    assert "NVDA" in result and "error" in result


def test_an_unknown_mark_is_refused_rather_than_silently_ignored():
    # Quietly drawing an unmarked chart would have Steven describe a marker
    # that is not on it.
    with collecting() as drawn:
        result = dispatch("chart", {"ticker": "NVDA", "mark": "wibble"})
    assert drawn == []
    assert "error" in result


def test_every_result_says_the_data_is_illustrative():
    # The one claim that must survive every path: these are invented numbers,
    # and the model is told so every single time it reads them.
    with collecting():
        for mark in ("", *MARKS):
            assert "llustrative" in dispatch("chart", {"ticker": "AMD", "mark": mark})


def test_a_surface_that_cannot_draw_is_not_told_a_chart_is_shown():
    # Discord renders text. Claiming a chart there points the reader at
    # something that does not exist.
    with collecting(False) as drawn:
        result = dispatch("chart", {"ticker": "NVDA", "mark": "peak"})
    assert drawn == []
    assert "chart is shown" not in result
    assert "Peak" in result


def test_charts_do_not_leak_between_questions():
    # `collecting` is a ContextVar because each request runs on its own worker
    # thread; two people asking at once must not be handed each other's charts.
    with collecting() as first:
        dispatch("chart", {"ticker": "NVDA"})
    with collecting() as second:
        dispatch("chart", {"ticker": "DOW"})
    assert [c.ticker for c in first] == ["NVDA"]
    assert [c.ticker for c in second] == ["DOW"]


def test_the_payload_is_json_and_keeps_the_marks():
    with collecting() as drawn:
        dispatch("chart", {"ticker": "JPM", "mark": "low"})
    body = json.loads(json.dumps(drawn[0].payload()))
    assert len(body["series"]) == concept.SPAN
    assert len(body["dates"]) == concept.SPAN
    assert body["marks"][0]["kind"] == "point"
    # Rounded for transport: a hundredth of a point is far under one pixel.
    assert all(round(v, 2) == v for v in body["series"])
