# UI Swap Piece (b): The Read Path — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve the real scored screen and one security's traceable detail from two session-guarded endpoints, `GET /api/screen` and `GET /api/screen/security`, over a new `screener.screen` package, with nothing yet calling them.

**Architecture:** `screener.screen` splits reading from shaping, as `screener.ingest` splits `parse` from `load`. `rows`, `params` and `shape` are pure. `queries` holds every statement as a literal. `read` resolves the night and runs the reads, and `explain` re-runs scoring's explaining forms (piece (a)) for one security under the run's own view, giving each metric one of six statuses. `screener.health.server` gains two thin handlers that turn the package's exceptions into status codes. Scoring, the schema and the web app are untouched.

**Tech Stack:** Python 3.11+, `psycopg` 3, Postgres 16, `pytest`, `pyright`. No new dependencies.

**Spec:** `docs/specs/2026-09-13-ui-swap.md`. This plan implements D6–D11, D13, D14, §6, §7, §8 piece (b) and §12 step 2, plus the `screener.screen` erratum of §11.

## Global Constraints

- **Nothing scoring computes or writes changes, and there are no schema changes.** No file under `src/screener/scoring/` or `migrations/` is edited, and `tests/golden/scoring_output.json` is unchanged.
- **Behind the session unconditionally:** both handlers start with `login = self._require_login(config)`.
- **SQL is literal, with bound parameters only.** A statement may be a literal joined to another literal (the result stays a `LiteralString`), but never an f-string, `%` formatting or `.format`, in `src/` or `tests/`. `sort` selects one of five fixed `order by` literals.
- **Money and ratios travel as decimal strings, never floats** (D5). Scores and percentiles have one decimal, rounded half up. Raw and reproduced values are exact, fixed-point. Closes have two decimals below 1,000 and none at or above.
- **Display closes ignore the cutoff; reproduction does not** (D11, D13).
- **A missing value is absent, never imputed and never zero.**
- **Pure modules open no connection:** `rows.py`, `params.py`, `shape.py`, and `explain.check` / `explain.reproduce_values`.
- **Each package has a small public surface through `__init__.py`.** Tests import from `screener.screen`, never a submodule; a `monkeypatch.setattr("screener.screen.explain.read_facts", …)` string target is not an import and is allowed.
- **`__all__` ordering follows ruff's RUF022:** SCREAMING_CASE, then CamelCase, then snake_case, alphabetical within each group.
- **Open parameters (spec §10):** page size 50, maximum 100; 30 closes for a sparkline, 60 for a chart.
- **Comments explain why, not what.** `pyright` must report zero errors, and it checks `tests/`.

## Amendments to the spec

- **P1: two more modules.** D6 names four modules. This plan adds two:
  - `params.py` parses the query string. It is pure and tested without a server.
  - `read.py` holds the reads on a connection and the refusals they raise.

  That keeps `shape.py` pure and each handler a translation from exception to status code. `explain.py` still owns reproduction.
- **P2: `sector` is checked for shape, not existence.** Whether a code exists is a question about the run's rows, so a well-formed unknown code returns an empty page with `total: 0`. `offset` is capped at 1,000,000 so an enormous number cannot reach Postgres as an out-of-range integer and come back as a 503.
- **P3: extra response keys and exact error bodies.**
  - The unscored D10 form also carries `run_id` and `latest`, since D8 says every response carries the run.
  - The scored D10 form also carries `active` and `agreement`.
  - The error bodies are:

    | status | body |
    |---|---|
    | 400 | `{"error", "parameter"}` |
    | 404 | `{"error": "unknown_symbol", "symbol"}` |
    | 409 | `{"error": "run_changed", "run"}` or `{"error": "ambiguous_symbol", "symbol", "exchanges"}` |
    | 503 | `{"error": "cannot read the screen", "database": <exception type name>}` |
- **P4: a stored metric that no longer applies is still listed.** D10 lists the applicable metrics. Suppose a security's industry changed class after the run (F15), so a metric the run stored no longer applies. That metric is listed too, as `mismatch` with the reason "no longer applicable to this security's industry", rather than dropped from the panel.
- **P5: reproduction repeats `score()`'s per-security assembly.** §12 keeps scoring untouched in this piece, so `explain.reproduce_values` repeats the dozen lines of `run.score` that feed one security into the explaining forms. What catches drift is `test_every_stored_metric_reproduces`, which runs real scoring and requires two things: every stored value reproduces exactly, and no value appears that was not stored.
- **P6: named parameters.** Statements use `%(name)s`. When Steven starts reading through these statements, piece (c) widens `playground.select`'s `params` to accept a mapping.

## Environment

Docker Desktop must be running.

```bash
cd /home/daniel/projects/stock-aggregator
docker compose -f compose.yaml up -d          # project stock-aggregator-test, port 5432
export DATABASE_URL_TEST="postgresql://postgres:screener@localhost:5432/screener_test"
```

**Export the variable.** An unset one silently skips every database test. Use `.venv/bin/python -m pytest <file> -n0` for one file. The full suite has 5 known failures in `tests/test_magpie_reachable.py` and `tests/test_magpie_acquire.py` in sandboxes without DNS; any other failure is real.

Work on branch `ui-swap-read-path`, which already exists off `main`.

## File map

| file | responsibility | task |
|---|---|---|
| `src/screener/screen/__init__.py` | public surface | 1, extended in 2–7 |
| `src/screener/screen/rows.py` | typed records, and parsing from a cursor or a `playground.Result` | 1 |
| `src/screener/screen/params.py` | query strings into typed parameters, or `BadParameter` | 2 |
| `src/screener/screen/shape.py` | typed rows into D9/D10 JSON | 3, extended in 7 |
| `src/screener/screen/queries.py` | every statement, as literals | 4, extended in 5–7 |
| `src/screener/screen/read.py` | run resolution, the screen read, the security read, refusals | 4, extended in 5, 7 |
| `src/screener/screen/explain.py` | reproduction and the six statuses | 6 |
| `src/screener/health/server.py` | the two routes | 8 |
| `tests/test_screen_rows.py`, `tests/test_screen_params.py`, `tests/test_screen_shape.py`, `tests/test_screen_explain.py` | pure tests | 1, 2, 3, 6 |
| `tests/test_screen_read.py` | runs, filters, sorts, tiles, closes, against hand-written rows | 4, 5 |
| `tests/test_screen_security.py` | reproduction and detail, against a really scored night | 6, 7 |
| `tests/test_screen_http.py` | handlers | 8 |

---

### Task 1: Typed rows that parse from either source

**Files:**
- Create: `src/screener/screen/__init__.py`
- Create: `src/screener/screen/rows.py`
- Test: `tests/test_screen_rows.py`

**Interfaces:**
- Consumes: `screener.playground.Result` (tests only).
- Produces:
  - `parse(record: type[R], cells: Sequence[Any]) -> R` and `parse_all(record: type[R], rows: Sequence[Sequence[Any]]) -> list[R]`.
  - Frozen dataclasses, with fields in this exact order, since every later query selects columns in it:
    - `RunRow(id: int, as_of: date, started_at: datetime, finished_at: datetime | None, git_sha: str, config_hash: str, weight_version: str, weight_version_id: int, cutoff_offset_seconds: int, logic: str, emits_alerts: bool)`
    - `PreviousRunRow(id: int, as_of: date)`
    - `ScreenRow(security_id: int, symbol: str, name: str, sector_code: str, sector_name: str, score: Decimal, previous_score: Decimal | None, v_score: Decimal | None, v_coverage: Decimal | None, q_score: Decimal | None, q_coverage: Decimal | None, m_score: Decimal | None, m_coverage: Decimal | None, pillar_agreement: int, min_coverage: Decimal)`
    - `TilesRow(scored: int, active_now: int, partial: int, agreement_3: int, market_ranked_values: int)`
    - `SectorRow(code: str, name: str)`
    - `BarRow(security_id: int, trade_date: date, close: Decimal, observed_at: datetime)`
    - `ActionRow(security_id: int, effective_date: date, action_type: str, ratio: Decimal | None, amount: Decimal | None)`
    - `SymbolRow(security_id: int, symbol: str, name: str, mic: str, is_active: bool)`
    - `ClassificationRow(sector_code: str, sector_name: str, industry_code: str | None, industry_name: str | None)`
    - `SnapshotRow(score: Decimal, pillar_agreement: int, min_coverage: Decimal)`
    - `PillarRow(pillar_code: str, score: Decimal, metric_count: int, coverage: Decimal)`
    - `MetricRow(code: str, raw_value: Decimal, percentile: Decimal, peer_group: str, peer_count: int, fallback_level: int, period_basis: str | None, period_end: date | None)`
    - `MetricInfoRow(code: str, name: str, higher_is_better: bool, pillar_code: str)`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_screen_rows.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_screen_rows.py -n0 -q`
Expected: collection error, `ModuleNotFoundError: No module named 'screener.screen'`.

- [ ] **Step 3: Write `rows.py`**

Create `src/screener/screen/rows.py`:

```python
"""Typed rows for the screen, parsed from either place a row comes from.

The status service reads on the application's own connection, where psycopg
hands back `Decimal`, `date` and `datetime`. Steven reads through
`playground.select`, whose `Result` cells were made JSON-safe on the way out --
decimals as strings, dates and times as ISO strings (ui-swap spec F12). Shaping
and adjustment must not care which, so both are parsed here into one record and
nothing past this module sees a raw cell.

Pure: no connection, no socket.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from types import NoneType, UnionType
from typing import Any, TypeVar, get_args, get_type_hints

R = TypeVar("R")


@dataclass(frozen=True)
class RunRow:
    id: int
    as_of: date
    started_at: datetime
    finished_at: datetime | None
    git_sha: str
    config_hash: str
    weight_version: str
    weight_version_id: int
    cutoff_offset_seconds: int
    logic: str
    emits_alerts: bool


@dataclass(frozen=True)
class PreviousRunRow:
    id: int
    as_of: date


@dataclass(frozen=True)
class ScreenRow:
    security_id: int
    symbol: str
    name: str
    sector_code: str
    sector_name: str
    score: Decimal
    previous_score: Decimal | None
    v_score: Decimal | None
    v_coverage: Decimal | None
    q_score: Decimal | None
    q_coverage: Decimal | None
    m_score: Decimal | None
    m_coverage: Decimal | None
    pillar_agreement: int
    min_coverage: Decimal


@dataclass(frozen=True)
class TilesRow:
    scored: int
    active_now: int
    partial: int
    agreement_3: int
    market_ranked_values: int


@dataclass(frozen=True)
class SectorRow:
    code: str
    name: str


@dataclass(frozen=True)
class BarRow:
    security_id: int
    trade_date: date
    close: Decimal
    observed_at: datetime


@dataclass(frozen=True)
class ActionRow:
    security_id: int
    effective_date: date
    action_type: str
    ratio: Decimal | None
    amount: Decimal | None


@dataclass(frozen=True)
class SymbolRow:
    security_id: int
    symbol: str
    name: str
    mic: str
    is_active: bool


@dataclass(frozen=True)
class ClassificationRow:
    sector_code: str
    sector_name: str
    industry_code: str | None
    industry_name: str | None


@dataclass(frozen=True)
class SnapshotRow:
    score: Decimal
    pillar_agreement: int
    min_coverage: Decimal


@dataclass(frozen=True)
class PillarRow:
    pillar_code: str
    score: Decimal
    metric_count: int
    coverage: Decimal


@dataclass(frozen=True)
class MetricRow:
    code: str
    raw_value: Decimal
    percentile: Decimal
    peer_group: str
    peer_count: int
    fallback_level: int
    period_basis: str | None
    period_end: date | None


@dataclass(frozen=True)
class MetricInfoRow:
    code: str
    name: str
    higher_is_better: bool
    pillar_code: str


def _concrete(hint: Any) -> Any:
    """`Decimal | None` -> `Decimal`; any other annotation unchanged."""
    if isinstance(hint, UnionType):
        (inner,) = [arg for arg in get_args(hint) if arg is not NoneType]
        return inner
    return hint


def _cell(kind: Any, value: Any) -> Any:
    if value is None:
        return None
    if kind is Decimal and not isinstance(value, Decimal):
        return Decimal(value)
    if kind is datetime and not isinstance(value, datetime):
        return datetime.fromisoformat(value)
    if kind is date and not isinstance(value, date):
        return date.fromisoformat(value)
    return value


def parse(record: type[R], cells: Sequence[Any]) -> R:
    """One row into `record`, its cells taken in the record's field order.

    A row of the wrong width is refused rather than zipped short: a query that
    gained or lost a column would otherwise shift values into the wrong fields
    and still construct a record.
    """
    hints = get_type_hints(record)
    if len(cells) != len(hints):
        raise ValueError(f"{record.__name__} takes {len(hints)} cells, got {len(cells)}")
    return record(
        **{
            name: _cell(_concrete(hint), cell)
            for (name, hint), cell in zip(hints.items(), cells)
        }
    )


def parse_all(record: type[R], rows: Sequence[Sequence[Any]]) -> list[R]:
    return [parse(record, row) for row in rows]
```

- [ ] **Step 4: Create the package surface**

Create `src/screener/screen/__init__.py`:

```python
"""The scored screen, read for the dashboard and for Steven (ui-swap spec D6).

Pure modules and reading modules are kept apart, as `screener.ingest` keeps
`parse` from `load`. `rows`, `params` and `shape` are pure: typed records parsed
from either a psycopg cursor or a `playground.Result`, query strings refused by
name, and JSON built from typed rows. `queries` holds every statement as a
literal. `read` resolves which night is served and reads it, and `explain`
re-runs scoring's explaining forms for one security under the run's own view,
giving each metric one of six statuses.
"""

from screener.screen.rows import (
    ActionRow,
    BarRow,
    ClassificationRow,
    MetricInfoRow,
    MetricRow,
    PillarRow,
    PreviousRunRow,
    RunRow,
    ScreenRow,
    SectorRow,
    SnapshotRow,
    SymbolRow,
    TilesRow,
    parse,
    parse_all,
)

__all__ = [
    "ActionRow",
    "BarRow",
    "ClassificationRow",
    "MetricInfoRow",
    "MetricRow",
    "PillarRow",
    "PreviousRunRow",
    "RunRow",
    "ScreenRow",
    "SectorRow",
    "SnapshotRow",
    "SymbolRow",
    "TilesRow",
    "parse",
    "parse_all",
]
```

- [ ] **Step 5: Run the tests to verify they pass, and typecheck**

```bash
.venv/bin/python -m pytest tests/test_screen_rows.py -n0 -q
.venv/bin/pyright src/screener/screen tests/test_screen_rows.py 2>&1 | tail -1
```

Expected: 4 passed; `0 errors, 0 warnings, 0 informations`.

- [ ] **Step 6: Commit**

```bash
git add src/screener/screen/__init__.py src/screener/screen/rows.py tests/test_screen_rows.py
git commit -m "Parse screen rows into one typed record, from a cursor or from the playground"
```

---

### Task 2: Query-string parameters, refused by name

**Files:**
- Create: `src/screener/screen/params.py`
- Modify: `src/screener/screen/__init__.py`
- Test: `tests/test_screen_params.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - Constants: `DEFAULT_LIMIT = 50`, `MAX_LIMIT = 100`, `MAX_OFFSET = 1_000_000`, `MAX_SYMBOL = 16`, `SORTS = ("score", "delta", "V", "Q", "M")`, `PARTIALS = ("only", "hide")`.
  - `class BadParameter(ValueError)`, with `.name: str`; `str(exc)` is the message.
  - `ScreenParams(run: int | None = None, sector: str | None = None, agree: int | None = None, partial: str | None = None, sort: str = "score", offset: int = 0, limit: int = 50)`, a frozen dataclass.
  - `SecurityParams(symbol: str, run: int | None = None)`, a frozen dataclass.
  - `screen_params(query: Mapping[str, Sequence[str]]) -> ScreenParams` and `security_params(query: Mapping[str, Sequence[str]]) -> SecurityParams`. Both take `urllib.parse.parse_qs` output.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_screen_params.py`:

```python
"""The screen endpoints' query strings (ui-swap spec D9, D10).

