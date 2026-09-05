# Universe and Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two commands — `refresh-universe` builds a committed CSV from Wikipedia, SEC and Yahoo; `load-universe` reconciles that CSV into the identity tables.

**Architecture:** A `screener.universe` package mirroring `screener.boot`'s argparse entry point. Only `load` touches Postgres; only `refresh` touches the network. The reconciler is split in two: `reconcile.plan()` is a pure function from CSV rows plus current database state to a `Plan`, tested without a database; `load.apply()` executes a `Plan` in one transaction, tested against a real one.

**Tech Stack:** Python 3.11+, httpx via `screener.fetch`, psycopg 3, pytest, Postgres 16.

**Spec:** `docs/specs/2026-09-05-universe-and-identity.md`

## Global Constraints

- `screener.fetch.fetch()` is the only way to make an HTTP request. It takes `transport=` for tests; no test may reach the network.
- `refresh` must never open a database connection. `load` must never open a socket. This is the invariant the whole design rests on.
- Entry point mirrors `screener.boot`: `python -m screener.universe <command>`, argparse with a positional `command` and a `choices` tuple.
- Database access uses `screener.config.settings().database_url` and psycopg 3.
- All SQL identifiers lowercase; `text` never `varchar(n)`; `timestamptz` never `timestamp`; `numeric` never float.
- Temporal rows use half-open `daterange(valid_from, valid_to, '[)')`, so closing at `valid_to = D` and opening at `valid_from = D` do not overlap.
- Yahoo `quoteSummary` needs `modules=assetProfile,price` — `assetProfile` carries neither `exchange` nor `currency`.
- SEC returns **403** without a declared `User-Agent` carrying contact details.
- No `yfinance`. Yahoo is called directly.

---

## File structure

| File | Responsibility |
|---|---|
| `src/screener/universe/__init__.py` | Public surface: `UniverseRow`, `Plan`, `refresh`, `plan`, `apply` |
| `src/screener/universe/__main__.py` | `raise SystemExit(main())` |
| `src/screener/universe/cli.py` | argparse `main(argv)`, dispatch to refresh/load |
| `src/screener/universe/rows.py` | `UniverseRow`, `ExistingSecurity`, CSV read/write, slugify |
| `src/screener/universe/sources/wikipedia.py` | constituent tables → symbol/name/gics_sector |
| `src/screener/universe/sources/sec.py` | `company_tickers.json` → ticker→CIK |
| `src/screener/universe/sources/yahoo.py` | crumb handling, `assetProfile,price` → sector/industry/mic/currency |
| `src/screener/universe/refresh.py` | orchestrate the three sources into two CSV files |
| `src/screener/universe/reconcile.py` | pure `plan(rows, existing) -> Plan` |
| `src/screener/universe/load.py` | read state, apply a `Plan` in one transaction, taxonomy + peer groups |

---

### Task 1: Package skeleton, row types, CSV contract

**Files:**
- Create: `src/screener/universe/__init__.py`, `__main__.py`, `cli.py`, `rows.py`
- Test: `tests/test_universe_rows.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `UniverseRow` frozen dataclass with fields `symbol, name, index_name, mic, currency, cik, yf_sector, yf_industry, gics_sector` — all `str`, `cik` may be `""`.
  - `FIELDNAMES: tuple[str, ...]` in that order.
  - `write_rows(path: Path, rows: Iterable[UniverseRow]) -> int` — sorts by `symbol`, returns count written.
  - `read_rows(path: Path) -> list[UniverseRow]`.
  - `slugify(text: str) -> str` — lowercase, non-alphanumerics to `-`, collapsed, stripped.
  - `normalise_symbol(text: str) -> str` — strip, uppercase, `.` → `-`.
  - `screener.universe.cli.main(argv: list[str] | None = None) -> int`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_universe_rows.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_universe_rows.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'screener.universe'`

- [ ] **Step 3: Write `src/screener/universe/rows.py`**

```python
"""The CSV contract: one row per security, sorted, stable field order.

The file is committed, so a diff is the review surface for both membership
changes and reclassifications. That only works if the ordering is deterministic
and the field order never drifts, which is why both are pinned here rather than
left to whatever a dict happens to iterate.
"""

import csv
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

FIELDNAMES: tuple[str, ...] = (
    "symbol",
    "name",
    "index_name",
    "mic",
    "currency",
    "cik",
    "yf_sector",
    "yf_industry",
    "gics_sector",
)


@dataclass(frozen=True)
class UniverseRow:
    symbol: str
    name: str
    index_name: str
    mic: str
    currency: str
    cik: str
    yf_sector: str
    yf_industry: str
    gics_sector: str


def normalise_symbol(text: str) -> str:
    """Wikipedia writes share classes as `BRK.B`; Yahoo wants `BRK-B`."""
    return text.strip().upper().replace(".", "-")


def slugify(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


def write_rows(path: Path, rows: Iterable[UniverseRow]) -> int:
    ordered = sorted(rows, key=lambda r: r.symbol)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(FIELDNAMES))
        writer.writeheader()
        for row in ordered:
            writer.writerow(asdict(row))
    return len(ordered)


def read_rows(path: Path) -> list[UniverseRow]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [UniverseRow(**r) for r in csv.DictReader(handle)]
```

- [ ] **Step 4: Write `src/screener/universe/cli.py`**

```python
"""Entry point, shaped like `screener.boot` so the two read the same."""

import argparse
import logging

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="screener.universe", description=__doc__)
    parser.add_argument(
        "command",
        choices=("refresh", "load"),
        help=(
            "refresh: rebuild data/universe.csv from Wikipedia, SEC and Yahoo "
            "(no database). load: reconcile that CSV into Postgres (no network)."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="load: print the plan, change nothing")
    parser.add_argument("--force", action="store_true", help="load: proceed past the departure ceiling")
    parser.add_argument("--as-of", default=None, help="load: date to stamp changes with (default today)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    raise NotImplementedError(args.command)
```

- [ ] **Step 5: Write `src/screener/universe/__init__.py` and `__main__.py`**

```python
# __init__.py
"""Choosing, refreshing and loading the ticker universe.

Two commands with a hard split: `refresh` talks to the network and never opens a
database connection; `load` talks to the database and never opens a socket. The
committed CSV between them is what makes a reclassification reviewable before it
can move a score.
"""

from screener.universe.rows import (
    FIELDNAMES,
    UniverseRow,
    normalise_symbol,
    read_rows,
    slugify,
    write_rows,
)

__all__ = [
    "FIELDNAMES",
    "UniverseRow",
    "normalise_symbol",
    "read_rows",
    "slugify",
    "write_rows",
]
```

```python
# __main__.py
from screener.universe.cli import main

raise SystemExit(main())
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_universe_rows.py -v && pyright`
Expected: 8 passed, pyright 0 errors

- [ ] **Step 7: Commit**

```bash
git add src/screener/universe tests/test_universe_rows.py
git commit -m "feat: universe package skeleton and CSV contract"
```

---

### Task 2: Wikipedia constituent source

**Files:**
- Create: `src/screener/universe/sources/__init__.py`, `src/screener/universe/sources/wikipedia.py`
- Test: `tests/test_universe_wikipedia.py`

**Interfaces:**
- Consumes: `normalise_symbol` from Task 1; `screener.fetch.fetch`.
- Produces: `PAGES: dict[str, str]` mapping `sp500`/`sp400`/`sp600` to URLs, and
  `constituents(*, transport=None) -> list[Constituent]` where `Constituent` is a frozen dataclass
  with `symbol: str`, `name: str`, `gics_sector: str`, `index_name: str`.
