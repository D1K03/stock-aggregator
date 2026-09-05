"""S&P constituent lists.

The only free complete source of index membership, and the fragile layer
`DESIGN.md` warns about — which is why it runs in the quarterly refresh and never
on the daily path. Parsed with the stdlib HTML parser rather than pandas or lxml:
reading three tables does not justify a dependency.
"""

from dataclasses import dataclass
from html.parser import HTMLParser

import httpx

from screener.fetch import fetch
from screener.universe.rows import normalise_symbol

PAGES: dict[str, str] = {
    "sp500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "sp400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
    "sp600": "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
}

HEADERS = {"User-Agent": "screener/0.1 (universe refresh; +https://github.com/D1K03/stock-aggregator)"}


class SourceError(RuntimeError):
    """A page loaded but did not contain what we came for."""


@dataclass(frozen=True)
class Constituent:
    symbol: str
    name: str
    gics_sector: str
    index_name: str


class _TableParser(HTMLParser):
    """Collects every table as a list of rows of cell text."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            self._table.append(self._row)
            self._row = None
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _pick(header: list[str], *wanted: str) -> int | None:
    for index, cell in enumerate(header):
        low = cell.lower()
        if any(w in low for w in wanted):
            return index
    return None


def _parse(html: str, index_name: str) -> list[Constituent]:
    parser = _TableParser()
    parser.feed(html)
    for table in parser.tables:
        if len(table) < 2:
            continue
        header = table[0]
        sym = _pick(header, "symbol", "ticker")
        name = _pick(header, "security", "company")
        sector = _pick(header, "sector")
        if sym is None or name is None or sector is None:
            continue
        rows = [
            Constituent(
                symbol=normalise_symbol(r[sym]),
                name=r[name].strip(),
                gics_sector=r[sector].strip(),
                index_name=index_name,
            )
            for r in table[1:]
            if len(r) > max(sym, name, sector) and r[sym].strip()
        ]
        if rows:
            return rows
    raise SourceError(f"no constituent table found on the {index_name} page")


def constituents(*, transport: httpx.BaseTransport | None = None) -> list[Constituent]:
    """Every S&P 1500 member, tagged with which of the three lists it came from."""
    out: list[Constituent] = []
    for index_name, url in PAGES.items():
        result = fetch(url, headers=HEADERS, transport=transport)
        out.extend(_parse(result.text, index_name))
    return out