Every wrong value is a refusal that names its parameter. A quiet default would
serve a screen sorted or filtered differently from what was asked, with nothing
on the page saying so.
"""

import pytest

from screener.screen import BadParameter, ScreenParams, SecurityParams, screen_params, security_params


def test_no_parameters_is_the_first_page_of_the_latest_screen_by_score():
    assert screen_params({}) == ScreenParams(
        run=None, sector=None, agree=None, partial=None, sort="score", offset=0, limit=50
    )


def test_every_parameter_is_read():
    got = screen_params({
        "run": ["7"], "sector": ["consumer-cyclical"], "agree": ["2"], "partial": ["hide"],
        "sort": ["delta"], "offset": ["50"], "limit": ["100"],
    })

    assert got == ScreenParams(7, "consumer-cyclical", 2, "hide", "delta", 50, 100)


def test_unclassified_is_a_sector():
    assert screen_params({"sector": ["unclassified"]}).sector == "unclassified"


@pytest.mark.parametrize(
    "name,value",
    [
        ("sort", "price"),
        ("sort", "v"),
        ("agree", "0"),
        ("agree", "4"),
        ("partial", "yes"),
        ("offset", "-1"),
        ("offset", "1e3"),
        ("offset", "1000001"),
        ("limit", "0"),
        ("limit", "101"),
        ("run", "0"),
        ("run", "seven"),
        ("run", "+7"),
        ("run", "٧"),
        ("sector", "Technology"),
        ("sector", "tech; drop table security"),
    ],
)
def test_a_wrong_value_is_refused_naming_its_parameter(name, value):
    with pytest.raises(BadParameter) as caught:
        screen_params({name: [value]})

    assert caught.value.name == name
    assert name in str(caught.value)


def test_the_sort_refusal_lists_what_is_allowed():
    with pytest.raises(BadParameter, match="sort must be one of score, delta, V, Q, M"):
        screen_params({"sort": ["price"]})


def test_an_unknown_parameter_is_refused_by_name():
    with pytest.raises(BadParameter) as caught:
        screen_params({"colour": ["red"]})

    assert caught.value.name == "colour"


def test_a_parameter_given_twice_is_refused():
    with pytest.raises(BadParameter) as caught:
        screen_params({"sort": ["score", "V"]})

    assert caught.value.name == "sort"


def test_a_symbol_is_trimmed_of_whitespace_and_a_dollar_sign():
    assert security_params({"symbol": [" $jpm "], "run": ["7"]}) == SecurityParams("jpm", 7)


@pytest.mark.parametrize("query", [{}, {"symbol": ["$"]}, {"symbol": ["   "]}, {"symbol": ["A" * 17]}])
def test_a_missing_or_impossible_symbol_is_refused(query):
    with pytest.raises(BadParameter) as caught:
        security_params(query)

    assert caught.value.name == "symbol"


def test_the_security_endpoint_refuses_the_screen_s_filters():
    with pytest.raises(BadParameter) as caught:
        security_params({"symbol": ["JPM"], "sort": ["V"]})

    assert caught.value.name == "sort"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_screen_params.py -n0 -q`
Expected: collection error, `ImportError: cannot import name 'BadParameter' from 'screener.screen'`.

- [ ] **Step 3: Write `params.py`**

Create `src/screener/screen/params.py`:

```python
"""The screen endpoints' query strings, refused by name when wrong (ui-swap D9, D10).

Pure. A parameter that is present but wrong is a 400 naming it, never a quiet
default: a page that asked for `sort=value` and silently got `score` would show a
screen in the wrong order with nothing on it saying so.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

DEFAULT_LIMIT = 50
MAX_LIMIT = 100
# Far past any real screen (~1,500 rows), and short of anything that would reach
# Postgres as an out-of-range integer and come back as a 503.
MAX_OFFSET = 1_000_000
# Long enough for any listed symbol with a class suffix.
MAX_SYMBOL = 16
SORTS: tuple[str, ...] = ("score", "delta", "V", "Q", "M")
PARTIALS: tuple[str, ...] = ("only", "hide")

_MAX_ID = 2**63 - 1
# The universe loader's sector codes are lowercase words joined by hyphens. Only
# the shape is checked: whether a code exists is a question about the run's rows,
# and an unknown one returns an empty page (plan amendment P2).
_SECTOR = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_SCREEN = frozenset({"run", "sector", "agree", "partial", "sort", "offset", "limit"})
_SECURITY = frozenset({"symbol", "run"})


class BadParameter(ValueError):
    """A query-string parameter the endpoint cannot honour."""

    def __init__(self, name: str, message: str) -> None:
        super().__init__(message)
        self.name = name


@dataclass(frozen=True)
class ScreenParams:
    run: int | None = None
    sector: str | None = None
    agree: int | None = None
    partial: str | None = None
    sort: str = "score"
    offset: int = 0
    limit: int = DEFAULT_LIMIT


@dataclass(frozen=True)
class SecurityParams:
    symbol: str
    run: int | None = None


def _known(query: Mapping[str, Sequence[str]], allowed: frozenset[str]) -> None:
    for name in sorted(query):
        if name not in allowed:
            raise BadParameter(name, f"unknown parameter {name!r}")


def _one(query: Mapping[str, Sequence[str]], name: str) -> str | None:
    values = query.get(name) or []
    if len(values) > 1:
        raise BadParameter(name, f"{name} given more than once")
    return values[0] if values else None


def _integer(raw: str | None, name: str, *, low: int, high: int) -> int | None:
    if raw is None:
        return None
    # ASCII digits only: `int` also accepts " 7", "+7", "7_000" and other
    # scripts' digits, none of which a page sends.
    if not (raw.isascii() and raw.isdigit()) or not low <= int(raw) <= high:
        raise BadParameter(name, f"{name} must be a whole number from {low} to {high}")
    return int(raw)


def _choice(raw: str | None, name: str, choices: Sequence[str]) -> str | None:
    if raw is not None and raw not in choices:
        raise BadParameter(name, f"{name} must be one of {', '.join(choices)}")
    return raw


def screen_params(query: Mapping[str, Sequence[str]]) -> ScreenParams:
    """`/api/screen`'s parameters, from `urllib.parse.parse_qs` output."""
    _known(query, _SCREEN)
    sector = _one(query, "sector")
    if sector is not None and not _SECTOR.fullmatch(sector):
        raise BadParameter("sector", "sector must be a sector code or 'unclassified'")
    offset = _integer(_one(query, "offset"), "offset", low=0, high=MAX_OFFSET)
    limit = _integer(_one(query, "limit"), "limit", low=1, high=MAX_LIMIT)
    return ScreenParams(
        run=_integer(_one(query, "run"), "run", low=1, high=_MAX_ID),
        sector=sector,
        agree=_integer(_one(query, "agree"), "agree", low=1, high=3),
        partial=_choice(_one(query, "partial"), "partial", PARTIALS),
        sort=_choice(_one(query, "sort"), "sort", SORTS) or "score",
        offset=0 if offset is None else offset,
        limit=DEFAULT_LIMIT if limit is None else limit,
    )


def security_params(query: Mapping[str, Sequence[str]]) -> SecurityParams:
    """`/api/screen/security`'s parameters. A leading `$` is dropped, as people type one."""
    _known(query, _SECURITY)
    symbol = (_one(query, "symbol") or "").strip().removeprefix("$")
    if not symbol:
        raise BadParameter("symbol", "symbol is required")
    if len(symbol) > MAX_SYMBOL:
        raise BadParameter("symbol", f"symbol is longer than {MAX_SYMBOL} characters")
    return SecurityParams(
        symbol=symbol, run=_integer(_one(query, "run"), "run", low=1, high=_MAX_ID)
    )
```

- [ ] **Step 4: Export from the package**

In `src/screener/screen/__init__.py`, add this import after the `rows` import:

```python
from screener.screen.params import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MAX_OFFSET,
    MAX_SYMBOL,
    PARTIALS,
    SORTS,
    BadParameter,
    ScreenParams,
    SecurityParams,
    screen_params,
    security_params,
)
```

Then add the names to `__all__` at their RUF022 positions:
- SCREAMING_CASE, before `"ActionRow"`: `"DEFAULT_LIMIT"`, `"MAX_LIMIT"`, `"MAX_OFFSET"`, `"MAX_SYMBOL"`, `"PARTIALS"`, `"SORTS"`.
- CamelCase: `"BadParameter"` after `"BarRow"`, and `"ScreenParams"` and `"SecurityParams"` after `"ScreenRow"`.
- snake_case: `"screen_params"` and `"security_params"` after `"parse_all"`.

- [ ] **Step 5: Run the tests to verify they pass, and typecheck**

```bash
.venv/bin/python -m pytest tests/test_screen_params.py -n0 -q
.venv/bin/pyright src/screener/screen tests/test_screen_params.py 2>&1 | tail -1
```

Expected: all passed; `0 errors`.

- [ ] **Step 6: Commit**

```bash
git add src/screener/screen/params.py src/screener/screen/__init__.py tests/test_screen_params.py
git commit -m "Refuse a wrong screen parameter by name rather than serving a quiet default"
```

---

### Task 3: Shaping a screen page

**Files:**
- Create: `src/screener/screen/shape.py`
- Modify: `src/screener/screen/__init__.py`
- Test: `tests/test_screen_shape.py`

**Interfaces:**
- Consumes: every record from Task 1; `screener.scoring.Action`, `adjusted_closes`, `CODES`, `RATIO_CODES`.
- Produces:
  - Constants: `PILLAR_ORDER = ("valuation", "quality", "momentum")`, `PILLAR_KEYS = {"valuation": "V", "quality": "Q", "momentum": "M"}`, `PERCENT = "percent"`, `MULTIPLE = "multiple"`, `UNITS: dict[str, str]`, and `ALL_CODES = (*RATIO_CODES, *CODES)`.
  - Formatting: `one_decimal(value: Decimal | None) -> str | None`, `exact(value: Decimal | None) -> str | None`, `price(value: Decimal) -> str`.
  - Row arithmetic: `delta(row: ScreenRow) -> Decimal | None`, `partial(min_coverage: Decimal) -> bool`.
  - Closes: `closes(bars: Sequence[BarRow], actions: Sequence[ActionRow], count: int) -> list[list[str]]`.
  - Payload pieces: `sector(code: str, name: str) -> dict[str, str]`, `run_payload(run: RunRow) -> dict[str, Any]`, `screen_row(row: ScreenRow, row_closes: list[list[str]]) -> dict[str, Any]`.
  - `screen_payload(*, run: RunRow, latest: bool, previous: PreviousRunRow | None, tiles: TilesRow, sectors: Sequence[SectorRow], total: int, rows: Sequence[ScreenRow], closes_by_security: Mapping[int, list[list[str]]]) -> dict[str, Any]`.
  - `awaiting() -> dict[str, Any]`, which returns `{"state": "awaiting_first_night"}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_screen_shape.py`:

```python
"""Typed rows into the screen's JSON (ui-swap spec D5, D9, D11)."""

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from screener.scoring import CODES, RATIO_CODES
from screener.screen import (
    UNITS,
    ActionRow,
    BarRow,
    PreviousRunRow,
    RunRow,
    ScreenRow,
    SectorRow,
    TilesRow,
    closes,
    delta,
    exact,
    one_decimal,
    price,
    screen_payload,
    screen_row,
)

GEF = ScreenRow(
    11, "GEF", "Greif", "consumer-cyclical", "Consumer Cyclical", Decimal("84.8"), None,
    None, None, Decimal("79"), Decimal("0.75"), Decimal("91"), Decimal("1"), 2, Decimal("0"),
)
RUN = RunRow(
    7, date(2026, 9, 13), datetime(2026, 9, 13, 23, tzinfo=timezone.utc),
    datetime(2026, 9, 13, 23, 19, tzinfo=timezone.utc), "6b1c111", "9f3a71c2", "v2", 3,
    108000, "v2 momentum", False,
)


def _at(day: int) -> datetime:
    return datetime(2026, 3, day, 23, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "value,shown",
    [(Decimal("84.85"), "84.9"), (Decimal("84.8499"), "84.8"), (Decimal("79"), "79.0"), (None, None)],
)
def test_scores_have_one_decimal_rounded_half_up(value, shown):
    assert one_decimal(value) == shown


@pytest.mark.parametrize(
    "value,shown",
    [(Decimal("0.0741"), "0.0741"), (Decimal("1E+1"), "10"), (Decimal("0.75"), "0.75"), (None, None)],
)
def test_raw_values_are_exact_and_never_in_exponent_form(value, shown):
    assert exact(value) == shown


@pytest.mark.parametrize(
    "value,shown",
    [
        (Decimal("41.2"), "41.20"),
        (Decimal("999.994"), "999.99"),
        (Decimal("1000.4"), "1000"),
        (Decimal("2718.5"), "2719"),
    ],
)
def test_closes_have_two_decimals_below_a_thousand_and_none_from_it(value, shown):
    assert price(value) == shown


def test_three_metrics_are_multiples_and_every_other_is_a_percent():
    assert set(UNITS) == {*RATIO_CODES, *CODES}
    assert {code for code, unit in UNITS.items() if unit == "multiple"} == {
        "book_yield", "debt_to_equity", "interest_cover",
    }


def test_delta_is_null_without_a_previous_score():
    assert delta(GEF) is None
    assert delta(replace(GEF, previous_score=Decimal("80.1"))) == Decimal("4.7")


def test_a_split_fetched_before_its_bar_is_applied_and_one_fetched_after_is_not():
    split = [ActionRow(4, date(2026, 3, 3), "split", Decimal(2), None)]
    # Fetched the night before the split: Yahoo had not restated it yet.
    unrestated = [
        BarRow(4, date(2026, 3, 2), Decimal("200"), _at(2)),
        BarRow(4, date(2026, 3, 3), Decimal("100"), _at(3)),
    ]
    # Refetched after the split: Yahoo already halved it, so halving again would
    # be the double count PR #49 fixed.
    restated = [
        BarRow(4, date(2026, 3, 2), Decimal("100"), _at(4)),
        BarRow(4, date(2026, 3, 3), Decimal("100"), _at(4)),
    ]

    continuous = [["2026-03-02", "100.00"], ["2026-03-03", "100.00"]]
    assert closes(unrestated, split, 60) == continuous
    assert closes(restated, split, 60) == continuous


def test_only_the_newest_closes_are_kept():
    bars = [BarRow(4, date(2026, 3, day), Decimal(day), _at(day)) for day in range(1, 6)]

    assert closes(bars, [], 2) == [["2026-03-04", "4.00"], ["2026-03-05", "5.00"]]


def test_a_partial_row_with_an_absent_pillar():
    assert screen_row(GEF, [["2026-03-02", "41.20"]]) == {
        "symbol": "GEF",
        "name": "Greif",
        "sector": {"code": "consumer-cyclical", "name": "Consumer Cyclical"},
        "score": "84.8",
        "delta": None,
        "pillars": {
            "V": None,
            "Q": {"score": "79.0", "coverage": "0.75"},
            "M": {"score": "91.0", "coverage": "1"},
        },
        "agreement": 2,
        "min_coverage": "0",
        "partial": True,
        "closes": [["2026-03-02", "41.20"]],
    }


def test_a_fully_covered_row_is_not_partial():
    assert screen_row(replace(GEF, min_coverage=Decimal(1)), [])["partial"] is False


def test_the_page_carries_its_run_its_previous_night_and_its_tiles():
    shown = screen_payload(
        run=RUN,
        latest=True,
        previous=PreviousRunRow(6, date(2026, 9, 12)),
        tiles=TilesRow(1499, 1504, 41, 58, 15),
        sectors=[SectorRow("technology", "Technology")],
        total=1,
        rows=[GEF],
        closes_by_security={},
    )

    assert set(shown) == {
        "state", "latest", "run", "previous_as_of", "tiles", "sectors", "total", "rows",
    }
    assert shown["state"] == "ready"
    assert shown["run"] == {
        "id": 7, "as_of": "2026-09-13", "started_at": "2026-09-13T23:00:00+00:00",
        "finished_at": "2026-09-13T23:19:00+00:00", "git_sha": "6b1c111",
        "config_hash": "9f3a71c2", "weight_version": "v2", "cutoff_offset_seconds": 108000,
        "logic": "v2 momentum", "emits_alerts": False,
    }
    assert shown["previous_as_of"] == "2026-09-12"
    assert shown["tiles"] == {
        "scored": 1499, "active_now": 1504, "partial": 41, "agreement_3": 58,
        "market_ranked_values": 15,
    }
    assert shown["sectors"] == [{"code": "technology", "name": "Technology"}]
    assert shown["rows"][0]["closes"] == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_screen_shape.py -n0 -q`
Expected: collection error, `ImportError: cannot import name 'UNITS' from 'screener.screen'`.

- [ ] **Step 3: Write `shape.py`**

Create `src/screener/screen/shape.py`:

```python
"""Typed rows into the JSON the screen endpoints send (ui-swap spec D5, D9, D10).

Pure. Every figure leaves as a decimal string, never a float: a stored value and
its reproduction that differ in the seventh digit must not arrive as two floats
that print identically (D5). Rounding happens here, once, half up, so the page
formats units and never rounds again.
"""