- Raises `SourceError` (defined here, subclass of `RuntimeError`) when a page yields no usable table.

Parsing uses the stdlib `html.parser`, not pandas or lxml — the project has neither, and adding a
dependency to read three tables is not warranted.

- [ ] **Step 1: Write the failing test**

Create `tests/test_universe_wikipedia.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_universe_wikipedia.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'screener.universe.sources'`

- [ ] **Step 3: Write `src/screener/universe/sources/__init__.py`**

```python
"""Where universe data comes from. Each module returns plain rows, never writes."""
```

- [ ] **Step 4: Write `src/screener/universe/sources/wikipedia.py`**

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_universe_wikipedia.py -v && pyright`
Expected: 5 passed, pyright 0 errors

- [ ] **Step 6: Commit**

```bash
git add src/screener/universe/sources tests/test_universe_wikipedia.py
git commit -m "feat: parse S&P constituent lists"
```

---

### Task 3: SEC ticker-to-CIK source

**Files:**
- Create: `src/screener/universe/sources/sec.py`
- Test: `tests/test_universe_sec.py`

**Interfaces:**
- Consumes: `screener.fetch.fetch`, `normalise_symbol`.
- Produces: `cik_by_symbol(*, transport=None) -> dict[str, str]` mapping normalised symbol to a
  10-digit zero-padded CIK, and `SEC_URL`, `SEC_HEADERS`.

**SEC returns 403 without a declared User-Agent.** It asks for an identifying string with contact
details. This is a hard block, not a courtesy, and a test asserts the header is sent.

- [ ] **Step 1: Write the failing test**

Create `tests/test_universe_sec.py`:

```python
import json

import httpx

from screener.universe.sources.sec import cik_by_symbol

PAYLOAD = json.dumps(
    {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
        "2": {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY"},
    }
)


def recording_transport(body: str, status: int = 200):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, text=body)

    return httpx.MockTransport(handler), seen


def test_maps_symbol_to_zero_padded_cik():
    transport, _ = recording_transport(PAYLOAD)
    assert cik_by_symbol(transport=transport)["AAPL"] == "0000320193"


def test_cik_is_always_ten_digits():
    transport, _ = recording_transport(PAYLOAD)
    assert all(len(v) == 10 for v in cik_by_symbol(transport=transport).values())


def test_symbols_are_normalised():
    transport, _ = recording_transport(PAYLOAD)
    assert "BRK-B" in cik_by_symbol(transport=transport)


def test_a_user_agent_is_declared_because_sec_returns_403_without_one():
    transport, seen = recording_transport(PAYLOAD)
    cik_by_symbol(transport=transport)
    agent = seen[0].headers.get("user-agent", "")
    assert agent and "screener" in agent.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_universe_sec.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'screener.universe.sources.sec'`

- [ ] **Step 3: Write `src/screener/universe/sources/sec.py`**

```python
"""Ticker to CIK, from SEC's published filer list.

CIK is the identity anchor the reconciler matches on. A symbol is a mutable
attribute — match on it and a rename reads as a departure plus an unrelated
arrival, orphaning the company's history behind a dead ticker.
"""

import httpx

from screener.fetch import fetch
from screener.universe.rows import normalise_symbol

SEC_URL = "https://www.sec.gov/files/company_tickers.json"

# SEC returns 403 to requests without a declared User-Agent carrying contact
# details. This is enforced, not advisory.
SEC_HEADERS = {
    "User-Agent": "screener/0.1 (universe refresh; +https://github.com/D1K03/stock-aggregator)"
}


def cik_by_symbol(*, transport: httpx.BaseTransport | None = None) -> dict[str, str]:
    """Every US filer's ticker mapped to its zero-padded 10-digit CIK."""
    result = fetch(SEC_URL, headers=SEC_HEADERS, transport=transport)
    payload = result.json()
    out: dict[str, str] = {}
    for entry in payload.values():
        ticker = entry.get("ticker")
        cik = entry.get("cik_str")
        if ticker and cik is not None:
            out[normalise_symbol(str(ticker))] = f"{int(cik):010d}"
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_universe_sec.py -v && pyright`
Expected: 4 passed, pyright 0 errors

- [ ] **Step 5: Commit**

```bash
git add src/screener/universe/sources/sec.py tests/test_universe_sec.py
git commit -m "feat: map tickers to CIK from SEC"
```

---

### Task 4: Yahoo profile source, including crumb expiry

**Files:**
- Create: `src/screener/universe/sources/yahoo.py`
- Test: `tests/test_universe_yahoo.py`

**Interfaces:**
- Consumes: `screener.fetch.fetch`.
- Produces:
  - `Profile` frozen dataclass: `sector: str`, `industry: str`, `mic: str`, `currency: str`.
  - `MIC_BY_EXCHANGE: dict[str, str]`.
  - `class YahooClient` with `__init__(self, *, transport=None)`, `crumb_fetches: int` attribute,
    and `profile(self, symbol: str) -> Profile | None` returning `None` when the symbol is unresolvable.

**Crumb expiry is main-line behaviour, not an edge case.** The crumb expires on Yahoo's schedule.
A run that fetches one at 22:00 and is still working at 02:00 meets a 401 in the ordinary course
of events, so refresh-and-retry is tested as normal flow. A second consecutive 401 gives up on
that symbol rather than looping.

- [ ] **Step 1: Write the failing test**

Create `tests/test_universe_yahoo.py`:

```python
import json

import httpx

from screener.universe.sources.yahoo import YahooClient

PROFILE = json.dumps(
    {
        "quoteSummary": {
            "result": [
                {
                    "assetProfile": {"sector": "Technology", "industry": "Consumer Electronics"},
                    "price": {"exchange": "NMS", "currency": "USD"},
                }
            ]
        }
    }
)
UNAUTHORISED = json.dumps({"finance": {"error": {"code": "Unauthorized", "description": "Invalid Crumb"}}})
NOT_FOUND = json.dumps({"quoteSummary": {"result": None, "error": {"code": "Not Found"}}})


def scripted(script):
    """A transport driven by a list of (url_fragment, status, body) in order."""
    seen: list[httpx.Request] = []
    remaining = list(script)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        fragment, status, body = remaining.pop(0)
        assert fragment in str(request.url), f"expected {fragment} in {request.url}"
        return httpx.Response(status, text=body)

    return httpx.MockTransport(handler), seen


def test_profile_reads_sector_industry_mic_and_currency():
    transport, _ = scripted([
        ("fc.yahoo.com", 200, "ok"),
        ("getcrumb", 200, "CRUMB1"),
        ("quoteSummary/AAPL", 200, PROFILE),
    ])
    got = YahooClient(transport=transport).profile("AAPL")
    assert got is not None
    assert (got.sector, got.industry, got.mic, got.currency) == (
        "Technology", "Consumer Electronics", "XNAS", "USD",
    )


def test_the_request_asks_for_both_modules_because_assetprofile_lacks_exchange():
    transport, seen = scripted([
        ("fc.yahoo.com", 200, "ok"),
        ("getcrumb", 200, "CRUMB1"),
        ("quoteSummary/AAPL", 200, PROFILE),
    ])
    YahooClient(transport=transport).profile("AAPL")
    url = str(seen[-1].url)
    assert "assetProfile" in url and "price" in url


def test_the_crumb_is_fetched_once_and_reused_across_symbols():
    transport, _ = scripted([
        ("fc.yahoo.com", 200, "ok"),
        ("getcrumb", 200, "CRUMB1"),
        ("quoteSummary/AAPL", 200, PROFILE),
        ("quoteSummary/MSFT", 200, PROFILE),
    ])
    client = YahooClient(transport=transport)
    client.profile("AAPL")
    client.profile("MSFT")
    assert client.crumb_fetches == 1


