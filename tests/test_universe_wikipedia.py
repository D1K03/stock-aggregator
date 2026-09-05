import httpx
import pytest

from screener.universe.sources.wikipedia import SourceError, constituents

PAGE = """
<html><body>
<table class="wikitable sortable" id="constituents">
<tr><th>Symbol</th><th>Security</th><th>GICS Sector</th><th>GICS Sub-Industry</th></tr>
<tr><td><a href="/x">AAPL</a></td><td>Apple Inc.</td><td>Information Technology</td><td>Tech Hardware</td></tr>
<tr><td><a href="/y">BRK.B</a></td><td>Berkshire Hathaway</td><td>Financials</td><td>Multi-Sector</td></tr>
</table>
</body></html>
"""

EMPTY = "<html><body><p>no tables here</p></body></html>"


def transport_for(body: str, status: int = 200) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(status, text=body))


def test_parses_symbol_name_and_sector():
    rows = constituents(transport=transport_for(PAGE))
    aapl = next(r for r in rows if r.symbol == "AAPL")
    assert aapl.name == "Apple Inc."
    assert aapl.gics_sector == "Information Technology"


def test_share_class_symbols_are_normalised():
    rows = constituents(transport=transport_for(PAGE))
    assert "BRK-B" in {r.symbol for r in rows}
    assert "BRK.B" not in {r.symbol for r in rows}


def test_every_row_is_tagged_with_its_index():
    rows = constituents(transport=transport_for(PAGE))
    assert {r.index_name for r in rows} == {"sp500", "sp400", "sp600"}


def test_a_page_with_no_usable_table_raises():
    with pytest.raises(SourceError, match="no constituent table"):
        constituents(transport=transport_for(EMPTY))


def test_an_http_failure_propagates():
    with pytest.raises(Exception):
        constituents(transport=transport_for("nope", status=500))