from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from screener.scoring import CODES, RATIO_CODES, Action, adjusted_closes
from screener.screen.rows import (
    ActionRow,
    BarRow,
    PreviousRunRow,
    RunRow,
    ScreenRow,
    SectorRow,
    TilesRow,
)

PILLAR_ORDER: tuple[str, ...] = ("valuation", "quality", "momentum")
PILLAR_KEYS: dict[str, str] = {"valuation": "V", "quality": "Q", "momentum": "M"}
ALL_CODES: tuple[str, ...] = (*RATIO_CODES, *CODES)

PERCENT = "percent"
MULTIPLE = "multiple"
# D5: three metrics are multiples; every other is a fraction shown as a percent.
UNITS: dict[str, str] = {
    code: MULTIPLE if code in ("book_yield", "debt_to_equity", "interest_cover") else PERCENT
    for code in ALL_CODES
}

_TENTH = Decimal("0.1")
_CENT = Decimal("0.01")
_WHOLE = Decimal(1)
# D5: a close of a thousand or more is shown without decimals.
_WHOLE_PRICE_FROM = Decimal(1000)


def one_decimal(value: Decimal | None) -> str | None:
    """A score or a percentile."""
    return None if value is None else str(value.quantize(_TENTH, rounding=ROUND_HALF_UP))


def exact(value: Decimal | None) -> str | None:
    """A raw or reproduced metric value, or a coverage, unrounded.

    Fixed-point rather than `str`, which writes a stored `1E+1` in exponent form
    that the page would then have to parse.
    """
    return None if value is None else format(value, "f")


def price(value: Decimal) -> str:
    step = _WHOLE if abs(value) >= _WHOLE_PRICE_FROM else _CENT
    return str(value.quantize(step, rounding=ROUND_HALF_UP))


def delta(row: ScreenRow) -> Decimal | None:
    """The change since the previous comparable night, or None when there is nothing to compare."""
    return None if row.previous_score is None else row.score - row.previous_score


def partial(min_coverage: Decimal) -> bool:
    return min_coverage < 1


def closes(
    bars: Sequence[BarRow], actions: Sequence[ActionRow], count: int
) -> list[list[str]]:
    """The newest `count` adjusted closes, as `[date, price]` pairs (D11).

    Adjusted by scoring's own `adjusted_closes`, so the chart and momentum cannot
    disagree about a split -- including one Yahoo had already applied to a bar by
    the time it was fetched.
    """
    adjusted = adjusted_closes(
        [(bar.trade_date, bar.close, bar.observed_at) for bar in bars],
        [Action(a.effective_date, a.action_type, a.ratio, a.amount) for a in actions],
    )
    return [[day.isoformat(), price(close)] for day, close in adjusted[-count:]]


def awaiting() -> dict[str, Any]:
    """No v2 night qualifies yet (D7). No time is promised: this process does not hold the scheduler's."""
    return {"state": "awaiting_first_night"}


def sector(code: str, name: str) -> dict[str, str]:
    return {"code": code, "name": name}


def run_payload(run: RunRow) -> dict[str, Any]:
    return {
        "id": run.id,
        "as_of": run.as_of.isoformat(),
        "started_at": run.started_at.isoformat(),
        "finished_at": None if run.finished_at is None else run.finished_at.isoformat(),
        "git_sha": run.git_sha,
        "config_hash": run.config_hash,
        "weight_version": run.weight_version,
        "cutoff_offset_seconds": run.cutoff_offset_seconds,
        "logic": run.logic,
        "emits_alerts": run.emits_alerts,
    }


def _pillar(score: Decimal | None, coverage: Decimal | None) -> dict[str, str | None] | None:
    if score is None:
        return None
    return {"score": one_decimal(score), "coverage": exact(coverage)}


def screen_row(row: ScreenRow, row_closes: list[list[str]]) -> dict[str, Any]:
    return {
        "symbol": row.symbol,
        "name": row.name,
        "sector": sector(row.sector_code, row.sector_name),
        "score": one_decimal(row.score),
        "delta": one_decimal(delta(row)),
        "pillars": {
            "V": _pillar(row.v_score, row.v_coverage),
            "Q": _pillar(row.q_score, row.q_coverage),
            "M": _pillar(row.m_score, row.m_coverage),
        },
        "agreement": row.pillar_agreement,
        "min_coverage": exact(row.min_coverage),
        "partial": partial(row.min_coverage),
        "closes": row_closes,
    }


def screen_payload(
    *,
    run: RunRow,
    latest: bool,
    previous: PreviousRunRow | None,
    tiles: TilesRow,
    sectors: Sequence[SectorRow],
    total: int,
    rows: Sequence[ScreenRow],
    closes_by_security: Mapping[int, list[list[str]]],
) -> dict[str, Any]:
    return {
        "state": "ready",
        "latest": latest,
        "run": run_payload(run),
        "previous_as_of": None if previous is None else previous.as_of.isoformat(),
        "tiles": {
            "scored": tiles.scored,
            "active_now": tiles.active_now,
            "partial": tiles.partial,
            "agreement_3": tiles.agreement_3,
            "market_ranked_values": tiles.market_ranked_values,
        },
        "sectors": [sector(row.code, row.name) for row in sectors],
        "total": total,
        "rows": [
            screen_row(row, closes_by_security.get(row.security_id, [])) for row in rows
        ],
    }
```

- [ ] **Step 4: Export from the package**

In `src/screener/screen/__init__.py`, add this import after the `params` import:

```python
from screener.screen.shape import (
    ALL_CODES,
    MULTIPLE,
    PERCENT,
    PILLAR_KEYS,
    PILLAR_ORDER,
    UNITS,
    awaiting,
    closes,
    delta,
    exact,
    one_decimal,
    partial,
    price,
    run_payload,
    screen_payload,
    screen_row,
    sector,
)
```

Then add every name to `__all__` at its RUF022 position:
- SCREAMING_CASE: `"ALL_CODES"` first, `"MULTIPLE"` after `"MAX_SYMBOL"`, `"PERCENT"`, `"PILLAR_KEYS"` and `"PILLAR_ORDER"` after `"PARTIALS"`, and `"UNITS"` after `"SORTS"`.
- snake_case, alphabetically among the existing names: `awaiting`, `closes`, `delta`, `exact`, `one_decimal`, `partial`, `price`, `run_payload`, `screen_payload`, `screen_row`, `sector`.

- [ ] **Step 5: Run the tests to verify they pass, and typecheck**

```bash
.venv/bin/python -m pytest tests/test_screen_shape.py -n0 -q
.venv/bin/pyright src/screener/screen tests/test_screen_shape.py 2>&1 | tail -1
```

Expected: all passed; `0 errors`.

- [ ] **Step 6: Commit**

```bash
git add src/screener/screen/shape.py src/screener/screen/__init__.py tests/test_screen_shape.py
git commit -m "Shape a screen page as decimal strings, with closes adjusted by scoring's own rule"
```

---

### Task 4: Which night is served

**Files:**
- Create: `src/screener/screen/queries.py`
- Create: `src/screener/screen/read.py`
- Modify: `src/screener/screen/__init__.py`
- Test: `tests/test_screen_read.py`

**Interfaces:**
- Consumes:
  - `RunRow`, `PreviousRunRow`, `parse_all` (Task 1).
  - `screener.scoring.LOGIC_DESCRIPTION`.
  - The `fresh_db` and `an_observation` fixtures.
- Produces:
  - In `queries`: `LATEST_RUN`, `RUN_BY_ID` and `PREVIOUS_RUN`, all `LiteralString`s selecting `RunRow` / `PreviousRunRow` columns in field order.
  - In `read`: `class RunChanged(LookupError)`, with `.run_id: int`.
  - `resolve_run(conn: psycopg.Connection, pinned: int | None) -> tuple[RunRow, bool] | None`. The bool is `latest`.
  - `previous_run(conn: psycopg.Connection, run: RunRow) -> PreviousRunRow | None`.
  - The private helpers `_all(conn, query, bind, record) -> list[R]` and `_one(...) -> R | None`, which Tasks 5 and 7 reuse.
  - In `tests/test_screen_read.py`: the `World` dataclass and the `world` fixture, which Task 5 extends.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_screen_read.py`:

```python
"""The screen's reads against a real database (ui-swap spec D7-D9, D11).

Runs, snapshots and pillar scores are written by hand rather than by running
scoring: what is under test is which night is served and how its rows are
filtered, sorted and paged, and that wants scores chosen for the purpose.
`test_screen_security.py` is where scoring really runs.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from screener.scoring import LOGIC_DESCRIPTION
from screener.screen import RunChanged, RunRow, previous_run, resolve_run

AS_OF = date(2026, 3, 2)
SEEN = datetime(2026, 3, 2, 22, tzinfo=timezone.utc)
V1_LOGIC = "v1 momentum: four price metrics, sector percentiles"


@dataclass
class World:
    # `Any`, because the fixture's connection is untyped and a typed one would make
    # every `fetchone()[0]` below an optional-subscript error.
    conn: Any
    observe: Callable[[int], int]
    market: int
    groups: dict[str, int]
    nodes: dict[str, int]
    observations: dict[int, int] = field(default_factory=dict)

    def security(
        self,
        symbol: str,
        node: str | None = "software",
        *,
        active: bool = True,
        classified_from: date = date(2020, 1, 1),
    ) -> int:
        security_id = self.conn.execute(
            """insert into security
               (name, mic, currency, country, primary_symbol, first_seen, is_active)
               values (%s, 'XNAS', 'USD', 'US', %s, '2020-01-01', %s) returning id""",
            (f"{symbol} Inc", symbol, active),
        ).fetchone()[0]
        self.conn.execute(
            """insert into security_symbol (security_id, symbol, mic, valid_from, source)
               values (%s, %s, 'XNAS', '2020-01-01', 'test')""",
            (security_id, symbol),
        )
        if node is not None:
            self.conn.execute(
                """insert into security_sector (security_id, sector_node_id, valid_from, source)
                   values (%s, %s, %s, 'yfinance')""",
                (security_id, self.nodes[node], classified_from),
            )
        return security_id

    def run(
        self,
        as_of: date = AS_OF,
        *,
        logic: str = LOGIC_DESCRIPTION,
        weight: str = "v2",
        outcome: str = "ok",
        status: str = "live",
    ) -> int:
        started = datetime.combine(as_of, time(23), tzinfo=timezone.utc)
        return self.conn.execute(
            """insert into scoring_run
               (as_of_range, cutoff_offset, logic_version_id, weight_version_id, status,
                emits_alerts, git_sha, config_hash, started_at, finished_at, outcome)
               select daterange(%(as_of)s, %(next)s, '[)'), interval '30 hours', l.id, w.id,
                      %(status)s, false, 'abc1234', %(hash)s, %(started)s, %(started)s,
                      %(outcome)s
                 from scoring_logic_version l, weight_version w
                where l.description = %(logic)s and w.code = %(weight)s
               returning id""",
            {
                "as_of": as_of, "next": as_of + timedelta(days=1), "status": status,
                "hash": b"\x9f\x3a", "started": started, "outcome": outcome,
                "logic": logic, "weight": weight,
            },
        ).fetchone()[0]

    def snapshot(
        self,
        run_id: int,
        security_id: int,
        score: str,
        *,
        as_of: date = AS_OF,
        agreement: int = 1,
        min_coverage: str = "1",
        pillars: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        """A snapshot and its pillar rows; by default one Momentum pillar at the blended score."""
        self.conn.execute(
            """insert into snapshot_daily
               (as_of, scoring_run_id, security_id, blended_score, pillar_agreement,
                min_coverage, worst_fallback_level)
               values (%s, %s, %s, %s, %s, %s, 1)""",
            (as_of, run_id, security_id, Decimal(score), agreement, Decimal(min_coverage)),
        )
        for code, (pillar_score, coverage) in (pillars or {"momentum": (score, "1")}).items():
            self.conn.execute(
                """insert into pillar_score_daily
                   (as_of, scoring_run_id, security_id, pillar_id, score, metric_count, coverage)
                   select %s, %s, %s, p.id, %s, 1, %s from pillar p where p.code = %s""",
                (as_of, run_id, security_id, Decimal(pillar_score), Decimal(coverage), code),
            )

    def metric(
        self, run_id: int, security_id: int, code: str, raw: str, *, level: int = 1
    ) -> None:
        group = self.market if level == 0 else self.groups["technology"]
        self.conn.execute(
            """insert into metric_daily
               (as_of, scoring_run_id, security_id, metric_id, raw_value, percentile,
                peer_group_id, peer_count, fallback_level)
               select %s, %s, %s, m.id, %s, 50, %s, 20, %s from metric m where m.code = %s""",
            (AS_OF, run_id, security_id, Decimal(raw), group, level, code),
        )

    def bar(
        self, security_id: int, day: date, close: str, *, observed_at: datetime = SEEN
    ) -> None:
        if security_id not in self.observations:
            self.observations[security_id] = self.observe(security_id)
        value = Decimal(close)
        self.conn.execute(
            """insert into price_daily
               (security_id, trade_date, open, high, low, close, volume, observed_at,
                ingest_observation_id)
               values (%s, %s, %s, %s, %s, %s, 1, %s, %s)""",
            (security_id, day, value, value, value, value, observed_at,
             self.observations[security_id]),
        )


@pytest.fixture
def world(fresh_db, an_observation) -> World:
    """Three sectors: Technology and Energy each with one industry, and Utilities
    with none, so a security can be classified straight at a sector node."""
    scheme = fresh_db.execute(
        "insert into sector_scheme (code, name) values ('yfinance', 'yfinance') returning id"
    ).fetchone()[0]
    market = fresh_db.execute(
        "insert into peer_group (scheme_id, sector_node_id, level, code)"
        " values (%s, null, 0, 'market') returning id",
        (scheme,),
    ).fetchone()[0]
    groups: dict[str, int] = {}
    nodes: dict[str, int] = {}
    for sector_code, sector_name, industry_code, industry_name in (
        ("technology", "Technology", "software", "Software"),
        ("energy", "Energy", "oil-gas-integrated", "Oil & Gas Integrated"),
        ("utilities", "Utilities", None, None),
    ):
        nodes[sector_code] = fresh_db.execute(
            "insert into sector_node (scheme_id, level, code, name)"
            " values (%s, 1, %s, %s) returning id",
            (scheme, sector_code, sector_name),
        ).fetchone()[0]
        groups[sector_code] = fresh_db.execute(
            "insert into peer_group (scheme_id, sector_node_id, level, code)"
            " values (%s, %s, 1, %s) returning id",
            (scheme, nodes[sector_code], sector_code),
        ).fetchone()[0]
        if industry_code is not None:
            nodes[industry_code] = fresh_db.execute(
                "insert into sector_node (scheme_id, parent_id, level, code, name)"
                " values (%s, %s, 2, %s, %s) returning id",
                (scheme, nodes[sector_code], industry_code, industry_name),
            ).fetchone()[0]
    return World(fresh_db, an_observation, market, groups, nodes)


def _serve(world: World, pinned: int | None = None) -> tuple[RunRow, bool]:
    resolved = resolve_run(world.conn, pinned)
    assert resolved is not None
    return resolved


# -- which night (D7, D8) ----------------------------------------------------


def test_no_night_scored_under_v2_means_there_is_nothing_to_serve(world):
    world.run(logic=V1_LOGIC, weight="v1")

    assert resolve_run(world.conn, None) is None


def test_a_failed_night_is_passed_over_for_the_last_good_one(world):
    good = world.run(AS_OF)
    world.run(AS_OF + timedelta(days=1), outcome="failed")

    run, latest = _serve(world)

    assert (run.id, run.as_of, latest) == (good, AS_OF, True)


def test_the_latest_good_live_night_is_served_with_its_provenance(world):
    world.run(AS_OF)
    newer = world.run(AS_OF + timedelta(days=1))
    world.run(AS_OF + timedelta(days=2), status="experiment")

    run, latest = _serve(world)

    assert run.id == newer and latest
    assert (run.weight_version, run.cutoff_offset_seconds, run.git_sha, run.config_hash) == (
        "v2", 108000, "abc1234", "9f3a",
    )


def test_a_pinned_night_is_still_served_after_a_newer_one_lands(world):
    pinned = world.run(AS_OF)
    world.run(AS_OF + timedelta(days=1))

    run, latest = _serve(world, pinned)

    assert run.id == pinned and latest is False


@pytest.mark.parametrize("which", ["unknown", "v1", "failed"])
def test_a_pinned_night_that_does_not_qualify_is_refused(world, which):
    world.run(AS_OF)
    if which == "unknown":
        pinned = 999_999
    elif which == "v1":
        pinned = world.run(AS_OF - timedelta(days=1), logic=V1_LOGIC, weight="v1")
    else:
        pinned = world.run(AS_OF + timedelta(days=1), outcome="failed")

    with pytest.raises(RunChanged) as caught:
        resolve_run(world.conn, pinned)

    assert caught.value.run_id == pinned


def test_delta_compares_with_the_last_good_earlier_night_under_the_same_weights(world):
    world.run(AS_OF - timedelta(days=3))
    wanted = world.run(AS_OF - timedelta(days=2))
    world.run(AS_OF - timedelta(days=1), outcome="failed")
    run, _ = _serve(world, world.run(AS_OF))

    previous = previous_run(world.conn, run)

    assert previous is not None
    assert (previous.id, previous.as_of) == (wanted, AS_OF - timedelta(days=2))


def test_a_change_of_weights_starts_delta_afresh(world):
    world.run(AS_OF - timedelta(days=1), weight="v1")
    run, _ = _serve(world, world.run(AS_OF))

    assert previous_run(world.conn, run) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_screen_read.py -n0 -q`