def test_a_401_refreshes_the_crumb_and_retries_with_the_new_one():
    transport, seen = scripted([
        ("fc.yahoo.com", 200, "ok"),
        ("getcrumb", 200, "CRUMB1"),
        ("quoteSummary/AAPL", 401, UNAUTHORISED),
        ("fc.yahoo.com", 200, "ok"),
        ("getcrumb", 200, "CRUMB2"),
        ("quoteSummary/AAPL", 200, PROFILE),
    ])
    client = YahooClient(transport=transport)
    assert client.profile("AAPL") is not None
    assert client.crumb_fetches == 2
    assert "CRUMB2" in str(seen[-1].url)


def test_a_second_consecutive_401_gives_up_rather_than_looping():
    transport, _ = scripted([
        ("fc.yahoo.com", 200, "ok"),
        ("getcrumb", 200, "CRUMB1"),
        ("quoteSummary/AAPL", 401, UNAUTHORISED),
        ("fc.yahoo.com", 200, "ok"),
        ("getcrumb", 200, "CRUMB2"),
        ("quoteSummary/AAPL", 401, UNAUTHORISED),
    ])
    assert YahooClient(transport=transport).profile("AAPL") is None


def test_a_429_backs_off_and_retries():
    """The spec promises backoff on 429. Measurement says it should not fire at
    this scale, which is exactly why it needs a test rather than a live trigger."""
    transport, _ = scripted([
        ("fc.yahoo.com", 200, "ok"),
        ("getcrumb", 200, "CRUMB1"),
        ("quoteSummary/AAPL", 429, "slow down"),
        ("quoteSummary/AAPL", 200, PROFILE),
    ])
    slept: list[float] = []
    client = YahooClient(transport=transport, sleep=slept.append, backoff=0.5)
    assert client.profile("AAPL") is not None
    assert slept == [0.5]


def test_an_unknown_symbol_returns_none():
    transport, _ = scripted([
        ("fc.yahoo.com", 200, "ok"),
        ("getcrumb", 200, "CRUMB1"),
        ("quoteSummary/CWEN-A", 404, NOT_FOUND),
    ])
    assert YahooClient(transport=transport).profile("CWEN-A") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_universe_yahoo.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'screener.universe.sources.yahoo'`

- [ ] **Step 3: Write `src/screener/universe/sources/yahoo.py`**

```python
"""Company profile from Yahoo, called directly.

`quoteSummary` needs a crumb; `assetProfile` alone does not carry exchange or
currency, so `price` is requested alongside it. Both come back in one request.

No yfinance: it emits parsed DataFrames, and a payload stored from it would be
the library's reshaping rather than the response — which would break the
restatement detector and leave "did the API lie or did our parser?" unanswerable.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from screener.fetch import fetch

# Two refreshable failures (one 401, one 429) plus the retry each earns, then stop.
_MAX_ATTEMPTS = 4
BASE = "https://query1.finance.yahoo.com"
COOKIE_URL = "https://fc.yahoo.com"
CRUMB_URL = f"{BASE}/v1/test/getcrumb"
MODULES = "assetProfile,price"

BROWSER = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

MIC_BY_EXCHANGE: dict[str, str] = {
    "NMS": "XNAS",
    "NGM": "XNAS",
    "NCM": "XNAS",
    "NYQ": "XNYS",
    "PCX": "ARCX",
    "ASE": "XASE",
    "BTS": "BATS",
}


@dataclass(frozen=True)
class Profile:
    sector: str
    industry: str
    mic: str
    currency: str


class YahooClient:
    """Holds the crumb across symbols and refreshes it when Yahoo expires it."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
        backoff: float = 1.0,
    ) -> None:
        self._transport = transport
        self._crumb: str | None = None
        self._sleep = sleep or time.sleep
        self._backoff = backoff
        self.crumb_fetches = 0

    def _fetch_crumb(self) -> str:
        fetch(COOKIE_URL, headers=BROWSER, transport=self._transport, allow_empty=True)
        result = fetch(CRUMB_URL, headers=BROWSER, transport=self._transport)
        self.crumb_fetches += 1
        self._crumb = result.text.strip()
        return self._crumb

    def _crumbed(self) -> str:
        return self._crumb or self._fetch_crumb()

    def profile(self, symbol: str) -> Profile | None:
        """Sector, industry, MIC and currency, or None if Yahoo will not say.

        A 401 means the crumb expired, which over a long run is ordinary rather
        than exceptional: refresh once and retry. A 429 means we are going too
        fast, so back off and retry. Either way a second consecutive failure of
        the same kind stops here — retrying forever would hang the refresh, and
        the departure ceiling in `load` catches a CSV that came back mostly empty.
        """
        backoff = self._backoff
        refreshed = False
        for _ in range(_MAX_ATTEMPTS):
            crumb = self._crumbed()
            raw = _get(
                f"{BASE}/v10/finance/quoteSummary/{symbol}?modules={MODULES}&crumb={crumb}",
                self._transport,
            )
            if raw is None:
                return None
            if raw.status_code == 401 and not refreshed:
                self._crumb = None
                refreshed = True
                continue
            if raw.status_code == 429:
                self._sleep(backoff)
                backoff *= 2
                continue
            if raw.status_code != 200:
                return None
            return _parse(raw.text)
        return None


def _get(url: str, transport: httpx.BaseTransport | None) -> httpx.Response | None:
    """One request, returning the response whatever its status.

    `fetch()` raises on a non-2xx, but a 401 here is information rather than a
    failure — it is how Yahoo says the crumb expired — so the status is needed
    intact. Everything else is a genuine failure and becomes None.
    """
    client = httpx.Client(transport=transport, follow_redirects=True, timeout=25.0)
    try:
        return client.get(url, headers=BROWSER)
    except httpx.HTTPError:
        return None
    finally:
        client.close()


