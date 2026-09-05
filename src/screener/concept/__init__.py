"""Invented sample data for the dashboard concept.

    from screener.concept import ROWS, find, series

Delete this package when ingest lands. It exists so the dashboard and the chart
tool have something to draw; every surface that shows it says so.
"""

from screener.concept.data import (
    MEDIANS,
    PEERS,
    PILLAR_NAMES,
    ROWS,
    SPAN,
    THRESHOLD,
    Row,
    dates,
    find,
    series,
)

__all__ = [
    "MEDIANS", "PEERS", "PILLAR_NAMES", "ROWS", "SPAN", "THRESHOLD",
    "Row", "dates", "find", "series",
]