Expected: collection error, `ImportError: cannot import name 'RunChanged' from 'screener.screen'`.

- [ ] **Step 3: Write `queries.py`**

Create `src/screener/screen/queries.py`:

```python
"""Every statement the screen runs, as literals (ui-swap spec §4).

Only bound parameters vary. Where two statements share a clause it is one
literal joined to another, which is still a `LiteralString` -- the type psycopg
requires so that SQL assembled from runtime values is a type error.

Parameters are named. Piece (c) reads some of these through `playground.select`
for Steven and widens that function to take a mapping (plan amendment P6).

Every select lists its columns in the field order of the `rows` record it is
parsed into; `rows.parse` refuses a row of the wrong width.
"""

from typing import LiteralString

# D7: live, finished well, and scored by the logic `screener.scoring.run` writes
# today, so a v1 night -- momentum only, split-distorted -- is never the screen.
_QUALIFYING_RUNS: LiteralString = """
select r.id, lower(r.as_of_range), r.started_at, r.finished_at, r.git_sha,
       encode(r.config_hash, 'hex'), w.code, r.weight_version_id,
       extract(epoch from r.cutoff_offset)::bigint, l.description, r.emits_alerts
  from scoring_run r
  join scoring_logic_version l on l.id = r.logic_version_id
  join weight_version w on w.id = r.weight_version_id
 where r.status = 'live'
   and r.outcome = 'ok'
   and l.description = %(logic)s
"""

LATEST_RUN: LiteralString = _QUALIFYING_RUNS + """
 order by lower(r.as_of_range) desc, r.id desc
 limit 1
"""

RUN_BY_ID: LiteralString = _QUALIFYING_RUNS + """
   and r.id = %(run)s::bigint
"""

# D7: a weight change starts Δ afresh, because a score under other weights is
# not the same measurement.
PREVIOUS_RUN: LiteralString = """
select r.id, lower(r.as_of_range)
  from scoring_run r
  join scoring_logic_version l on l.id = r.logic_version_id
 where r.status = 'live'
   and r.outcome = 'ok'
   and l.description = %(logic)s
   and lower(r.as_of_range) < %(as_of)s
   and r.weight_version_id = %(weight)s
 order by lower(r.as_of_range) desc, r.id desc
 limit 1
"""
```

- [ ] **Step 4: Write `read.py`**

Create `src/screener/screen/read.py`:

```python
"""The screen's reads on a connection, and the refusals they raise (ui-swap D7-D10).

On the connection the caller opens, which for the endpoints is the application's
own: the statements are fixed in `queries` and only bound parameters vary, which
is not what the playground's read-only roles exist to guard against (D6).
"""

from collections.abc import Mapping
from typing import Any, LiteralString, TypeVar

import psycopg

from screener.scoring import LOGIC_DESCRIPTION
from screener.screen import queries
from screener.screen.rows import PreviousRunRow, RunRow, parse_all

R = TypeVar("R")


class RunChanged(LookupError):
    """A pinned run no longer qualifies as a screen night (D8)."""

    def __init__(self, run_id: int) -> None:
        super().__init__(f"run {run_id} is not a scored v2 night")
        self.run_id = run_id


def _all(
    conn: psycopg.Connection, query: LiteralString, bind: Mapping[str, Any], record: type[R]
) -> list[R]:
    return parse_all(record, conn.execute(query, bind).fetchall())


def _one(
    conn: psycopg.Connection, query: LiteralString, bind: Mapping[str, Any], record: type[R]
) -> R | None:
    found = _all(conn, query, bind, record)
    return found[0] if found else None


def resolve_run(
    conn: psycopg.Connection, pinned: int | None
) -> tuple[RunRow, bool] | None:
    """The run to serve and whether it is the latest, or None when no v2 night exists.

    A pinned run goes on being served after a newer night lands, so paging through
    a screen never mixes two nights; it is refused only once it stops qualifying
    at all (D8).
    """
    bind = {"logic": LOGIC_DESCRIPTION, "run": pinned}
    latest = _one(conn, queries.LATEST_RUN, bind, RunRow)
    if pinned is None:
        return None if latest is None else (latest, True)
    run = _one(conn, queries.RUN_BY_ID, bind, RunRow)
    if run is None:
        raise RunChanged(pinned)
    return run, latest is not None and latest.id == run.id


def previous_run(conn: psycopg.Connection, run: RunRow) -> PreviousRunRow | None:
    """The night Δ is measured against: earlier, and under the same weights (D7)."""
    return _one(
        conn,
        queries.PREVIOUS_RUN,
        {"logic": LOGIC_DESCRIPTION, "as_of": run.as_of, "weight": run.weight_version_id},
        PreviousRunRow,
    )
```

- [ ] **Step 5: Export from the package**

In `src/screener/screen/__init__.py`, add this import after the `params` import. It goes before `rows` alphabetically, which is fine: `read` imports `rows` itself.

```python
from screener.screen.read import RunChanged, previous_run, resolve_run
```

Add `"RunChanged"` to `__all__`'s CamelCase group after `"PreviousRunRow"`, and add `"previous_run"` and `"resolve_run"` to the snake_case group at their alphabetical positions.

- [ ] **Step 6: Run the tests to verify they pass, and typecheck**

```bash
.venv/bin/python -m pytest tests/test_screen_read.py -n0 -q
.venv/bin/pyright src/screener/screen tests/test_screen_read.py 2>&1 | tail -1
```

Expected: all passed; `0 errors`.

- [ ] **Step 7: Commit**

```bash
git add src/screener/screen/queries.py src/screener/screen/read.py src/screener/screen/__init__.py tests/test_screen_read.py
git commit -m "Serve the latest good v2 night, and keep serving a pinned one after a newer lands"
```

---

### Task 5: The screen page: rows, filters, sorts, tiles and closes

**Files:**
- Modify: `src/screener/screen/queries.py` (append)
- Modify: `src/screener/screen/read.py` (append)
- Modify: `src/screener/screen/__init__.py`
- Test: `tests/test_screen_read.py` (append)

**Interfaces:**
- Consumes:
  - `ScreenParams` and `screen_params` (Task 2).
  - `shape.screen_payload`, `shape.closes` and `shape.awaiting` (Task 3).
  - `resolve_run`, `previous_run`, `_all` and `_one` (Task 4).
- Produces:
  - In `queries`: `SECTOR_AT_AS_OF`, a join fragment that expects the security aliased `sec`. Also `TILES`, `SECTORS`, `SCREEN_COUNT`, `ORDER_BY: dict[str, LiteralString]`, `screen_page(sort: str) -> LiteralString`, `CLOSES` and `ACTIONS`.
  - In `read`: `SPARKLINE_CLOSES = 30`, `CHART_CLOSES = 60` and `CLOSE_LOOKBACK_DAYS = 120`.
  - `_closes(conn, security_ids: Sequence[int], as_of: date, count: int) -> dict[int, list[list[str]]]`, which Task 7 reuses.
  - `read_screen(conn: psycopg.Connection, params: ScreenParams) -> dict[str, Any]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_screen_read.py`, and add `read_screen, screen_params` to its `from screener.screen import` line:

```python
# -- the page (D9, D11) ------------------------------------------------------


def ask(world: World, **query: object) -> dict[str, Any]:
    return read_screen(world.conn, screen_params({name: [str(value)] for name, value in query.items()}))


def symbols(payload: dict[str, Any]) -> list[str]:
    return [row["symbol"] for row in payload["rows"]]


def test_before_the_first_v2_night_the_screen_says_so(world):
    assert ask(world) == {"state": "awaiting_first_night"}


def test_delta_against_the_previous_night_and_new_for_a_newly_scored_security(world):
    old, new = world.security("OLD"), world.security("NEW")
    yesterday = AS_OF - timedelta(days=1)
    world.snapshot(world.run(yesterday), old, "70.0", as_of=yesterday)
    tonight = world.run()
    world.snapshot(tonight, old, "74.25")
    world.snapshot(tonight, new, "60")

    shown = ask(world)
    rows = {row["symbol"]: row for row in shown["rows"]}

    assert shown["previous_as_of"] == yesterday.isoformat()
    assert rows["OLD"]["delta"] == "4.3"
    assert rows["NEW"]["delta"] is None


def test_delta_is_null_across_a_weight_change(world):
    held = world.security("HELD")
    yesterday = AS_OF - timedelta(days=1)
    world.snapshot(world.run(yesterday, weight="v1"), held, "70", as_of=yesterday)
    world.snapshot(world.run(), held, "74")

    shown = ask(world)

    assert shown["previous_as_of"] is None
    assert shown["rows"][0]["delta"] is None


def test_a_pinned_night_serves_its_own_rows_after_a_newer_one_lands(world):
    held = world.security("HELD")
    first = world.run()
    world.snapshot(first, held, "50")
    tomorrow = AS_OF + timedelta(days=1)
    world.snapshot(world.run(tomorrow), held, "60", as_of=tomorrow)

    shown = ask(world, run=first)

    assert (shown["run"]["id"], shown["latest"]) == (first, False)
    assert shown["rows"][0]["score"] == "50.0"


def test_each_filter_narrows_the_rows_and_the_total(world):
    run = world.run()
    world.snapshot(run, world.security("TFUL"), "80", agreement=3)
    world.snapshot(run, world.security("TPRT"), "70", agreement=2, min_coverage="0.5")
    world.snapshot(run, world.security("OIL", "oil-gas-integrated"), "60")
    world.snapshot(run, world.security("LOOS", None), "50", agreement=0)

    def seen(**query: object) -> tuple[list[str], int]:
        shown = ask(world, **query)
        return symbols(shown), shown["total"]

    assert seen() == (["TFUL", "TPRT", "OIL", "LOOS"], 4)
    assert seen(sector="technology") == (["TFUL", "TPRT"], 2)
    assert seen(sector="unclassified") == (["LOOS"], 1)
    assert seen(sector="no-such-sector") == ([], 0)
    assert seen(agree=2) == (["TFUL", "TPRT"], 2)
    assert seen(partial="only") == (["TPRT"], 1)
    assert seen(partial="hide") == (["TFUL", "OIL", "LOOS"], 3)
    assert seen(sector="technology", partial="hide") == (["TFUL"], 1)
    assert ask(world, sector="energy")["sectors"] == [
        {"code": "energy", "name": "Energy"},
        {"code": "technology", "name": "Technology"},
        {"code": "unclassified", "name": "Unclassified"},
    ]


def test_each_sort_is_descending_with_nulls_last(world):
    yesterday = AS_OF - timedelta(days=1)
    before, run = world.run(yesterday), world.run()
    a, b, c = world.security("AAA"), world.security("BBB"), world.security("CCC")
    world.snapshot(before, a, "50", as_of=yesterday)
    world.snapshot(before, b, "10", as_of=yesterday)
    world.snapshot(run, a, "60", pillars={
        "valuation": ("20", "1"), "quality": ("90", "1"), "momentum": ("40", "1"),
    })
    world.snapshot(run, b, "55", pillars={"valuation": ("80", "1"), "momentum": ("70", "1")})
    world.snapshot(run, c, "70", pillars={"quality": ("10", "1"), "momentum": ("30", "1")})

    assert symbols(ask(world)) == ["CCC", "AAA", "BBB"]
    assert symbols(ask(world, sort="delta")) == ["BBB", "AAA", "CCC"]
    assert symbols(ask(world, sort="V")) == ["BBB", "AAA", "CCC"]
    assert symbols(ask(world, sort="Q")) == ["AAA", "CCC", "BBB"]
    assert symbols(ask(world, sort="M")) == ["BBB", "AAA", "CCC"]


def test_ties_break_on_security_id_so_pages_neither_overlap_nor_skip(world):
    run = world.run()
    ids = [world.security(f"T{i}") for i in range(5)]
    for security_id in reversed(ids):
        world.snapshot(run, security_id, "50")

    pages = [symbols(ask(world, offset=offset, limit=2)) for offset in (0, 2, 4)]

    assert pages == [["T0", "T1"], ["T2", "T3"], ["T4"]]
    assert ask(world, offset=10, limit=2)["total"] == 5


def test_the_sector_is_the_one_held_on_the_night_not_today(world):
    run = world.run()
    moved = world.security("MOVD", "oil-gas-integrated")
    reclassified = AS_OF + timedelta(days=1)
    world.conn.execute(
        "update security_sector set valid_to = %s where security_id = %s", (reclassified, moved)
    )
    world.conn.execute(
        """insert into security_sector (security_id, sector_node_id, valid_from, source)
           values (%s, %s, %s, 'yfinance')""",
        (moved, world.nodes["software"], reclassified),
    )
    world.snapshot(run, moved, "50")
    world.snapshot(run, world.security("UTIL", "utilities"), "40")

    shown = ask(world)

    assert [row["sector"] for row in shown["rows"]] == [
        {"code": "energy", "name": "Energy"},
        {"code": "utilities", "name": "Utilities"},
    ]


def test_the_tiles_count_the_whole_night_whatever_the_filters(world):
    run = world.run()
    full, part, loose = world.security("FULL"), world.security("PART"), world.security("LOOS", None)
    world.security("GONE", active=False)
    world.security("UNSC")
    world.snapshot(run, full, "80", agreement=3)
    world.snapshot(run, part, "60", agreement=2, min_coverage="0.5")
    world.snapshot(run, loose, "40")
    world.metric(run, full, "ret_3m", "0.1", level=1)
    # A thin bucket ranked against the market: counted.
    world.metric(run, part, "ret_3m", "0.1", level=0)
    # Unclassified, so the market is where it belongs rather than a fallback: not counted.
    world.metric(run, loose, "ret_3m", "0.1", level=0)

    assert ask(world, sector="technology", partial="hide")["tiles"] == {
        "scored": 3, "active_now": 4, "partial": 1, "agreement_3": 1, "market_ranked_values": 1,
    }


def test_closes_are_the_newest_thirty_including_a_bar_restamped_since_the_run(world):
    run = world.run()
    security_id = world.security("BARS")
    world.snapshot(run, security_id, "50")
    for back in range(35):
        world.bar(security_id, AS_OF - timedelta(days=back), str(100 + back))
    world.bar(security_id, AS_OF + timedelta(days=1), "1")
    # Ingest's settling window re-stamps the last week nightly (F13); the chart must
    # not lose its right edge for it.
    world.conn.execute(
        "update price_daily set observed_at = %s where security_id = %s and trade_date = %s",
        (datetime(2026, 9, 1, tzinfo=timezone.utc), security_id, AS_OF),
    )

    closes = ask(world)["rows"][0]["closes"]

    assert len(closes) == 30
    assert closes[0] == [(AS_OF - timedelta(days=29)).isoformat(), "129.00"]
    assert closes[-1] == [AS_OF.isoformat(), "100.00"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_screen_read.py -n0 -q`
Expected: collection error, `ImportError: cannot import name 'read_screen' from 'screener.screen'`.

- [ ] **Step 3: Append the screen statements to `queries.py`**