def _parse(text: str) -> Profile | None:
    import json

    try:
        result = json.loads(text)["quoteSummary"]["result"]
    except (KeyError, TypeError, ValueError):
        return None
    if not result:
        return None
    profile = result[0].get("assetProfile") or {}
    price = result[0].get("price") or {}
    sector = (profile.get("sector") or "").strip()
    industry = (profile.get("industry") or "").strip()
    if not sector:
        return None
    exchange = (price.get("exchange") or "").strip()
    return Profile(
        sector=sector,
        industry=industry,
        mic=MIC_BY_EXCHANGE.get(exchange, exchange),
        currency=(price.get("currency") or "USD").strip(),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_universe_yahoo.py -v && pyright`
Expected: 7 passed, pyright 0 errors

- [ ] **Step 5: Commit**

```bash
git add src/screener/universe/sources/yahoo.py tests/test_universe_yahoo.py
git commit -m "feat: Yahoo profile source with crumb refresh"
```

---

### Task 5: Refresh orchestration

**Files:**
- Create: `src/screener/universe/refresh.py`
- Test: `tests/test_universe_refresh.py`

**Interfaces:**
- Consumes: `constituents()`, `cik_by_symbol()`, `YahooClient`, `write_rows`, `UniverseRow`.
- Produces: `refresh(out_dir: Path, *, transport=None, delay: float = 0.0, client=None, sleep=None) -> RefreshReport`
  where `RefreshReport` is a frozen dataclass with `written: int`, `unresolved: int`.
  Writes `out_dir/universe.csv` and `out_dir/universe-unresolved.csv`.
- `UNRESOLVED_FIELDNAMES: tuple[str, ...] = ("symbol", "name", "index_name", "reason")`

**This function must never touch a database.** A test asserts `screener.config.settings` is not called.

- [ ] **Step 1: Write the failing test**

Create `tests/test_universe_refresh.py`:

```python
import csv
from pathlib import Path

import pytest

from screener.universe.refresh import refresh
from screener.universe.sources.wikipedia import Constituent
from screener.universe.sources.yahoo import Profile


class FakeClient:
    """Stands in for YahooClient; returns a profile for known symbols only."""

    def __init__(self, known: dict[str, Profile]) -> None:
        self.known = known
        self.asked: list[str] = []

    def profile(self, symbol: str) -> Profile | None:
        self.asked.append(symbol)
        return self.known.get(symbol)


PROFILE = Profile(sector="Technology", industry="Consumer Electronics", mic="XNAS", currency="USD")


@pytest.fixture
def sources(monkeypatch):
    monkeypatch.setattr(
        "screener.universe.refresh.constituents",
        lambda **_: [
            Constituent("AAPL", "Apple Inc.", "Information Technology", "sp500"),
            Constituent("CWEN-A", "Clearway A", "Utilities", "sp400"),
        ],
    )
    monkeypatch.setattr(
        "screener.universe.refresh.cik_by_symbol",
        lambda **_: {"AAPL": "0000320193"},
    )


def test_resolved_rows_go_to_universe_csv(tmp_path: Path, sources):
    report = refresh(tmp_path, client=FakeClient({"AAPL": PROFILE}))
    assert report.written == 1
    rows = list(csv.DictReader((tmp_path / "universe.csv").open()))
    assert rows[0]["symbol"] == "AAPL"
    assert rows[0]["yf_sector"] == "Technology"
    assert rows[0]["cik"] == "0000320193"
    assert rows[0]["gics_sector"] == "Information Technology"


def test_unresolved_rows_go_to_their_own_file_with_a_reason(tmp_path: Path, sources):
    report = refresh(tmp_path, client=FakeClient({"AAPL": PROFILE}))
    assert report.unresolved == 1
    rows = list(csv.DictReader((tmp_path / "universe-unresolved.csv").open()))
    assert rows[0]["symbol"] == "CWEN-A"
    assert rows[0]["reason"]


def test_the_main_file_never_contains_a_half_populated_row(tmp_path: Path, sources):
    refresh(tmp_path, client=FakeClient({"AAPL": PROFILE}))
    for row in csv.DictReader((tmp_path / "universe.csv").open()):
        assert all(row[field] for field in ("symbol", "mic", "currency", "yf_sector"))


def test_a_missing_cik_is_empty_rather_than_fatal(tmp_path: Path, sources, monkeypatch):
    monkeypatch.setattr("screener.universe.refresh.cik_by_symbol", lambda **_: {})
    refresh(tmp_path, client=FakeClient({"AAPL": PROFILE}))
    rows = list(csv.DictReader((tmp_path / "universe.csv").open()))
    assert rows[0]["cik"] == ""


def test_refresh_never_touches_the_database(tmp_path: Path, sources, monkeypatch):
    def explode():
        raise AssertionError("refresh must not open a database connection")

    monkeypatch.setattr("screener.config.settings", explode)
    refresh(tmp_path, client=FakeClient({"AAPL": PROFILE}))


def test_the_delay_is_applied_between_symbols(tmp_path: Path, sources):
    slept: list[float] = []
    refresh(tmp_path, client=FakeClient({"AAPL": PROFILE}), delay=0.25, sleep=slept.append)
    assert slept == [0.25, 0.25]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_universe_refresh.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'screener.universe.refresh'`

- [ ] **Step 3: Write `src/screener/universe/refresh.py`**

```python
"""Build the committed CSV from Wikipedia, SEC and Yahoo.

Never opens a database connection. Everything fragile or slow lives here, in a
command run four times a year while someone is watching, rather than on the
nightly path.
"""

import csv
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from screener.universe.rows import UniverseRow, write_rows
from screener.universe.sources.sec import cik_by_symbol
from screener.universe.sources.wikipedia import constituents
from screener.universe.sources.yahoo import YahooClient

logger = logging.getLogger(__name__)

UNRESOLVED_FIELDNAMES: tuple[str, ...] = ("symbol", "name", "index_name", "reason")


@dataclass(frozen=True)
class RefreshReport:
    written: int
    unresolved: int


def refresh(
    out_dir: Path,
    *,
    transport: httpx.BaseTransport | None = None,
    delay: float = 0.0,
    client=None,
    sleep: Callable[[float], None] | None = None,
) -> RefreshReport:
    """Write `universe.csv` and `universe-unresolved.csv` into `out_dir`.

    A symbol Yahoo will not classify goes to the unresolved file rather than
    into the main one half-populated: the loader then needs no "is this row
    usable" branch, and the failures stay visible instead of polluting a diff.
    """
    members = constituents(transport=transport)
    ciks = cik_by_symbol(transport=transport)
    yahoo = client or YahooClient(transport=transport)
    pause = sleep or time.sleep

    rows: list[UniverseRow] = []
    unresolved: list[dict[str, str]] = []

    for member in members:
        profile = yahoo.profile(member.symbol)
        if delay:
            pause(delay)
        if profile is None:
            unresolved.append(
                {
                    "symbol": member.symbol,
                    "name": member.name,
                    "index_name": member.index_name,
                    "reason": "yahoo returned no usable profile",
                }
            )
            continue
        rows.append(
            UniverseRow(
                symbol=member.symbol,
                name=member.name,
                index_name=member.index_name,
                mic=profile.mic,
                currency=profile.currency,
                cik=ciks.get(member.symbol, ""),
                yf_sector=profile.sector,
                yf_industry=profile.industry,
                gics_sector=member.gics_sector,
            )
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    written = write_rows(out_dir / "universe.csv", rows)

    with (out_dir / "universe-unresolved.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(UNRESOLVED_FIELDNAMES))
        writer.writeheader()
        for row in sorted(unresolved, key=lambda r: r["symbol"]):
            writer.writerow(row)

    logger.info("universe: %d written, %d unresolved", written, len(unresolved))
    return RefreshReport(written=written, unresolved=len(unresolved))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_universe_refresh.py -v && pyright`
Expected: 6 passed, pyright 0 errors

- [ ] **Step 5: Commit**

```bash
git add src/screener/universe/refresh.py tests/test_universe_refresh.py
git commit -m "feat: refresh the universe CSV from its three sources"
```

---

### Task 6: The reconciler, as a pure function

**Files:**
- Create: `src/screener/universe/reconcile.py`
- Test: `tests/test_universe_reconcile.py`

**Interfaces:**
- Consumes: `UniverseRow` from Task 1.
- Produces:
  - `ExistingSecurity` frozen dataclass: `id: int`, `cik: str`, `symbol: str`, `industry_code: str`, `is_active: bool`.
  - `Plan` frozen dataclass with tuples `new: tuple[UniverseRow, ...]`,
    `departed: tuple[ExistingSecurity, ...]`,
    `reclassified: tuple[tuple[ExistingSecurity, UniverseRow], ...]`,
    `renamed: tuple[tuple[ExistingSecurity, UniverseRow], ...]`,
    `reentered: tuple[tuple[ExistingSecurity, UniverseRow], ...]`,
    `unchanged: int`, and a property `departure_share: float`.
  - `AmbiguousIdentity(RuntimeError)`.
  - `plan(rows: Sequence[UniverseRow], existing: Sequence[ExistingSecurity]) -> Plan`.
  - `DEPARTURE_CEILING: float = 0.10`.

Pure: no database, no clock, no filesystem. This is where every transition rule lives, which is
why it can be tested exhaustively without either.

- [ ] **Step 1: Write the failing test**

Create `tests/test_universe_reconcile.py`:

```python
import pytest

from screener.universe.reconcile import (
    AmbiguousIdentity,
    ExistingSecurity,
    plan,
)
from screener.universe.rows import UniverseRow


def csv_row(symbol: str, *, cik: str = "", industry: str = "Consumer Electronics") -> UniverseRow:
    return UniverseRow(
        symbol=symbol, name=f"{symbol} Inc", index_name="sp500", mic="XNAS",
        currency="USD", cik=cik, yf_sector="Technology", yf_industry=industry,
        gics_sector="Information Technology",
    )


def existing(sec_id: int, symbol: str, *, cik: str = "", industry: str = "consumer-electronics",
             active: bool = True) -> ExistingSecurity:
    return ExistingSecurity(id=sec_id, cik=cik, symbol=symbol, industry_code=industry, is_active=active)


def test_a_symbol_not_in_the_database_is_new():
    result = plan([csv_row("AAPL")], [])
    assert [r.symbol for r in result.new] == ["AAPL"]


def test_an_active_security_absent_from_the_csv_has_departed():
    result = plan([], [existing(1, "AAPL")])
    assert [e.id for e in result.departed] == [1]


def test_an_unchanged_security_is_counted_not_acted_on():
    result = plan([csv_row("AAPL")], [existing(1, "AAPL")])
    assert result.unchanged == 1
    assert not (result.new or result.departed or result.reclassified or result.renamed)


def test_a_changed_industry_is_a_reclassification():
    result = plan([csv_row("AAPL", industry="Software - Infrastructure")], [existing(1, "AAPL")])
    assert [e.id for e, _ in result.reclassified] == [1]


def test_a_rename_is_matched_by_cik_not_symbol():
    """The decision the reconciler hangs on. Matching by symbol would report a
    departure plus an unrelated arrival and orphan the company's history."""
    result = plan(
        [csv_row("META", cik="0001326801")],
        [existing(1, "FB", cik="0001326801")],
    )
    assert [e.id for e, _ in result.renamed] == [1]
    assert not result.new
    assert not result.departed


def test_a_rename_without_a_cik_cannot_be_detected_and_reads_as_churn():
    """Recorded so the limitation is explicit: no CIK, no rename detection."""
    result = plan([csv_row("META")], [existing(1, "FB")])
    assert [r.symbol for r in result.new] == ["META"]
    assert [e.id for e in result.departed] == [1]


def test_an_inactive_security_present_again_has_re_entered():
    result = plan([csv_row("AAPL")], [existing(1, "AAPL", active=False)])
    assert [e.id for e, _ in result.reentered] == [1]
    assert not result.new


def test_an_inactive_security_still_absent_is_not_a_departure():
    result = plan([], [existing(1, "AAPL", active=False)])
    assert not result.departed


def test_two_csv_rows_resolving_to_one_security_is_an_error():
    with pytest.raises(AmbiguousIdentity, match="0000320193"):
        plan(
            [csv_row("AAPL", cik="0000320193"), csv_row("AAPL2", cik="0000320193")],
            [existing(1, "AAPL", cik="0000320193")],
        )


def test_departure_share_is_measured_against_the_active_universe():
    result = plan([csv_row("A")], [existing(1, "A"), existing(2, "B"), existing(3, "C"), existing(4, "D")])
    assert result.departure_share == pytest.approx(0.75)


def test_departure_share_is_zero_when_the_database_is_empty():
    assert plan([csv_row("A")], []).departure_share == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_universe_reconcile.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'screener.universe.reconcile'`

- [ ] **Step 3: Write `src/screener/universe/reconcile.py`**

```python
"""Work out what changed, without touching anything.

Pure by design: no database, no clock, no filesystem. Every transition rule
lives here, so all of them can be tested exhaustively and cheaply, and `load`
is left with only the job of applying a decision someone else made.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from screener.universe.rows import UniverseRow, slugify

DEPARTURE_CEILING: float = 0.10


class AmbiguousIdentity(RuntimeError):
    """Two CSV rows resolved to one security. Never merged silently."""


@dataclass(frozen=True)
class ExistingSecurity:
    id: int
    cik: str
    symbol: str
    industry_code: str
    is_active: bool


@dataclass(frozen=True)
class Plan:
    new: tuple[UniverseRow, ...] = ()
    departed: tuple[ExistingSecurity, ...] = ()
    reclassified: tuple[tuple[ExistingSecurity, UniverseRow], ...] = ()
    renamed: tuple[tuple[ExistingSecurity, UniverseRow], ...] = ()
    reentered: tuple[tuple[ExistingSecurity, UniverseRow], ...] = ()
    unchanged: int = 0
    active_before: int = field(default=0)

    @property
    def departure_share(self) -> float:
        if not self.active_before:
            return 0.0
        return len(self.departed) / self.active_before

    def is_empty(self) -> bool:
        return not (self.new or self.departed or self.reclassified or self.renamed or self.reentered)

    def summary(self) -> str:
        return (
            f"{len(self.new)} new, {len(self.departed)} retired, "
            f"{len(self.reclassified)} reclassified, {len(self.renamed)} renamed, "
            f"{len(self.reentered)} re-entered, {self.unchanged} unchanged"
        )


def plan(rows: Sequence[UniverseRow], existing: Sequence[ExistingSecurity]) -> Plan:
    """Compare a CSV against current state and describe the difference.

    Resolution order is CIK, then current symbol, then treat as new. CIK first
    because a symbol is a mutable attribute: matching on it turns a rename into
    a departure plus an arrival and orphans the company's history.
    """
    by_cik = {e.cik: e for e in existing if e.cik}
    by_symbol = {e.symbol: e for e in existing}

    new: list[UniverseRow] = []
    reclassified: list[tuple[ExistingSecurity, UniverseRow]] = []
    renamed: list[tuple[ExistingSecurity, UniverseRow]] = []
    reentered: list[tuple[ExistingSecurity, UniverseRow]] = []
    unchanged = 0
    seen: dict[int, str] = {}

    for row in rows:
        match = by_cik.get(row.cik) if row.cik else None
        if match is None:
            match = by_symbol.get(row.symbol)
        if match is None:
            new.append(row)
            continue
        if match.id in seen:
            raise AmbiguousIdentity(
                f"rows {seen[match.id]!r} and {row.symbol!r} both resolve to "
                f"security {match.id} (cik {match.cik or 'none'})"
            )
        seen[match.id] = row.symbol

        acted = False
        if not match.is_active:
            reentered.append((match, row))
            acted = True
        if match.symbol != row.symbol:
            renamed.append((match, row))
            acted = True
        if match.industry_code != slugify(row.yf_industry or row.yf_sector):
            reclassified.append((match, row))
            acted = True
        if not acted:
            unchanged += 1

    departed = tuple(e for e in existing if e.is_active and e.id not in seen)
    return Plan(
        new=tuple(new),
        departed=departed,
        reclassified=tuple(reclassified),
        renamed=tuple(renamed),
        reentered=tuple(reentered),
        unchanged=unchanged,
        active_before=sum(1 for e in existing if e.is_active),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_universe_reconcile.py -v && pyright`
Expected: 11 passed, pyright 0 errors

- [ ] **Step 5: Commit**

```bash
git add src/screener/universe/reconcile.py tests/test_universe_reconcile.py
git commit -m "feat: pure universe reconciler"
```

---

### Task 7: Apply the plan to Postgres

**Files:**
- Create: `src/screener/universe/load.py`
- Test: `tests/test_universe_load.py`

**Interfaces:**
- Consumes: `plan()`, `Plan`, `ExistingSecurity`, `DEPARTURE_CEILING`, `read_rows`, `slugify`, `UniverseRow`.
- Produces:
  - `ensure_taxonomy(conn, rows) -> None` — upserts `sector_scheme`, both `sector_node` levels, and `peer_group` at levels 0 and 1.
  - `current_state(conn) -> list[ExistingSecurity]`.
  - `apply(conn, rows, *, as_of: date, force: bool = False) -> Plan` — one transaction, raises `DepartureCeilingExceeded` unless `force`.
  - `DepartureCeilingExceeded(RuntimeError)`.
  - `load(path: Path, *, as_of: date, dry_run: bool = False, force: bool = False) -> Plan`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_universe_load.py`:

```python
from datetime import date

import pytest

from screener.universe.load import (
    DepartureCeilingExceeded,
    apply,
    current_state,
    ensure_taxonomy,
)
from screener.universe.rows import UniverseRow

AS_OF = date(2026, 9, 5)
LATER = date(2026, 12, 5)


def row(symbol: str, *, cik: str = "", industry: str = "Consumer Electronics",
        sector: str = "Technology", name: str | None = None) -> UniverseRow:
    return UniverseRow(
        symbol=symbol, name=name or f"{symbol} Inc", index_name="sp500", mic="XNAS",
        currency="USD", cik=cik, yf_sector=sector, yf_industry=industry,
        gics_sector="Information Technology",
    )


def active_symbols(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("select primary_symbol from security where is_active")
        return {r[0] for r in cur.fetchall()}


def test_a_first_load_inserts_securities(fresh_db):
    apply(fresh_db, [row("AAPL"), row("MSFT")], as_of=AS_OF)
    assert active_symbols(fresh_db) == {"AAPL", "MSFT"}


def test_loading_the_same_csv_twice_changes_nothing(fresh_db):
    apply(fresh_db, [row("AAPL")], as_of=AS_OF)
    second = apply(fresh_db, [row("AAPL")], as_of=LATER)
    assert second.is_empty()
    assert second.unchanged == 1


def test_a_departure_retires_rather_than_deletes(fresh_db):
    apply(fresh_db, [row("AAPL"), row("MSFT")], as_of=AS_OF)
    apply(fresh_db, [row("AAPL")], as_of=LATER)
    with fresh_db.cursor() as cur:
        cur.execute("select is_active, last_seen from security where primary_symbol = 'MSFT'")
        is_active, last_seen = cur.fetchone()
    assert is_active is False and last_seen == LATER


def test_a_departure_leaves_the_temporal_rows_open(fresh_db):
    """It left the universe, not existence: we do not know its symbol or sector changed."""
    apply(fresh_db, [row("MSFT")], as_of=AS_OF)
    apply(fresh_db, [], as_of=LATER)
    with fresh_db.cursor() as cur:
        cur.execute(
            "select ss.valid_to from security_symbol ss join security s on s.id = ss.security_id"
            " where s.primary_symbol = 'MSFT'"
        )
        assert cur.fetchone()[0] is None


def test_a_rename_keeps_the_same_security_and_closes_the_old_symbol(fresh_db):
    apply(fresh_db, [row("FB", cik="0001326801", name="Meta")], as_of=AS_OF)
    with fresh_db.cursor() as cur:
        cur.execute("select id from security where primary_symbol = 'FB'")
        before = cur.fetchone()[0]

    apply(fresh_db, [row("META", cik="0001326801", name="Meta")], as_of=LATER)

    with fresh_db.cursor() as cur:
        cur.execute("select id from security where primary_symbol = 'META'")
        assert cur.fetchone()[0] == before
        cur.execute(
            "select symbol, valid_from, valid_to from security_symbol"
            " where security_id = %s order by valid_from",
            (before,),
        )
        history = cur.fetchall()
    assert [h[0] for h in history] == ["FB", "META"]
    assert history[0][2] == LATER and history[1][2] is None


def test_a_reclassification_closes_the_old_row_adjacently(fresh_db):
    apply(fresh_db, [row("AAPL", industry="Consumer Electronics")], as_of=AS_OF)
    apply(fresh_db, [row("AAPL", industry="Software - Infrastructure")], as_of=LATER)
    with fresh_db.cursor() as cur:
        cur.execute(
            "select valid_from, valid_to from security_sector ss"
            " join security s on s.id = ss.security_id"
            " where s.primary_symbol = 'AAPL' order by valid_from"
        )
        rows = cur.fetchall()
    assert len(rows) == 2
    assert rows[0][1] == LATER == rows[1][0]
    assert rows[1][1] is None


def test_re_entry_reactivates_the_original_security(fresh_db):
    apply(fresh_db, [row("AAPL")], as_of=AS_OF)
    with fresh_db.cursor() as cur:
        cur.execute("select id from security where primary_symbol = 'AAPL'")
        original = cur.fetchone()[0]
    apply(fresh_db, [], as_of=LATER)
    apply(fresh_db, [row("AAPL")], as_of=date(2027, 3, 1))
    with fresh_db.cursor() as cur:
        cur.execute("select id, is_active, last_seen from security where primary_symbol = 'AAPL'")
        got = cur.fetchone()
    assert got == (original, True, None)


def test_the_departure_ceiling_refuses_a_truncated_csv(fresh_db):
    apply(fresh_db, [row(s) for s in ("A", "B", "C", "D", "E", "F", "G", "H", "I", "J")], as_of=AS_OF)
    with fresh_db.cursor() as cur:
        cur.execute("select count(*) from sector_node")
        nodes_before = cur.fetchone()[0]

    with pytest.raises(DepartureCeilingExceeded):
        apply(fresh_db, [row("A", industry="Something Entirely New")], as_of=LATER)

    assert len(active_symbols(fresh_db)) == 10
    with fresh_db.cursor() as cur:
        cur.execute("select count(*) from sector_node")
        # The refusal rolls back the taxonomy upsert too: a run that reports
        # doing nothing must not have left a new sector node behind.
        assert cur.fetchone()[0] == nodes_before


def test_force_overrides_the_ceiling(fresh_db):
    apply(fresh_db, [row(s) for s in ("A", "B", "C", "D", "E", "F", "G", "H", "I", "J")], as_of=AS_OF)
    apply(fresh_db, [row("A")], as_of=LATER, force=True)
    assert active_symbols(fresh_db) == {"A"}


def test_peer_groups_exist_for_market_and_every_sector(fresh_db):
    ensure_taxonomy(fresh_db, [row("AAPL"), row("XOM", sector="Energy", industry="Oil & Gas Integrated")])
    with fresh_db.cursor() as cur:
        cur.execute("select level, count(*) from peer_group group by level order by level")
        assert cur.fetchall() == [(0, 1), (1, 2)]


def test_industries_are_stored_as_level_two_nodes_under_their_sector(fresh_db):
    ensure_taxonomy(fresh_db, [row("AAPL")])
    with fresh_db.cursor() as cur:
        cur.execute(
            "select child.level, parent.code from sector_node child"
            " join sector_node parent on parent.id = child.parent_id"
            " where child.code = 'consumer-electronics'"
        )
        assert cur.fetchone() == (2, "technology")


def test_current_state_reports_what_the_reconciler_needs(fresh_db):
    apply(fresh_db, [row("AAPL", cik="0000320193")], as_of=AS_OF)
    state = current_state(fresh_db)
    assert len(state) == 1
    assert (state[0].symbol, state[0].cik, state[0].is_active) == ("AAPL", "0000320193", True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_universe_load.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'screener.universe.load'`

- [ ] **Step 3: Write `src/screener/universe/load.py`**

```python
"""Apply a reconciliation plan to Postgres.

The only writer, and it never opens a socket. Everything runs in one
transaction: a half-applied universe would leave the next scoring run computing
percentiles against a partly built peer group, which is silent corruption rather
than a visible failure.
"""

import logging
from datetime import date
from pathlib import Path

import psycopg

from screener.config import settings
from screener.universe.reconcile import (
    DEPARTURE_CEILING,
    ExistingSecurity,
    Plan,
    plan,
)
from screener.universe.rows import UniverseRow, read_rows, slugify

logger = logging.getLogger(__name__)

SCHEME = "yfinance"
MARKET_GROUP = "market"


class DepartureCeilingExceeded(RuntimeError):
    """The CSV would retire more of the universe than a real refresh ever does."""


def ensure_taxonomy(conn: psycopg.Connection, rows: list[UniverseRow]) -> None:
    """Upsert the scheme, both node levels, and peer groups for levels 0 and 1.

    `sector_node` gets industries as well as sectors because a security points at
    the most specific classification available. `peer_group` gets only what v1
    scores — industry groups would be rows nothing references.
    """
    with conn.cursor() as cur:
        cur.execute(
            "insert into sector_scheme (code, name) values (%s, %s)"
            " on conflict (code) do update set name = excluded.name returning id",
            (SCHEME, "yfinance"),
        )
        scheme_id = cur.fetchone()[0]

        sectors = {r.yf_sector for r in rows if r.yf_sector}
        for sector in sorted(sectors):
            cur.execute(
                "insert into sector_node (scheme_id, level, code, name) values (%s, 1, %s, %s)"
                " on conflict (scheme_id, code) do update set name = excluded.name",
                (scheme_id, slugify(sector), sector),
            )
        for row in rows:
            if not (row.yf_industry and row.yf_sector):
                continue
            cur.execute(
                "insert into sector_node (scheme_id, parent_id, level, code, name)"
                " select %s, parent.id, 2, %s, %s from sector_node parent"
                " where parent.scheme_id = %s and parent.code = %s"
                " on conflict (scheme_id, code) do update set name = excluded.name",
                (scheme_id, slugify(row.yf_industry), row.yf_industry, scheme_id, slugify(row.yf_sector)),
            )

        cur.execute(
            "insert into peer_group (scheme_id, sector_node_id, level, code)"
            " values (%s, null, 0, %s) on conflict (scheme_id, code) do nothing",
            (scheme_id, MARKET_GROUP),
        )
        cur.execute(
            "insert into peer_group (scheme_id, sector_node_id, level, code)"
            " select %s, id, 1, code from sector_node"
            " where scheme_id = %s and level = 1"
            " on conflict (scheme_id, code) do nothing",
            (scheme_id, scheme_id),
        )


def current_state(conn: psycopg.Connection) -> list[ExistingSecurity]:
    """Every security, with the fields the reconciler matches and compares on."""
    with conn.cursor() as cur:
        cur.execute(
            "select s.id, coalesce(s.cik, ''), s.primary_symbol,"
            "       coalesce(n.code, ''), s.is_active"
            "  from security s"
            "  left join security_sector ss"
            "    on ss.security_id = s.id and ss.valid_to is null"
            "  left join sector_node n on n.id = ss.sector_node_id"
        )
        return [
            ExistingSecurity(id=r[0], cik=r[1], symbol=r[2], industry_code=r[3], is_active=r[4])
            for r in cur.fetchall()
        ]


def _node_id(cur: psycopg.Cursor, code: str) -> int | None:
    cur.execute(
        "select n.id from sector_node n join sector_scheme s on s.id = n.scheme_id"
        " where s.code = %s and n.code = %s",
        (SCHEME, code),
    )
    got = cur.fetchone()
    return got[0] if got else None


def _classification(row: UniverseRow) -> str:
    return slugify(row.yf_industry or row.yf_sector)


def apply(
    conn: psycopg.Connection,
    rows: list[UniverseRow],
    *,
    as_of: date,
    force: bool = False,
) -> Plan:
    """Reconcile `rows` into the database. All of it, or none of it.

    Everything is inside one transaction, the taxonomy upsert and the ceiling
    check included. On an autocommit connection an `ensure_taxonomy` outside it
    would commit new sector nodes even when the load was then refused — leaving
    the database changed by a run that reported doing nothing.
    """
    with conn.transaction(), conn.cursor() as cur:
        ensure_taxonomy(conn, rows)
        decided = plan(rows, current_state(conn))

        if not force and decided.departure_share > DEPARTURE_CEILING:
            raise DepartureCeilingExceeded(
                f"{len(decided.departed)} of {decided.active_before} active securities would be "
                f"retired ({decided.departure_share:.0%}, ceiling {DEPARTURE_CEILING:.0%}). "
                "Pass --force if this is genuinely intended."
            )

        for row in decided.new:
            cur.execute(
                "insert into security (name, mic, currency, country, cik, primary_symbol,"
                " is_active, first_seen) values (%s, %s, %s, 'US', %s, %s, true, %s) returning id",
                (row.name, row.mic, row.currency, row.cik or None, row.symbol, as_of),
            )
            security_id = cur.fetchone()[0]
            cur.execute(
                "insert into security_symbol (security_id, symbol, mic, valid_from, source)"
                " values (%s, %s, %s, %s, %s)",
                (security_id, row.symbol, row.mic, as_of, SCHEME),
            )
            node = _node_id(cur, _classification(row))
            if node is not None:
                cur.execute(
                    "insert into security_sector (security_id, sector_node_id, valid_from, source)"
                    " values (%s, %s, %s, %s)",
                    (security_id, node, as_of, SCHEME),
                )

        for gone in decided.departed:
            cur.execute(
                "update security set is_active = false, last_seen = %s where id = %s",
                (as_of, gone.id),
            )

        for back, _row in decided.reentered:
            cur.execute(
                "update security set is_active = true, last_seen = null where id = %s",
                (back.id,),
            )

        for old, row in decided.renamed:
            cur.execute(
                "update security_symbol set valid_to = %s"
                " where security_id = %s and valid_to is null",
                (as_of, old.id),
            )
            cur.execute(
                "insert into security_symbol (security_id, symbol, mic, valid_from, source)"
                " values (%s, %s, %s, %s, %s)",
                (old.id, row.symbol, row.mic, as_of, SCHEME),
            )
            cur.execute(
                "update security set primary_symbol = %s, name = %s where id = %s",
                (row.symbol, row.name, old.id),
            )

        for old, row in decided.reclassified:
            node = _node_id(cur, _classification(row))
            if node is None:
                continue
            cur.execute(
                "update security_sector set valid_to = %s"
                " where security_id = %s and valid_to is null",
                (as_of, old.id),
            )
            cur.execute(
                "insert into security_sector (security_id, sector_node_id, valid_from, source)"
                " values (%s, %s, %s, %s)",
                (old.id, node, as_of, SCHEME),
            )

    logger.info("universe load: %s", decided.summary())
    return decided


def load(path: Path, *, as_of: date, dry_run: bool = False, force: bool = False) -> Plan:
    """Read the CSV and apply it, or describe what applying it would do."""
    rows = read_rows(path)
    with psycopg.connect(settings().database_url, autocommit=True) as conn:
        if dry_run:
            ensure_taxonomy(conn, rows)
            decided = plan(rows, current_state(conn))
            logger.info("dry run: %s", decided.summary())
            return decided
        return apply(conn, rows, as_of=as_of, force=force)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_universe_load.py -v && pyright`
Expected: 12 passed, pyright 0 errors

- [ ] **Step 5: Commit**

```bash
git add src/screener/universe/load.py tests/test_universe_load.py
git commit -m "feat: apply the universe plan in one transaction"
```

---

### Task 8: Wire the CLI and document the commands

**Files:**
- Modify: `src/screener/universe/cli.py` (replace the `NotImplementedError` from Task 1)
- Modify: `CLAUDE.md`, `README.md`
- Test: `tests/test_universe_cli.py`

**Interfaces:**
- Consumes: `refresh()` from Task 5, `load()` from Task 7.
- Produces: `main(argv)` returning `0` on success and `1` on a refused load.

- [ ] **Step 1: Write the failing test**

Create `tests/test_universe_cli.py`:

```python
from datetime import date

import pytest

from screener.universe.cli import main
from screener.universe.reconcile import Plan


def test_refresh_writes_into_the_data_directory(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(
        "screener.universe.cli.refresh",
        lambda out_dir, **kw: seen.update(out_dir=out_dir) or _report(),
    )
    assert main(["refresh", "--data-dir", str(tmp_path)]) == 0
    assert seen["out_dir"] == tmp_path


def _report():
    from screener.universe.refresh import RefreshReport

    return RefreshReport(written=2, unresolved=0)


def test_load_passes_dry_run_and_force_through(monkeypatch, tmp_path):
    seen = {}

    def fake_load(path, *, as_of, dry_run, force):
        seen.update(path=path, as_of=as_of, dry_run=dry_run, force=force)
        return Plan()

    monkeypatch.setattr("screener.universe.cli.load", fake_load)
    (tmp_path / "universe.csv").write_text("")
    assert main(["load", "--data-dir", str(tmp_path), "--dry-run", "--force"]) == 0
    assert seen["dry_run"] is True and seen["force"] is True


def test_as_of_defaults_to_today(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(
        "screener.universe.cli.load",
        lambda path, **kw: seen.update(kw) or Plan(),
    )
    (tmp_path / "universe.csv").write_text("")
    main(["load", "--data-dir", str(tmp_path)])
    assert seen["as_of"] == date.today()


def test_a_refused_load_exits_non_zero(monkeypatch, tmp_path):
    from screener.universe.load import DepartureCeilingExceeded

    def refuse(path, **kw):
        raise DepartureCeilingExceeded("too many departures")

    monkeypatch.setattr("screener.universe.cli.load", refuse)
    (tmp_path / "universe.csv").write_text("")
    assert main(["load", "--data-dir", str(tmp_path)]) == 1


def test_an_unknown_command_is_rejected_by_argparse(tmp_path):
    with pytest.raises(SystemExit):
        main(["demolish"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_universe_cli.py -v`
Expected: FAIL — `main` raises `NotImplementedError`

- [ ] **Step 3: Replace `src/screener/universe/cli.py`**

```python
"""Entry point, shaped like `screener.boot` so the two read the same.

`refresh` talks to the network and never opens a database connection. `load`
talks to the database and never opens a socket. Keeping that split visible at
the command line is the point: the fragile, slow half runs quarterly by hand,
and its output is a committed file you can review before it moves a score.
"""

import argparse
import logging
from datetime import date
from pathlib import Path

from screener.universe.load import DepartureCeilingExceeded, load
from screener.universe.refresh import refresh

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path("data")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="screener.universe", description=__doc__)
    parser.add_argument(
        "command",
        choices=("refresh", "load"),
        help=(
            "refresh: rebuild the universe CSV from Wikipedia, SEC and Yahoo "
            "(no database). load: reconcile that CSV into Postgres (no network)."
        ),
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                        help="where universe.csv lives (default: data/)")
    parser.add_argument("--dry-run", action="store_true",
                        help="load: print the plan and change nothing")
    parser.add_argument("--force", action="store_true",
                        help="load: proceed past the departure ceiling")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None,
                        help="load: date to stamp changes with (default: today)")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="refresh: seconds to pause between symbols")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.command == "refresh":
        report = refresh(args.data_dir, delay=args.delay)
        logger.info("refresh: %d written, %d unresolved", report.written, report.unresolved)
        return 0

    try:
        result = load(
            args.data_dir / "universe.csv",
            as_of=args.as_of or date.today(),
            dry_run=args.dry_run,
            force=args.force,
        )
    except DepartureCeilingExceeded as exc:
        logger.error("%s", exc)
        return 1
    logger.info("load: %s", result.summary())
    return 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_universe_cli.py -v && pytest -q && pyright`
Expected: 5 passed in the CLI file; whole suite green; pyright 0 errors

- [ ] **Step 5: Document the commands**

In `CLAUDE.md`, under `## Commands`, add:

```markdown
- Refresh the universe CSV (quarterly, manual, no database):
  `python -m screener.universe refresh`
- Load it (no network): `python -m screener.universe load --dry-run` then without `--dry-run`.
  Refuses if more than 10% of the active universe would be retired; `--force` overrides.
```

In `README.md`, under `## Commands`, add the same two rows to the table:

```markdown
| Refresh universe CSV | `python -m screener.universe refresh` |
| Load universe | `python -m screener.universe load --dry-run` |
```

- [ ] **Step 6: Commit**

```bash
git add src/screener/universe/cli.py tests/test_universe_cli.py CLAUDE.md README.md
git commit -m "feat: universe CLI"
```

---

## Verification

The suite passing means these spec claims are demonstrated rather than asserted:

| Claim | Test |
|---|---|
| A rename keeps the company's history | `test_a_rename_keeps_the_same_security_and_closes_the_old_symbol` |
| CIK, not symbol, is what makes that work | `test_a_rename_is_matched_by_cik_not_symbol` |
| ...and its absence is a known limitation | `test_a_rename_without_a_cik_cannot_be_detected_and_reads_as_churn` |
| Departures retire rather than delete | `test_a_departure_retires_rather_than_deletes` |
| A departure does not close temporal rows | `test_a_departure_leaves_the_temporal_rows_open` |
| Reclassification closes adjacently | `test_a_reclassification_closes_the_old_row_adjacently` |
| Re-loading the same CSV is a no-op | `test_loading_the_same_csv_twice_changes_nothing` |
| A truncated CSV cannot wipe the universe | `test_the_departure_ceiling_refuses_a_truncated_csv` |
| Crumb expiry is handled, not an edge case | `test_a_401_refreshes_the_crumb_and_retries_with_the_new_one` |
| The crumb is not refetched per symbol | `test_the_crumb_is_fetched_once_and_reused_across_symbols` |
| SEC needs a declared User-Agent | `test_a_user_agent_is_declared_because_sec_returns_403_without_one` |
| Both Yahoo modules are requested | `test_the_request_asks_for_both_modules_because_assetprofile_lacks_exchange` |
| Refresh never touches the database | `test_refresh_never_touches_the_database` |

## Out of scope

Prices, fundamentals and every other fact — that is ingest, and needs its own spec. No
`fundamental_fact`, no `price_daily`, no scoring, no `scoring_run`.

The first real `refresh` run is deliberately not part of this plan: it produces a data file whose
contents are a judgement call, and committing ~1,500 rows should be its own reviewable change.