```python
# The level-1 sector a security held on the night, walked up from its industry
# node as `screener.scoring.peers.resolve` does, in the scheme of the level-0
# peer group. Deliberately not the metric's `peer_group_id`, which can be the
# market (D9). Joined onto a security aliased `sec`.
SECTOR_AT_AS_OF: LiteralString = """
  left join security_sector ss
    on ss.security_id = sec.id
   and ss.valid_from <= %(as_of)s
   and (ss.valid_to is null or ss.valid_to > %(as_of)s)
  left join sector_node industry
    on industry.id = ss.sector_node_id
   and industry.scheme_id = (select scheme_id from peer_group where level = 0 order by id limit 1)
  -- `coalesce`, because a security classified straight at a level-1 node has no
  -- parent to walk up to and is already where it belongs.
  left join sector_node sector
    on sector.id = coalesce(industry.parent_id, industry.id)
"""

_RUN_ROWS: LiteralString = """
with run_rows as (
    select snap.security_id,
           sec.primary_symbol as symbol,
           sec.name,
           coalesce(sector.code, 'unclassified') as sector_code,
           coalesce(sector.name, 'Unclassified') as sector_name,
           snap.blended_score as score,
           prev.blended_score as previous_score,
           v.score as v_score, v.coverage as v_coverage,
           q.score as q_score, q.coverage as q_coverage,
           m.score as m_score, m.coverage as m_coverage,
           snap.pillar_agreement,
           snap.min_coverage
      from snapshot_daily snap
      join security sec on sec.id = snap.security_id
""" + SECTOR_AT_AS_OF + """
      left join snapshot_daily prev
        on prev.scoring_run_id = %(previous_run)s::bigint
       and prev.as_of = %(previous_as_of)s::date
       and prev.security_id = snap.security_id
      left join pillar_score_daily v
        on v.scoring_run_id = snap.scoring_run_id and v.as_of = snap.as_of
       and v.security_id = snap.security_id
       and v.pillar_id = (select id from pillar where code = 'valuation')
      left join pillar_score_daily q
        on q.scoring_run_id = snap.scoring_run_id and q.as_of = snap.as_of
       and q.security_id = snap.security_id
       and q.pillar_id = (select id from pillar where code = 'quality')
      left join pillar_score_daily m
        on m.scoring_run_id = snap.scoring_run_id and m.as_of = snap.as_of
       and m.security_id = snap.security_id
       and m.pillar_id = (select id from pillar where code = 'momentum')
     where snap.scoring_run_id = %(run)s
       and snap.as_of = %(as_of)s
)
"""

_FILTERED: LiteralString = """
, screen as (
    select *
      from run_rows
     where (%(sector)s::text is null or sector_code = %(sector)s::text)
       and (%(agree)s::int is null or pillar_agreement >= %(agree)s::int)
       and (%(partial)s::text is null
            or (%(partial)s::text = 'only' and min_coverage < 1)
            or (%(partial)s::text = 'hide' and min_coverage >= 1))
)
"""

SCREEN_COUNT: LiteralString = _RUN_ROWS + _FILTERED + """
select count(*) from screen
"""

# Every order ends with `security_id`, so pages neither overlap nor skip on ties (D8).
ORDER_BY: dict[str, LiteralString] = {
    "score": " order by score desc nulls last, security_id",
    "delta": " order by score - previous_score desc nulls last, security_id",
    "V": " order by v_score desc nulls last, security_id",
    "Q": " order by q_score desc nulls last, security_id",
    "M": " order by m_score desc nulls last, security_id",
}

_PAGE: LiteralString = """
select security_id, symbol, name, sector_code, sector_name, score, previous_score,
       v_score, v_coverage, q_score, q_coverage, m_score, m_coverage,
       pillar_agreement, min_coverage
  from screen
"""


def screen_page(sort: str) -> LiteralString:
    """One page, in one of `ORDER_BY`'s fixed orders. `sort` is validated by `params`."""
    return _RUN_ROWS + _FILTERED + _PAGE + ORDER_BY[sort] + " offset %(offset)s limit %(limit)s"


SECTORS: LiteralString = _RUN_ROWS + """
select sector_code, sector_name
  from run_rows
 group by sector_code, sector_name
 order by sector_code = 'unclassified', sector_name
"""

# Unfiltered, so the tiles describe the night rather than the current view. A
# market-ranked value counts only when the security had a sector that night: an
# unclassified security ranks against the market because that is where it
# belongs, not because its bucket was thin (D9).
TILES: LiteralString = """
select (select count(*) from snapshot_daily
         where scoring_run_id = %(run)s and as_of = %(as_of)s),
       (select count(*) from security where is_active),
       (select count(*) from snapshot_daily
         where scoring_run_id = %(run)s and as_of = %(as_of)s and min_coverage < 1),
       (select count(*) from snapshot_daily
         where scoring_run_id = %(run)s and as_of = %(as_of)s and pillar_agreement >= 3),
       (select count(*) from metric_daily md
         where md.scoring_run_id = %(run)s
           and md.as_of = %(as_of)s
           and md.fallback_level = 0
           and exists (select 1 from security_sector ss
                        where ss.security_id = md.security_id
                          and ss.valid_from <= %(as_of)s
                          and (ss.valid_to is null or ss.valid_to > %(as_of)s)))
"""

# Display closes carry no `observed_at` bound (D11). `since` only lets the read
# prune to one or two price partitions.
CLOSES: LiteralString = """
select security_id, trade_date, close, observed_at
  from (select security_id, trade_date, close, observed_at,
               row_number() over (partition by security_id order by trade_date desc) as newest
          from price_daily
         where security_id = any(%(ids)s)
           and trade_date > %(since)s
           and trade_date <= %(as_of)s) bars
 where newest <= %(count)s
 order by security_id, trade_date
"""

ACTIONS: LiteralString = """
select security_id, effective_date, action_type, ratio, amount
  from corporate_action
 where security_id = any(%(ids)s)
   and effective_date > %(since)s
   and effective_date <= %(as_of)s
 order by security_id, effective_date
"""
```

- [ ] **Step 4: Append the screen read to `read.py`**

Extend `read.py`'s imports so they read:

```python
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any, LiteralString, TypeVar

import psycopg

from screener.scoring import LOGIC_DESCRIPTION
from screener.screen import queries, shape
from screener.screen.params import ScreenParams
from screener.screen.rows import (
    ActionRow,
    BarRow,
    PreviousRunRow,
    RunRow,
    ScreenRow,
    SectorRow,
    TilesRow,
    parse_all,
)
```

After the imports and `R = TypeVar("R")`, add the constants:

```python
SPARKLINE_CLOSES = 30
CHART_CLOSES = 60
# Sixty trading days is about 87 calendar days once weekends and holidays are
# counted. The bound exists so the read prunes partitions, not to cut the series.
CLOSE_LOOKBACK_DAYS = 120
```

Append the functions:

```python
def _closes(
    conn: psycopg.Connection, security_ids: Sequence[int], as_of: date, count: int
) -> dict[int, list[list[str]]]:
    """Adjusted display closes per security, from the bars stored now (D11).

    Unlike every scoring read there is no `observed_at` bound: ingest re-stamps
    the last week of bars nightly (F13), so bounding by the run's cutoff would
    empty the right edge of every chart until the next night was scored.
    """
    if not security_ids:
        return {}
    bind = {
        "ids": list(security_ids),
        "as_of": as_of,
        "since": as_of - timedelta(days=CLOSE_LOOKBACK_DAYS),
        "count": count,
    }
    bars: dict[int, list[BarRow]] = {}
    for bar in _all(conn, queries.CLOSES, bind, BarRow):
        bars.setdefault(bar.security_id, []).append(bar)
    actions: dict[int, list[ActionRow]] = {}
    for action in _all(conn, queries.ACTIONS, bind, ActionRow):
        actions.setdefault(action.security_id, []).append(action)
    return {
        security_id: shape.closes(bars.get(security_id, []), actions.get(security_id, []), count)
        for security_id in security_ids
    }


def read_screen(conn: psycopg.Connection, params: ScreenParams) -> dict[str, Any]:
    """`/api/screen`: one page of the served night (D9)."""
    resolved = resolve_run(conn, params.run)
    if resolved is None:
        return shape.awaiting()
    run, latest = resolved
    previous = previous_run(conn, run)
    bind = {
        "run": run.id,
        "as_of": run.as_of,
        "previous_run": None if previous is None else previous.id,
        "previous_as_of": None if previous is None else previous.as_of,
        "sector": params.sector,
        "agree": params.agree,
        "partial": params.partial,
        "offset": params.offset,
        "limit": params.limit,
    }
    tiles = _one(conn, queries.TILES, bind, TilesRow)
    # A select of scalar subqueries always returns its one row.
    assert tiles is not None
    counted = conn.execute(queries.SCREEN_COUNT, bind).fetchone()
    rows = _all(conn, queries.screen_page(params.sort), bind, ScreenRow)
    return shape.screen_payload(
        run=run,
        latest=latest,
        previous=previous,
        tiles=tiles,
        sectors=_all(conn, queries.SECTORS, bind, SectorRow),
        total=0 if counted is None else counted[0],
        rows=rows,
        closes_by_security=_closes(
            conn, [row.security_id for row in rows], run.as_of, SPARKLINE_CLOSES
        ),
    )
```

The package `__init__` imports `read` before `shape`. That is safe: `from screener.screen import queries, shape` inside `read` loads those submodules on demand.

- [ ] **Step 5: Export from the package**

In `src/screener/screen/__init__.py`, change the `read` import to:

```python
from screener.screen.read import (
    CHART_CLOSES,
    CLOSE_LOOKBACK_DAYS,
    SPARKLINE_CLOSES,
    RunChanged,
    previous_run,
    read_screen,
    resolve_run,
)
```

Add to `__all__`:
- `"CHART_CLOSES"` and `"CLOSE_LOOKBACK_DAYS"` after `"ALL_CODES"`.
- `"SPARKLINE_CLOSES"` before `"SORTS"`.
- `"read_screen"` after `"price"`.

- [ ] **Step 6: Run the tests to verify they pass, and typecheck**

```bash
.venv/bin/python -m pytest tests/test_screen_read.py tests/test_screen_shape.py -n0 -q
.venv/bin/pyright src/screener/screen tests/test_screen_read.py 2>&1 | tail -1
```

Expected: all passed; `0 errors`.

- [ ] **Step 7: Commit**

```bash
git add src/screener/screen/queries.py src/screener/screen/read.py src/screener/screen/__init__.py tests/test_screen_read.py
git commit -m "Read a screen page with its tiles, sectors, filters, stable sorts and adjusted closes"
```

---

### Task 6: Reproduction, and the six statuses

**Files:**
- Create: `src/screener/screen/explain.py`
- Modify: `src/screener/screen/queries.py` (append `REFRESHED`)
- Modify: `src/screener/screen/__init__.py`
- Test: `tests/test_screen_explain.py` (pure)
- Test: `tests/test_screen_security.py` (database; Task 7 appends)

**Interfaces:**
- Consumes:
  - `RunRow` and `resolve_run` (Tasks 1 and 4).
  - From `screener.scoring`: `Absent`, `Action`, `Item`, `adjusted_closes`, `explain_momentum`, `explain_ratios`, `index_facts_explained`, `read_bars`, `read_actions`, `read_currencies`, `months_before`, `BAR_WINDOW_MONTHS`, `resolve`, `run_scoring`.
  - From `screener.ingest`: `read_facts`, `Fact`, `insert_facts`, `latest_values`, `metric_ids`.
- Produces:
  - Status constants `OK`, `MISMATCH`, `ABSENT`, `UNEXPECTED`, `REFRESHED`, `UNCHECKED`, and input constants `PRICE = "price"` and `FUNDAMENTALS = "fundamentals"`.
  - `DEPENDS: dict[str, frozenset[str]]`, keyed by pillar code.
  - `Reproduction(visible_through: datetime, values: Mapping[str, Decimal | Absent] | None, refreshed: frozenset[str])`, a frozen dataclass.
  - `Check(status: str, reproduced: Decimal | None, reason: str | None)`, a frozen dataclass.
  - `view_offset(run: RunRow) -> timedelta`.
  - `reproduce_values(bars, actions, items, *, currency: str, industry: str | None, as_of: date) -> dict[str, Decimal | Absent]`.
  - `reproduce(conn, *, security_id: int, run: RunRow, industry: str | None) -> Reproduction`, which never raises.
  - `check(*, pillar: str, code: str, stored: Decimal | None, reproduction: Reproduction) -> Check`.
  - In `tests/test_screen_security.py`: the `scored` fixture. It returns `dict[str, int]` of security ids by symbol (`S00`–`S19`, `BANK`, `EURO`, `NOBR`) plus `"run"`.

- [ ] **Step 1: Write the failing pure tests**

Create `tests/test_screen_explain.py`:

```python
"""Each metric's one status, from what was stored and what reproduces (ui-swap D13, D14)."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from screener.scoring import Absent
from screener.screen import Check, Reproduction, RunRow, check, view_offset

AS_OF = date(2026, 3, 2)
RUN = RunRow(
    7, AS_OF, datetime(2026, 3, 2, 23, tzinfo=timezone.utc), None, "abc1234", "9f3a", "v2", 3,
    108000, "v2 momentum", False,
)


def _reproduced(**values: Decimal | Absent) -> Reproduction:
    return Reproduction(RUN.started_at, values, frozenset())


@pytest.mark.parametrize(
    "stored,reproduced,status",
    [
        (Decimal("0.0741"), Decimal("0.0741"), "ok"),
        (Decimal("0.0741"), Decimal("0.07410"), "ok"),
        (Decimal("0.0741"), Decimal("0.0742"), "mismatch"),
        (Decimal("0.0741"), Absent("equity ≤ 0"), "mismatch"),
        (None, Absent("equity ≤ 0"), "absent"),
        (None, Decimal("0.0741"), "unexpected"),
    ],
)
def test_a_metric_gets_one_status_from_what_was_stored_and_what_reproduces(stored, reproduced, status):
    got = check(
        pillar="valuation", code="earnings_yield", stored=stored,
        reproduction=_reproduced(earnings_yield=reproduced),
    )

    assert got.status == status


def test_an_absence_carries_its_reason_and_a_value_carries_itself():
    assert check(
        pillar="quality", code="roic", stored=None,
        reproduction=_reproduced(roic=Absent("invested capital ≤ 0")),
    ) == Check("absent", None, "invested capital ≤ 0")
    assert check(
        pillar="quality", code="roic", stored=Decimal("1"),
        reproduction=_reproduced(roic=Decimal("2")),
    ) == Check("mismatch", Decimal("2"), None)


def test_a_stored_metric_that_no_longer_applies_is_a_mismatch_that_says_so():
    got = check(pillar="valuation", code="book_yield", stored=Decimal("0.5"), reproduction=_reproduced())

    assert got.status == "mismatch"
    assert got.reason is not None and "no longer applicable" in got.reason


@pytest.mark.parametrize(
    "refreshed,pillar,status",
    [
        ("price", "momentum", "refreshed"),
        ("price", "valuation", "refreshed"),
        ("price", "quality", "ok"),
        ("fundamentals", "momentum", "ok"),
        ("fundamentals", "valuation", "refreshed"),
        ("fundamentals", "quality", "refreshed"),
    ],
)
def test_refreshed_inputs_skip_only_the_metrics_that_read_them(refreshed, pillar, status):
    reproduction = Reproduction(RUN.started_at, {"m": Decimal(1)}, frozenset({refreshed}))

    assert check(pillar=pillar, code="m", stored=Decimal(1), reproduction=reproduction).status == status


def test_a_reproduction_that_could_not_run_leaves_every_metric_unchecked():
    failed = Reproduction(RUN.started_at, None, frozenset({"price"}))

    assert check(pillar="quality", code="roic", stored=Decimal(1), reproduction=failed) == Check(
        "unchecked", None, None
    )


def test_the_view_ends_at_the_run_s_start_when_that_precedes_its_cutoff():
    assert view_offset(RUN) == timedelta(hours=23)
    started_late = replace(RUN, started_at=datetime(2026, 3, 9, tzinfo=timezone.utc))
    assert view_offset(started_late) == timedelta(hours=30)
```

- [ ] **Step 2: Write the failing database tests**

Create `tests/test_screen_security.py`:

```python
"""One security against a really scored night (ui-swap spec D10, D13, D14).

Scoring runs for real here. A reproduction that drifted from `score()` fails
`test_every_stored_metric_reproduces`, before anyone sees a panel full of
mismatches (plan amendment P5).
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from screener.ingest import Fact, insert_facts, latest_values, metric_ids
from screener.scoring import Absent, resolve, run_scoring
from screener.screen import Reproduction, reproduce, resolve_run

AS_OF = date(2026, 3, 2)
SEEN = datetime(2026, 1, 1, tzinfo=timezone.utc)
# See `test_scoring_run.py`: inside `price_daily`'s 2025 partition and the bar window.
OFFSETS = (380, 200, 100, 30, 0)
QUARTERS = (date(2025, 12, 31), date(2025, 9, 30), date(2025, 6, 30), date(2025, 3, 31))
FLOWS = {
    "net_income": "25",
    "revenue": "250",
    "gross_profit": "100",
    "ebit": "40",
    "depreciation_amortisation": "10",
    "operating_cash_flow": "50",
    "capital_expenditure": "-20",
    "interest_expense": "5",
    "tax_provision": "8",
    "pretax_income": "32",
}
BALANCES = {
    "stockholders_equity": "800",
    "total_debt": "400",
    "cash_and_equivalents": "200",
    "shares_outstanding": "100",
}


def _facts(scale: str, currency: str = "USD") -> list[Fact]:
    """Four quarters of every flow and one balance sheet, scaled per company; share
    counts unscaled, so market cap moves with the close alone."""
    factor = Decimal(scale)
    out = [
        Fact(code, end, "Q", Decimal(value) * factor, currency)
        for code, value in FLOWS.items()
        for end in QUARTERS
    ]
    out += [
        Fact(code, QUARTERS[0], "Q",
             Decimal(value) * (Decimal(1) if code == "shares_outstanding" else factor), currency)
        for code, value in BALANCES.items()
    ]
    return out


@pytest.fixture
def scored(fresh_db, an_observation) -> dict[str, int]:
    """Twenty software companies, a regional bank, a company reporting in euros while
    it trades in dollars, and one with neither bars nor facts, scored for AS_OF."""
    scheme = fresh_db.execute(
        "insert into sector_scheme (code, name) values ('yfinance', 'yfinance') returning id"
    ).fetchone()[0]
    fresh_db.execute(
        "insert into peer_group (scheme_id, sector_node_id, level, code)"
        " values (%s, null, 0, 'market')",
        (scheme,),
    )
    industries: dict[str, int] = {}
    for sector_code, sector_name, industry_code, industry_name in (
        ("technology", "Technology", "software", "Software"),
        ("financial-services", "Financial Services", "banks-regional", "Banks - Regional"),
    ):
        sector = fresh_db.execute(
            "insert into sector_node (scheme_id, level, code, name)"
            " values (%s, 1, %s, %s) returning id",
            (scheme, sector_code, sector_name),
        ).fetchone()[0]
        fresh_db.execute(
            "insert into peer_group (scheme_id, sector_node_id, level, code)"
            " values (%s, %s, 1, %s)",
            (scheme, sector, sector_code),
        )
        industries[industry_code] = fresh_db.execute(
            "insert into sector_node (scheme_id, parent_id, level, code, name)"
            " values (%s, %s, 2, %s, %s) returning id",
            (scheme, sector, industry_code, industry_name),
        ).fetchone()[0]

    ids: dict[str, int] = {}

    def company(symbol: str, industry: str, bump: int, facts: list[Fact], *, bars: bool = True) -> None:
        security = fresh_db.execute(
            """insert into security (name, mic, currency, country, primary_symbol, first_seen)
               values (%s, 'XNAS', 'USD', 'US', %s, '2020-01-01') returning id""",
            (f"{symbol} Inc", symbol),
        ).fetchone()[0]
        fresh_db.execute(
            """insert into security_symbol (security_id, symbol, mic, valid_from, source)
               values (%s, %s, 'XNAS', '2020-01-01', 'test')""",
            (security, symbol),
        )
        fresh_db.execute(
            """insert into security_sector (security_id, sector_node_id, valid_from, source)
               values (%s, %s, '2020-01-01', 'yfinance')""",
            (security, industries[industry]),
        )
        observation = an_observation(security)
        if bars:
            for offset in OFFSETS:
                close = Decimal(100 + bump) if offset == 0 else Decimal(100)
                fresh_db.execute(
                    """insert into price_daily
                       (security_id, trade_date, open, high, low, close, volume,
                        observed_at, ingest_observation_id)
                       values (%s, %s, %s, %s, %s, %s, 1, %s, %s)""",
                    (security, AS_OF - timedelta(days=offset), close, close, close, close,
                     SEEN, observation),
                )
        if facts:
            with fresh_db.cursor() as cur:
                insert_facts(
                    cur, security, observation, SEEN, facts,
                    latest_values(cur, security), metric_ids(cur),
                )
        ids[symbol] = security

    for i in range(20):
        company(f"S{i:02d}", "software", i, _facts(str(Decimal(1) + Decimal(i) / 10)))
    company("BANK", "banks-regional", 3, _facts("1.2"))
    company("EURO", "software", 8, _facts("1", currency="EUR"))
    company("NOBR", "software", 0, [], bars=False)

    ids["run"] = run_scoring(fresh_db, as_of=AS_OF).run_id
    return ids


def _reproduce(conn: Any, security_id: int, run_id: int) -> Reproduction:
    resolved = resolve_run(conn, run_id)
    assert resolved is not None
    run, _ = resolved
    industry = resolve(conn, [security_id], as_of=run.as_of)[security_id].industry
    return reproduce(conn, security_id=security_id, run=run, industry=industry)


def _stored(conn: Any, security_id: int) -> dict[str, Decimal]:
    return dict(conn.execute(
        """select m.code, md.raw_value
             from metric_daily md join metric m on m.id = md.metric_id
            where md.security_id = %s""",
        (security_id,),
    ).fetchall())


def test_every_stored_metric_reproduces(fresh_db, scored):
    compared = 0
    for symbol in [*(f"S{i:02d}" for i in range(20)), "BANK", "EURO"]:
        reproduction = _reproduce(fresh_db, scored[symbol], scored["run"])
        stored = _stored(fresh_db, scored[symbol])
        assert reproduction.values is not None, symbol
        assert reproduction.refreshed == frozenset(), symbol

        for code, raw in stored.items():
            assert reproduction.values[code] == raw, (symbol, code)
            compared += 1
        produced = {
            code for code, value in reproduction.values.items() if not isinstance(value, Absent)
        }
        assert produced == set(stored), symbol

    assert compared > 200


def test_the_view_stops_at_the_cutoff_for_a_run_that_started_after_it(fresh_db, scored):
    reproduction = _reproduce(fresh_db, scored["S00"], scored["run"])

    assert reproduction.visible_through == datetime(2026, 3, 3, 6, tzinfo=timezone.utc)


def test_a_bar_restamped_since_the_run_marks_prices_refreshed(fresh_db, scored):
    fresh_db.execute(
        """update price_daily set observed_at = now() + interval '1 hour'
            where security_id = %s and trade_date = %s""",
        (scored["S03"], AS_OF),
    )

    assert _reproduce(fresh_db, scored["S03"], scored["run"]).refreshed == {"price"}


def test_a_fact_restamped_since_the_run_marks_fundamentals_refreshed(fresh_db, scored):
    fresh_db.execute(
        """update fundamental_fact set observed_at = now() + interval '1 hour'
            where id = (select min(id) from fundamental_fact where security_id = %s)""",
        (scored["S03"],),
    )

    assert _reproduce(fresh_db, scored["S03"], scored["run"]).refreshed == {"fundamentals"}


def test_a_failure_inside_reproduction_is_reported_rather_than_raised(fresh_db, scored, monkeypatch, caplog):
    def unreadable(*args: object, **kwargs: object) -> None:
        raise RuntimeError("facts unreadable")

    monkeypatch.setattr("screener.screen.explain.read_facts", unreadable)

    reproduction = _reproduce(fresh_db, scored["S05"], scored["run"])

    assert reproduction.values is None
    assert "could not reproduce security" in caplog.text
```

- [ ] **Step 3: Run both files to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_screen_explain.py tests/test_screen_security.py -n0 -q`
Expected: collection errors, `ImportError: cannot import name 'Check' from 'screener.screen'` and `… 'Reproduction' …`.

- [ ] **Step 4: Append `REFRESHED` to `queries.py`**

```python
# D13: whether a security's inputs changed after its run started. Bars are
# checked only inside the momentum window, which is all a run reads of them.
REFRESHED: LiteralString = """
select exists (select 1 from price_daily
                where security_id = %(id)s
                  and trade_date > %(start)s
                  and trade_date <= %(as_of)s
                  and observed_at > %(started_at)s),
       exists (select 1 from fundamental_fact
                where security_id = %(id)s
                  and observed_at > %(started_at)s)
"""
```

- [ ] **Step 5: Write `explain.py`**

Create `src/screener/screen/explain.py`:

```python
"""Reproducing one security's metrics as its run saw them (ui-swap spec D13, D14).

`reproduce` reads what the run could see and calls scoring's explaining forms;
`check` compares the result with what the run stored and gives each metric one
status. Percentiles are not re-derived -- they depend on every peer -- so `ok`
means the raw value reproduces, and no more.

**The run's view is `least(cutoff, started_at)`.** The nightly run starts about
23:00 on its date and its cutoff is 06:00 the next morning, so a manual ingest in
between writes rows the run never saw but a cutoff-bounded read would (F14).
Scoring's reads take a cutoff *offset*, so the view is expressed as the smaller
offset rather than as a second set of reads.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import psycopg

from screener.ingest import read_facts
from screener.scoring import (
    BAR_WINDOW_MONTHS,
    Absent,
    Action,
    Item,
    adjusted_closes,
    explain_momentum,
    explain_ratios,
    index_facts_explained,
    months_before,
    read_actions,
    read_bars,
    read_currencies,
)
from screener.screen import queries
from screener.screen.rows import RunRow

logger = logging.getLogger(__name__)

OK = "ok"
MISMATCH = "mismatch"
ABSENT = "absent"
UNEXPECTED = "unexpected"
REFRESHED = "refreshed"
UNCHECKED = "unchecked"

PRICE = "price"
FUNDAMENTALS = "fundamentals"

# D13: momentum reads bars; every Valuation ratio divides by a market cap built
# from a close as well as reading facts; Quality reads facts alone.
DEPENDS: dict[str, frozenset[str]] = {
    "momentum": frozenset({PRICE}),
    "valuation": frozenset({PRICE, FUNDAMENTALS}),
    "quality": frozenset({FUNDAMENTALS}),
}


@dataclass(frozen=True)
class Reproduction:
    visible_through: datetime
    # None when reproduction raised, which leaves every metric `unchecked` (spec §7).
    values: Mapping[str, Decimal | Absent] | None
    refreshed: frozenset[str]


@dataclass(frozen=True)
class Check:
    status: str
    reproduced: Decimal | None
    reason: str | None


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


def view_offset(run: RunRow) -> timedelta:
    """The cutoff offset under which scoring's reads see exactly what `run` could."""
    return min(
        timedelta(seconds=run.cutoff_offset_seconds), run.started_at - _midnight(run.as_of)
    )


def reproduce_values(
    bars: Sequence[tuple[date, Decimal, datetime]],
    actions: Sequence[Action],
    items: Sequence[Item],
    *,
    currency: str,
    industry: str | None,
    as_of: date,
) -> dict[str, Decimal | Absent]:
    """Every metric scoring would produce for one security, as a value or a reason.

    This repeats the per-security half of `screener.scoring.run.score` over the
    explaining forms rather than sharing it, because piece (a) was the one change
    to scoring the spec allows (§12). `test_every_stored_metric_reproduces` runs
    real scoring and requires every stored value to come back equal from here,
    which is what fails if the two drift (plan amendment P5).
    """
    out: dict[str, Decimal | Absent] = dict(
        explain_momentum(adjusted_closes(bars, actions), as_of)
    )
    held, dropped = index_facts_explained(items, currency=currency)
    ratios = explain_ratios(
        held,
        industry=industry,
        # The raw close of the latest visible bar, as `score` passes it.
        close=bars[-1][1] if bars else None,
        close_date=bars[-1][0] if bars else None,
        split_dates=[a.effective_date for a in actions if a.action_type == "split"],
        as_of=as_of,
        currency=currency,
        dropped=dropped,
    )
    for code, ratio in ratios.items():
        out[code] = ratio if isinstance(ratio, Absent) else ratio.value
    return out


def reproduce(
    conn: psycopg.Connection, *, security_id: int, run: RunRow, industry: str | None
) -> Reproduction:
    """Recompute one security under its run's view, and say which inputs changed since.

    Never raises: a failure is logged and returned as a reproduction that could
    not run, so the stored metrics are still shown. Call it after every other
    read on `conn`, because a failed statement leaves the transaction unusable.
    """
    offset = view_offset(run)
    visible_through = _midnight(run.as_of) + offset
    ids = [security_id]
    try:
        bars = read_bars(conn, ids, as_of=run.as_of, cutoff_offset=offset).get(security_id, [])
        actions = read_actions(conn, ids, as_of=run.as_of, cutoff_offset=offset).get(security_id, [])
        facts = read_facts(conn, ids, as_of=run.as_of, cutoff_offset=offset).get(security_id, [])
        values = reproduce_values(
            bars,
            actions,
            [Item(f.metric_code, f.period_end, f.period_type, f.value, f.currency) for f in facts],
            # Today's currency, not the run's: scoring does not read it point in
            # time either, and a change since is a named cause of `unexpected` (F15).
            currency=read_currencies(conn, ids)[security_id],
            industry=industry,
            as_of=run.as_of,
        )
        changed = conn.execute(
            queries.REFRESHED,
            {
                "id": security_id,
                "start": months_before(run.as_of, BAR_WINDOW_MONTHS),
                "as_of": run.as_of,
                "started_at": run.started_at,
            },
        ).fetchone()
    except Exception:
        logger.exception(
            "could not reproduce security %d on scoring run %d", security_id, run.id
        )
        return Reproduction(visible_through, None, frozenset())
    prices, fundamentals = changed if changed is not None else (False, False)
    refreshed = frozenset(
        name for name, flag in ((PRICE, prices), (FUNDAMENTALS, fundamentals)) if flag
    )
    return Reproduction(visible_through, values, refreshed)


def check(
    *, pillar: str, code: str, stored: Decimal | None, reproduction: Reproduction
) -> Check:
    """One metric's status: exactly one of D14's six."""
    if reproduction.values is None:
        return Check(UNCHECKED, None, None)
    if DEPENDS[pillar] & reproduction.refreshed:
        return Check(REFRESHED, None, None)
    value = reproduction.values.get(code)
    if value is None:
        # Stored by the run, but today's rules for this security's industry do
        # not produce it: it changed class since (plan amendment P4).
        value = Absent("no longer applicable to this security's industry")
    if isinstance(value, Absent):
        return Check(ABSENT if stored is None else MISMATCH, None, value.reason)
    if stored is None:
        return Check(UNEXPECTED, value, None)
    return Check(OK if value == stored else MISMATCH, value, None)
```

- [ ] **Step 6: Export from the package**

In `src/screener/screen/__init__.py`, add this import directly after the docstring, before `params`:

```python
from screener.screen.explain import (
    ABSENT,
    DEPENDS,
    FUNDAMENTALS,
    MISMATCH,
    OK,
    PRICE,
    REFRESHED,
    UNCHECKED,
    UNEXPECTED,
    Check,
    Reproduction,
    check,
    reproduce,
    reproduce_values,
    view_offset,
)
```

Add to `__all__`:
- SCREAMING_CASE, alphabetically: `ABSENT`, `DEPENDS`, `FUNDAMENTALS`, `MISMATCH`, `OK`, `PRICE`, `REFRESHED`, `UNCHECKED`, `UNEXPECTED`.
- CamelCase: `Check` and `Reproduction`.
- snake_case: `check`, `reproduce`, `reproduce_values`, `view_offset`.

- [ ] **Step 7: Run the tests to verify they pass, and typecheck**

```bash
.venv/bin/python -m pytest tests/test_screen_explain.py tests/test_screen_security.py -n0 -q
.venv/bin/pyright src/screener/screen tests/test_screen_explain.py tests/test_screen_security.py 2>&1 | tail -1
```

Expected: all passed; `0 errors`. If `test_every_stored_metric_reproduces` fails, `reproduce_values` has drifted from `run.score`. Compare it with `run.py` line by line; do not change the test.

- [ ] **Step 8: Commit**

```bash
git add src/screener/screen/explain.py src/screener/screen/queries.py src/screener/screen/__init__.py tests/test_screen_explain.py tests/test_screen_security.py
git commit -m "Reproduce a security under its run's own view and give each metric one status"
```

---

### Task 7: The security detail

**Files:**
- Modify: `src/screener/screen/queries.py` (append)
- Modify: `src/screener/screen/shape.py` (append)
- Modify: `src/screener/screen/read.py` (append)
- Modify: `src/screener/screen/__init__.py`
- Test: `tests/test_screen_security.py` (append)

**Interfaces:**
- Consumes:
  - `SecurityParams` and `security_params` (Task 2).
  - `_closes` and `CHART_CLOSES` (Task 5).
  - `reproduce`, `check`, `Check` and `Reproduction` (Task 6).
  - From `screener.scoring`: `resolve`, `applicable`, `CODES`, `VALUATION`, `QUALITY`, `MOMENTUM`.
  - `screener.provenance.git_sha`.
- Produces:
  - In `queries`: `SYMBOL_MATCH`, `CLASSIFICATION`, `SNAPSHOT`, `PILLARS`, `STORED_METRICS` and `METRIC_INFO`.
  - In `shape`: `listed(expected: Mapping[str, Sequence[str]], stored: Collection[str], pillar_of: Mapping[str, str]) -> dict[str, list[str]]`.
  - `shape.unscored_payload(*, run, latest, match, where, closes) -> dict`.
  - `shape.security_payload(*, run, latest, match, where, snapshot, pillars, metrics, info, expected, checks, reproduction, running_build, closes) -> dict`.
  - In `read`: `class UnknownSymbol(LookupError)`, with `.symbol`.
  - `class AmbiguousSymbol(LookupError)`, with `.symbol` and `.exchanges: tuple[str, ...]`.
  - `read_security(conn: psycopg.Connection, params: SecurityParams) -> dict[str, Any]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_screen_security.py`:
- Add `CODES` to its `from screener.scoring import` line.
- Add `AmbiguousSymbol, RunChanged, UnknownSymbol, read_security, security_params` to its `from screener.screen import` line.

```python
# -- the detail endpoint (D10, D14) ------------------------------------------


def detail(conn: Any, symbol: str, run: int | None = None) -> dict[str, Any]:
    query = {"symbol": [symbol]} | ({} if run is None else {"run": [str(run)]})
    return read_security(conn, security_params(query))


def metrics(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {metric["code"]: metric for pillar in payload["pillars"] for metric in pillar["metrics"]}


def statuses(payload: dict[str, Any]) -> dict[str, str]:
    return {code: metric["status"] for code, metric in metrics(payload).items()}


def test_a_standard_company_and_a_bank_expect_different_metrics(fresh_db, scored):
    standard, bank = detail(fresh_db, "S05"), detail(fresh_db, "BANK")

    assert [(p["key"], p["present"], p["expected"]) for p in standard["pillars"]] == [
        ("V", 3, 3), ("Q", 4, 4), ("M", 4, 4),
    ]
    assert [(p["key"], p["present"], p["expected"]) for p in bank["pillars"]] == [
        ("V", 2, 2), ("Q", 1, 1), ("M", 4, 4),
    ]
    assert set(metrics(bank)) == {"earnings_yield", "book_yield", "roe", *CODES}
    assert standard["sector"] == {"code": "technology", "name": "Technology"}
    assert standard["industry"] == {"code": "software", "name": "Software"}
    assert metrics(standard)["debt_to_equity"]["higher_is_better"] is False
    assert metrics(standard)["debt_to_equity"]["unit"] == "multiple"


def test_every_metric_of_an_untouched_night_is_ok_or_absent(fresh_db, scored):
    for symbol in ("S00", "S19", "BANK", "EURO"):
        assert set(statuses(detail(fresh_db, symbol)).values()) <= {"ok", "absent"}, symbol
    assert set(statuses(detail(fresh_db, "S00")).values()) == {"ok"}


def test_an_ok_metric_carries_both_values_as_exact_strings(fresh_db, scored):
    got = metrics(detail(fresh_db, "S07"))["ret_12m"]

    assert got["status"] == "ok"
    assert got["stored"]["raw"] == got["reproduced"] == "0.07"
    assert got["stored"]["peer_group"] == "Technology"
    assert got["stored"]["market_ranked"] is False


def test_an_absent_metric_says_why(fresh_db, scored):
    got = metrics(detail(fresh_db, "EURO"))["earnings_yield"]

    assert (got["status"], got["stored"], got["reproduced"]) == ("absent", None, None)
    assert "EUR" in got["reason"]


def test_a_stored_value_altered_since_the_run_is_a_mismatch(fresh_db, scored):
    fresh_db.execute(
        """update metric_daily set raw_value = raw_value + 1
            where security_id = %s
              and metric_id = (select id from metric where code = 'ret_12m')""",
        (scored["S07"],),
    )

    got = metrics(detail(fresh_db, "S07"))["ret_12m"]

    assert (got["status"], got["stored"]["raw"], got["reproduced"]) == ("mismatch", "1.07", "0.07")


def test_a_currency_corrected_since_the_run_makes_its_ratios_reproduce_unexpectedly(fresh_db, scored):
    # F15: scoring reads today's currency, so facts the run dropped as foreign now count.
    fresh_db.execute("update security set currency = 'EUR' where id = %s", (scored["EURO"],))

    got = statuses(detail(fresh_db, "EURO"))

    assert got["earnings_yield"] == got["roic"] == "unexpected"
    assert got["ret_12m"] == "ok"


def test_a_bar_restamped_since_the_run_leaves_quality_compared(fresh_db, scored):
    fresh_db.execute(
        """update price_daily set observed_at = now() + interval '1 hour'
            where security_id = %s and trade_date = %s""",
        (scored["S03"], AS_OF),
    )

    shown = detail(fresh_db, "S03")
    got = statuses(shown)

    assert {got[code] for code in CODES} == {"refreshed"}
    assert {got[code] for code in ("earnings_yield", "ebitda_ev", "fcf_yield")} == {"refreshed"}
    assert {got[code] for code in ("roic", "gross_margin", "debt_to_equity", "interest_cover")} == {"ok"}
    assert shown["reproduction"]["refreshed_inputs"] == ["price"]


def test_a_fact_restamped_since_the_run_leaves_momentum_compared(fresh_db, scored):
    fresh_db.execute(
        """update fundamental_fact set observed_at = now() + interval '1 hour'
            where id = (select min(id) from fundamental_fact where security_id = %s)""",
        (scored["S03"],),
    )

    got = statuses(detail(fresh_db, "S03"))

    assert {got[code] for code in CODES} == {"ok"}
    assert {status for code, status in got.items() if code not in CODES} == {"refreshed"}


def test_a_reproduction_that_raises_still_returns_the_stored_metrics(fresh_db, scored, monkeypatch):
    def unreadable(*args: object, **kwargs: object) -> None:
        raise RuntimeError("facts unreadable")

    monkeypatch.setattr("screener.screen.explain.read_facts", unreadable)

    shown = detail(fresh_db, "S05")

    assert set(statuses(shown).values()) == {"unchecked"}
    assert metrics(shown)["roic"]["stored"] is not None
    assert shown["score"] is not None


def test_the_run_and_its_builds_are_named(fresh_db, scored):
    shown = detail(fresh_db, "S05")

    assert (shown["scored"], shown["run_id"], shown["latest"]) == (True, scored["run"], True)
    assert shown["reproduction"]["visible_through"] == "2026-03-03T06:00:00+00:00"
    assert shown["reproduction"]["run_build"] == shown["reproduction"]["running_build"]
    assert shown["closes"][-1] == [AS_OF.isoformat(), "105.00"]


def test_a_security_with_nothing_scored_is_not_scored(fresh_db, scored):
    assert detail(fresh_db, "NOBR") == {
        "scored": False,
        "run_id": scored["run"],
        "latest": True,
        "symbol": "NOBR",
        "name": "NOBR Inc",
        "sector": {"code": "technology", "name": "Technology"},
        "active": True,
        "closes": [],
    }


def test_a_symbol_is_matched_regardless_of_case_or_a_dollar_sign(fresh_db, scored):
    assert detail(fresh_db, "$s05")["symbol"] == "S05"


def test_an_unknown_symbol_is_refused(fresh_db, scored):
    with pytest.raises(UnknownSymbol):
        detail(fresh_db, "ZZZZ")


def test_a_symbol_listed_on_two_exchanges_is_ambiguous(fresh_db, scored):
    other = fresh_db.execute(
        """insert into security (name, mic, currency, country, primary_symbol, first_seen)
           values ('S05 Other', 'XNYS', 'USD', 'US', 'S05', '2020-01-01') returning id"""
    ).fetchone()[0]
    fresh_db.execute(
        """insert into security_symbol (security_id, symbol, mic, valid_from, source)
           values (%s, 'S05', 'XNYS', '2020-01-01', 'test')""",
        (other,),
    )

    with pytest.raises(AmbiguousSymbol) as caught:
        detail(fresh_db, "S05")

    assert caught.value.exchanges == ("XNAS", "XNYS")


def test_a_pinned_run_that_does_not_exist_is_refused(fresh_db, scored):
    with pytest.raises(RunChanged):
        detail(fresh_db, "S05", run=999_999)


def test_the_detail_awaits_the_first_night_before_looking_for_the_symbol(fresh_db):
    assert detail(fresh_db, "ANY") == {"state": "awaiting_first_night"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_screen_security.py -n0 -q`
Expected: collection error, `ImportError: cannot import name 'AmbiguousSymbol' from 'screener.screen'`.

- [ ] **Step 3: Append the security statements to `queries.py`**

```python
# D10: matched against current symbols, so a renamed ticker's old symbol is not a match.
SYMBOL_MATCH: LiteralString = """
select distinct on (s.id) s.id, sy.symbol, s.name, sy.mic, s.is_active
  from security_symbol sy
  join security s on s.id = sy.security_id
 where upper(sy.symbol) = upper(%(symbol)s::text)
   and sy.valid_to is null
 order by s.id, sy.mic
"""

CLASSIFICATION: LiteralString = """
select coalesce(sector.code, 'unclassified'),
       coalesce(sector.name, 'Unclassified'),
       case when industry.level = 2 then industry.code end,
       case when industry.level = 2 then industry.name end
  from security sec
""" + SECTOR_AT_AS_OF + """
 where sec.id = %(id)s
"""

SNAPSHOT: LiteralString = """
select blended_score, pillar_agreement, min_coverage
  from snapshot_daily
 where scoring_run_id = %(run)s and as_of = %(as_of)s and security_id = %(id)s
"""

PILLARS: LiteralString = """
select p.code, ps.score, ps.metric_count, ps.coverage
  from pillar_score_daily ps
  join pillar p on p.id = ps.pillar_id
 where ps.scoring_run_id = %(run)s and ps.as_of = %(as_of)s and ps.security_id = %(id)s
"""

# A level-0 group has no sector node; it is the market.
STORED_METRICS: LiteralString = """
select m.code, md.raw_value, md.percentile, coalesce(node.name, 'Market'), md.peer_count,
       md.fallback_level, md.period_basis, md.period_end
  from metric_daily md
  join metric m on m.id = md.metric_id
  join peer_group pg on pg.id = md.peer_group_id
  left join sector_node node on node.id = pg.sector_node_id
 where md.scoring_run_id = %(run)s and md.as_of = %(as_of)s and md.security_id = %(id)s
"""

METRIC_INFO: LiteralString = """
select m.code, m.name, m.higher_is_better, p.code
  from metric m
  join pillar p on p.id = m.pillar_id
 where m.code = any(%(codes)s)
"""
```

- [ ] **Step 4: Append the security shapes to `shape.py`**

Extend `shape.py`'s imports so they read:

```python
from collections.abc import Collection, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from screener.scoring import CODES, RATIO_CODES, Action, adjusted_closes
from screener.screen.explain import Check, Reproduction
from screener.screen.rows import (
    ActionRow,
    BarRow,
    ClassificationRow,
    MetricInfoRow,
    MetricRow,
    PillarRow,
    PreviousRunRow,
    RunRow,
    ScreenRow,
    SectorRow,
    SnapshotRow,
    SymbolRow,
    TilesRow,
)
```

Append:

```python
def listed(
    expected: Mapping[str, Sequence[str]],
    stored: Collection[str],
    pillar_of: Mapping[str, str],
) -> dict[str, list[str]]:
    """Each pillar's metrics to show: every one that applies, plus any the run
    stored that no longer does, so a reclassification cannot hide a stored value
    (plan amendment P4)."""
    return {
        pillar: [
            code
            for code in ALL_CODES
            if pillar_of[code] == pillar and (code in expected[pillar] or code in stored)
        ]
        for pillar in PILLAR_ORDER
    }


def unscored_payload(
    *,
    run: RunRow,
    latest: bool,
    match: SymbolRow,
    where: ClassificationRow,
    closes: list[list[str]],
) -> dict[str, Any]:
    return {
        "scored": False,
        "run_id": run.id,
        "latest": latest,
        "symbol": match.symbol,
        "name": match.name,
        "sector": sector(where.sector_code, where.sector_name),
        "active": match.is_active,
        "closes": closes,
    }


def _stored(row: MetricRow) -> dict[str, Any]:
    return {
        "raw": exact(row.raw_value),
        "percentile": one_decimal(row.percentile),
        "peer_group": row.peer_group,
        "peer_count": row.peer_count,
        "market_ranked": row.fallback_level == 0,
        "period_basis": row.period_basis,
        "period_end": None if row.period_end is None else row.period_end.isoformat(),
    }


def security_payload(
    *,
    run: RunRow,
    latest: bool,
    match: SymbolRow,
    where: ClassificationRow,
    snapshot: SnapshotRow,
    pillars: Sequence[PillarRow],
    metrics: Sequence[MetricRow],
    info: Mapping[str, MetricInfoRow],
    expected: Mapping[str, Sequence[str]],
    checks: Mapping[str, Check],
    reproduction: Reproduction,
    running_build: str,
    closes: list[list[str]],
) -> dict[str, Any]:
    stored = {row.code: row for row in metrics}
    scores = {row.pillar_code: row for row in pillars}
    shown = listed(expected, stored, {code: row.pillar_code for code, row in info.items()})
    industry = (
        None
        if where.industry_code is None or where.industry_name is None
        else {"code": where.industry_code, "name": where.industry_name}
    )
    return {
        "scored": True,
        "run_id": run.id,
        "latest": latest,
        "symbol": match.symbol,
        "name": match.name,
        "sector": sector(where.sector_code, where.sector_name),
        "industry": industry,
        "active": match.is_active,
        "score": one_decimal(snapshot.score),
        "agreement": snapshot.pillar_agreement,
        "partial": partial(snapshot.min_coverage),
        "pillars": [
            {
                "code": pillar,
                "key": PILLAR_KEYS[pillar],
                "score": one_decimal(scores[pillar].score) if pillar in scores else None,
                "present": sum(1 for code in shown[pillar] if code in stored),
                "expected": len(expected[pillar]),
                "metrics": [
                    {
                        "code": code,
                        "name": info[code].name,
                        "unit": UNITS[code],
                        "higher_is_better": info[code].higher_is_better,
                        "status": checks[code].status,
                        "stored": _stored(stored[code]) if code in stored else None,
                        "reproduced": exact(checks[code].reproduced),
                        "reason": checks[code].reason,
                    }
                    for code in shown[pillar]
                ],
            }
            for pillar in PILLAR_ORDER
        ],
        "reproduction": {
            "visible_through": reproduction.visible_through.isoformat(),
            "run_build": run.git_sha,
            "running_build": running_build,
            "refreshed_inputs": sorted(reproduction.refreshed),
        },
        "closes": closes,
    }
```

- [ ] **Step 5: Append the security read to `read.py`**

Extend `read.py`'s imports so they read:

```python
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any, LiteralString, TypeVar

import psycopg

from screener.provenance import git_sha
from screener.scoring import (
    CODES,
    LOGIC_DESCRIPTION,
    MOMENTUM,
    QUALITY,
    VALUATION,
    applicable,
    resolve,
)
from screener.screen import explain, queries, shape
from screener.screen.params import ScreenParams, SecurityParams
from screener.screen.rows import (
    ActionRow,
    BarRow,
    ClassificationRow,
    MetricInfoRow,
    MetricRow,
    PillarRow,
    PreviousRunRow,
    RunRow,
    ScreenRow,
    SectorRow,
    SnapshotRow,
    SymbolRow,
    TilesRow,
    parse_all,
)
```

After `RunChanged`, add:

```python
class UnknownSymbol(LookupError):
    """No current symbol matches (D10)."""

    def __init__(self, symbol: str) -> None:
        super().__init__(f"{symbol} is not a current symbol in the universe")
        self.symbol = symbol


class AmbiguousSymbol(LookupError):
    """More than one security trades under this symbol today (D10)."""

    def __init__(self, symbol: str, exchanges: tuple[str, ...]) -> None:
        super().__init__(f"{symbol} is listed on {', '.join(exchanges)}")
        self.symbol = symbol
        self.exchanges = exchanges
```

Append:

```python
def read_security(conn: psycopg.Connection, params: SecurityParams) -> dict[str, Any]:
    """`/api/screen/security`: one security on the served night, every metric re-checked (D10)."""
    resolved = resolve_run(conn, params.run)
    if resolved is None:
        return shape.awaiting()
    run, latest = resolved

    matches = _all(conn, queries.SYMBOL_MATCH, {"symbol": params.symbol}, SymbolRow)
    if not matches:
        raise UnknownSymbol(params.symbol)
    if len(matches) > 1:
        raise AmbiguousSymbol(params.symbol, tuple(match.mic for match in matches))
    match = matches[0]
    security_id = match.security_id
    bind = {"id": security_id, "run": run.id, "as_of": run.as_of}

    where = _one(conn, queries.CLASSIFICATION, bind, ClassificationRow)
    # Read from `security` itself, which the symbol match has just found.
    assert where is not None
    closes = _closes(conn, [security_id], run.as_of, CHART_CLOSES)[security_id]
    snapshot = _one(conn, queries.SNAPSHOT, bind, SnapshotRow)
    if snapshot is None:
        return shape.unscored_payload(
            run=run, latest=latest, match=match, where=where, closes=closes
        )

    metrics = _all(conn, queries.STORED_METRICS, bind, MetricRow)
    pillars = _all(conn, queries.PILLARS, bind, PillarRow)
    info = {
        row.code: row
        for row in _all(conn, queries.METRIC_INFO, {"codes": list(shape.ALL_CODES)}, MetricInfoRow)
    }
    # The industry as scoring resolves it, so `expected` is the denominator the
    # run's coverage used -- unless the security was reclassified since (F15).
    industry = resolve(conn, [security_id], as_of=run.as_of)[security_id].industry
    applies = applicable(industry)
    expected = {VALUATION: applies[VALUATION], QUALITY: applies[QUALITY], MOMENTUM: CODES}
    stored = {row.code: row.raw_value for row in metrics}

    # Last: a failure inside it is reported rather than raised, and leaves this
    # connection's transaction unusable for any read after it.
    reproduction = explain.reproduce(conn, security_id=security_id, run=run, industry=industry)
    shown = shape.listed(expected, stored, {code: row.pillar_code for code, row in info.items()})
    checks = {
        code: explain.check(
            pillar=pillar, code=code, stored=stored.get(code), reproduction=reproduction
        )
        for pillar, codes in shown.items()
        for code in codes
    }
    return shape.security_payload(
        run=run,
        latest=latest,
        match=match,
        where=where,
        snapshot=snapshot,
        pillars=pillars,
        metrics=metrics,
        info=info,
        expected=expected,
        checks=checks,
        reproduction=reproduction,
        running_build=git_sha(),
        closes=closes,
    )
```

- [ ] **Step 6: Export from the package**

In `src/screener/screen/__init__.py`, add `AmbiguousSymbol`, `UnknownSymbol` and `read_security` to the `read` import, and `listed`, `security_payload` and `unscored_payload` to the `shape` import.

Add them to `__all__` at their RUF022 positions:
- `"AmbiguousSymbol"` first in CamelCase, and `"UnknownSymbol"` last in CamelCase.
- `"listed"` after `"exact"`, `"read_security"` after `"read_screen"`, `"security_payload"` after `"sector"`, and `"unscored_payload"` after `"security_params"`.

- [ ] **Step 7: Run the tests to verify they pass, and typecheck**

```bash
.venv/bin/python -m pytest tests/test_screen_security.py tests/test_screen_read.py tests/test_screen_shape.py tests/test_screen_explain.py -n0 -q
.venv/bin/pyright src/screener/screen tests/test_screen_security.py 2>&1 | tail -1
```

Expected: all passed; `0 errors`.

- [ ] **Step 8: Commit**

```bash
git add src/screener/screen/queries.py src/screener/screen/shape.py src/screener/screen/read.py src/screener/screen/__init__.py tests/test_screen_security.py
git commit -m "Show one security's stored metrics beside their reproduction, each with its status"
```

---

### Task 8: The two endpoints, and the docs that describe them

**Files:**
- Modify: `src/screener/health/server.py`
- Modify: `tests/test_auth.py` (the unconfigured-server route list)
- Modify: `CLAUDE.md`
- Modify: `docs/architecture.md`
- Test: `tests/test_screen_http.py`

**Interfaces:**
- Consumes, from `screener.screen`:
  - `screen_params`, `security_params` and `BadParameter`.
  - `read_screen` and `read_security`.
  - `RunChanged`, `UnknownSymbol` and `AmbiguousSymbol`.
- Produces: `GET /api/screen` and `GET /api/screen/security`, with the statuses of spec §7 and plan amendment P3.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_screen_http.py`:

```python
"""The screen endpoints over HTTP: who may ask, and every refusal (ui-swap spec §7)."""

import json
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

import psycopg
import pytest

from screener import auth
from screener.health import build_server
from screener.scoring import LOGIC_DESCRIPTION

SECRET = "a-session-secret"
AS_OF = date(2026, 3, 2)


@pytest.fixture
def server(monkeypatch, db_url, fresh_db):
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("GITHUB_CLIENT_ID", "cid")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "sec")
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    monkeypatch.setenv("ALLOWED_GITHUB_LOGINS", "ehewes,D1K03")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8080")
    server = build_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", fresh_db
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def cookie(server) -> str:
    _, conn = server
    token = auth.create_session(conn, github_id=1, login="ehewes", secret=SECRET)
    return f"{auth.SESSION_COOKIE}={token}"


def get(url: str, cookie: str | None = None) -> tuple[int, Any]:
    request = urllib.request.Request(url)
    if cookie:
        request.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _listed(conn: Any, symbol: str, mic: str = "XNAS") -> int:
    security_id = conn.execute(
        """insert into security (name, mic, currency, country, primary_symbol, first_seen)
           values (%s, %s, 'USD', 'US', %s, '2020-01-01') returning id""",
        (f"{symbol} {mic}", mic, symbol),
    ).fetchone()[0]
    conn.execute(
        """insert into security_symbol (security_id, symbol, mic, valid_from, source)
           values (%s, %s, %s, '2020-01-01', 'test')""",
        (security_id, symbol, mic),
    )
    return security_id


def _night(conn: Any) -> int:
    """One v2 night with one unclassified security, JPM, scored on Momentum."""
    scheme = conn.execute(
        "insert into sector_scheme (code, name) values ('yfinance', 'yfinance') returning id"
    ).fetchone()[0]
    conn.execute(
        "insert into peer_group (scheme_id, sector_node_id, level, code)"
        " values (%s, null, 0, 'market')",
        (scheme,),
    )
    security_id = _listed(conn, "JPM")
    started = datetime.combine(AS_OF, time(23), tzinfo=timezone.utc)
    run_id = conn.execute(
        """insert into scoring_run
           (as_of_range, cutoff_offset, logic_version_id, weight_version_id, status,
            emits_alerts, git_sha, config_hash, started_at, finished_at, outcome)
           select daterange(%(as_of)s, %(next)s, '[)'), interval '30 hours', l.id, w.id,
                  'live', false, 'abc1234', %(hash)s, %(started)s, %(started)s, 'ok'
             from scoring_logic_version l, weight_version w
            where l.description = %(logic)s and w.code = 'v2'
           returning id""",
        {"as_of": AS_OF, "next": AS_OF + timedelta(days=1), "hash": b"\x9f",
         "started": started, "logic": LOGIC_DESCRIPTION},
    ).fetchone()[0]
    conn.execute(
        """insert into snapshot_daily
           (as_of, scoring_run_id, security_id, blended_score, pillar_agreement,
            min_coverage, worst_fallback_level)
           values (%s, %s, %s, %s, 1, 1, 0)""",
        (AS_OF, run_id, security_id, Decimal("61.5")),
    )
    conn.execute(
        """insert into pillar_score_daily
           (as_of, scoring_run_id, security_id, pillar_id, score, metric_count, coverage)
           select %s, %s, %s, p.id, %s, 4, 1 from pillar p where p.code = 'momentum'""",
        (AS_OF, run_id, security_id, Decimal("61.5")),
    )
    return run_id


@pytest.mark.parametrize("path", ["/api/screen", "/api/screen/security?symbol=JPM"])
def test_the_screen_is_refused_without_a_session(server, path):
    url, _ = server

    status, body = get(url + path)

    assert status == 401
    assert "sign in" in body["error"]


def test_a_bad_parameter_is_refused_by_name(server, cookie):
    url, _ = server

    assert get(url + "/api/screen?sort=price", cookie) == (
        400, {"error": "sort must be one of score, delta, V, Q, M", "parameter": "sort"},
    )
    assert get(url + "/api/screen?colour=red", cookie)[1]["parameter"] == "colour"
    assert get(url + "/api/screen/security", cookie) == (
        400, {"error": "symbol is required", "parameter": "symbol"},
    )


def test_before_the_first_night_both_endpoints_say_so(server, cookie):
    url, _ = server

    for path in ("/api/screen", "/api/screen/security?symbol=JPM"):
        assert get(url + path, cookie) == (200, {"state": "awaiting_first_night"})


def test_the_screen_answers_with_every_top_level_key(server, cookie):
    url, conn = server
    run_id = _night(conn)

    status, body = get(url + "/api/screen", cookie)

    assert status == 200
    assert set(body) == {
        "state", "latest", "run", "previous_as_of", "tiles", "sectors", "total", "rows",
    }
    assert body["run"]["id"] == run_id
    assert [row["symbol"] for row in body["rows"]] == ["JPM"]


def test_a_security_answers_with_every_top_level_key(server, cookie):
    url, conn = server
    _night(conn)

    status, body = get(url + "/api/screen/security?symbol=jpm", cookie)

    assert status == 200
    assert set(body) == {
        "scored", "run_id", "latest", "symbol", "name", "sector", "industry", "active",
        "score", "agreement", "partial", "pillars", "reproduction", "closes",
    }


def test_an_unknown_symbol_is_404(server, cookie):
    url, conn = server
    _night(conn)

    assert get(url + "/api/screen/security?symbol=ZZZZ", cookie) == (
        404, {"error": "unknown_symbol", "symbol": "ZZZZ"},
    )


def test_a_symbol_on_two_exchanges_is_409(server, cookie):
    url, conn = server
    _night(conn)
    _listed(conn, "JPM", "XNYS")

    assert get(url + "/api/screen/security?symbol=JPM", cookie) == (
        409, {"error": "ambiguous_symbol", "symbol": "JPM", "exchanges": ["XNAS", "XNYS"]},
    )


def test_a_pinned_run_that_does_not_qualify_is_409(server, cookie):
    url, conn = server
    _night(conn)

    for path in ("/api/screen?run=999999", "/api/screen/security?symbol=JPM&run=999999"):
        assert get(url + path, cookie) == (409, {"error": "run_changed", "run": 999999})


def test_an_unreachable_database_is_503_naming_only_the_error_type(server, cookie, monkeypatch):
    url, _ = server

    def unreachable(*args: object) -> None:
        raise psycopg.OperationalError("connection to 10.0.0.5 as user screener failed")

    monkeypatch.setattr("screener.screen.read_screen", unreachable)

    assert get(url + "/api/screen", cookie) == (
        503, {"error": "cannot read the screen", "database": "OperationalError"},
    )
```

In `tests/test_auth.py`, add these entries to the `unconfigured_server` parametrize list, after `("/api/handoff", None),`:

```python
        ("/api/screen", None),
        ("/api/screen/security?symbol=JPM", None),
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_screen_http.py -n0 -q`
Expected: most tests FAIL with `404 != 401` or `404 != 200`, because the routes do not exist.

- [ ] **Step 3: Add the routes to `server.py`**

In `src/screener/health/server.py`:
- Change `from screener import audit, auth, mcp` to `from screener import audit, auth, mcp, screen`.
- In `do_GET`, directly after the `elif route == "/api/audit":` branch and its call, add:

```python
        elif route == "/api/screen":
            self._screen(config, query)

        elif route == "/api/screen/security":
            self._screen_security(config, query)
```

Directly after the `_audit` method, add:

```python
    def _screen(self, config: AuthConfig, query: dict[str, list[str]]) -> None:
        """One page of the scored screen (ui-swap spec D9).

        On the application's own connection rather than a read-only role: the SQL
        is fixed in `screener.screen.queries` and only bound parameters vary, which
        is not what the playground's role exists to guard against.
        """
        login = self._require_login(config)
        if login is None:
            return
        try:
            params = screen.screen_params(query)
        except screen.BadParameter as exc:
            self._respond(HTTPStatus.BAD_REQUEST, {"error": str(exc), "parameter": exc.name})
            return
        self._screen_read(lambda conn: screen.read_screen(conn, params))

    def _screen_security(self, config: AuthConfig, query: dict[str, list[str]]) -> None:
        """One security on the screen's night, every metric re-checked (ui-swap spec D10)."""
        login = self._require_login(config)
        if login is None:
            return
        try:
            params = screen.security_params(query)
        except screen.BadParameter as exc:
            self._respond(HTTPStatus.BAD_REQUEST, {"error": str(exc), "parameter": exc.name})
            return
        self._screen_read(lambda conn: screen.read_security(conn, params))

    def _screen_read(self, read: Callable[[psycopg.Connection], dict[str, Any]]) -> None:
        """Run one screen read and answer with it, or with the refusal it raised (spec §7)."""
        try:
            with psycopg.connect(settings().database_url, connect_timeout=3) as conn:
                payload = read(conn)
        except screen.RunChanged as exc:
            self._respond(HTTPStatus.CONFLICT, {"error": "run_changed", "run": exc.run_id})
        except screen.AmbiguousSymbol as exc:
            self._respond(
                HTTPStatus.CONFLICT,
                {"error": "ambiguous_symbol", "symbol": exc.symbol, "exchanges": list(exc.exchanges)},
            )
        except screen.UnknownSymbol as exc:
            self._respond(HTTPStatus.NOT_FOUND, {"error": "unknown_symbol", "symbol": exc.symbol})
        except psycopg.Error as exc:
            # Named by type only: psycopg puts the host and the user in a
            # connection error's message.
            logger.warning("could not read the screen: %s", type(exc).__name__)
            self._respond(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "cannot read the screen", "database": type(exc).__name__},
            )
        else:
            self._respond(HTTPStatus.OK, payload)
```

`Callable` is already imported from `collections.abc`; check with `grep -n "^from collections.abc" src/screener/health/server.py`.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_screen_http.py tests/test_auth.py tests/test_health.py -n0 -q
```

Expected: all passed.

- [ ] **Step 5: Update `CLAUDE.md` (spec §11, the piece (b) erratum)**

In the `screener.health` bullet, after "…which makes an unconfigured sign-in open the endpoints rather than close them.", add:

```
`/api/screen` and `/api/screen/security` serve the scored screen through `screener.screen`,
on the application's own connection: the SQL is fixed and only bound parameters vary, which
is not what the playground's read-only roles guard.
```

After the `screener.scoring` bullet, which is the last in the list, add:

```
- `screener.screen` — the scored screen, read for the dashboard's two endpoints and, from piece
  (c) of the UI swap, for Steven's chart. `queries` holds every statement as a literal; `rows`
  parses a psycopg row or a `playground.Result` row into one typed record, because the
  playground's cells come back as JSON-safe strings; `params` refuses a wrong query-string value
  by name; `shape` builds JSON whose every figure is a decimal string, never a float. `read`
  serves the latest good v2 night, keeps serving a pinned one after a newer lands, and measures
  Δ against the previous night under the same weights. `explain` re-runs scoring's explaining
  forms for one security under `least(cutoff, started_at)` — the run's actual view — and gives
  each metric one status: ok, mismatch, absent, unexpected, refreshed or unchecked. **Display
  closes ignore the cutoff**, because ingest re-stamps the last week of bars every night; a
  reproduction never does. Spec `docs/specs/2026-09-13-ui-swap.md`.
```

Rewrap both to the file's existing width and two-space continuation indent.

- [ ] **Step 6: Update `docs/architecture.md`**

In the package-graph mermaid block:
- After the line `prov["provenance"]`, add:

```
    screen["screen<br/>queries + rows + params + shape + read + explain"]
```

- After the line `health --> prov`, add:

```
    health --> screen
    screen --> scoring
    screen --> ingest
    screen --> prov
```

Leave the `concept` node and the `tools --> concept` edge; piece (c) replaces them.

- [ ] **Step 7: Run every guard**

```bash
.venv/bin/python -m pytest -q 2>&1 | tail -1
.venv/bin/pyright 2>&1 | grep errors
git diff main --stat -- src/screener/scoring migrations tests/golden web | tail -1
```

Expected:
- The full suite shows only the 5 known magpie DNS failures.
- pyright prints `0 errors, 0 warnings, 0 informations`.
- The `git diff` prints nothing, which proves scoring, migrations, the golden output and the web app are untouched.

- [ ] **Step 8: Commit**

```bash
git add src/screener/health/server.py tests/test_screen_http.py tests/test_auth.py CLAUDE.md docs/architecture.md
git commit -m "Serve the screen and one security's traceable detail behind the session"
```
