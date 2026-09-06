# Fundamentals Ingest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ingest Yahoo's fundamentals timeseries into `fundamental_fact` with its observation trail, and ship the point-in-time read that is the only thing able to prove those writes correct.

**Architecture:** `screener.ingest` gains a fourth pair of modules alongside the price path: `timeseries.py` (a sessionless client shaped like `ChartClient`) and `facts.py` (a pure parser), with `load.py`, `run.py` and `cli.py` extended rather than duplicated. Facts are appended only when a value differs from the latest held, every fact from one payload carries its observation's `fetched_at` as `observed_at`, and the read is a `distinct on` that includes `period_type` — which the schema's own documented read omits, and which is the bug this cycle exists to not repeat.

**Tech Stack:** Python 3.11+, `httpx`, `psycopg` 3, Postgres 16, `pytest`, `pyright`. No new dependencies.

**Spec:** `docs/specs/2026-09-06-fundamentals-ingest.md`

## Global Constraints

- **No new runtime dependencies.** `httpx`, `psycopg` and `discord.py` are the whole runtime list.
- **`Decimal` throughout, never `float`.** `fundamental_fact.value` is `numeric`.
- **All SQL identifiers lowercase; `timestamptz` never `timestamp`.**
- **psycopg types query parameters as `LiteralString`**, so SQL assembled at runtime is rejected by design. Every query is a single literal string with placeholders — never concatenation or an f-string, in `src/` or in `tests/`.
- **Each package has a small public surface through `__init__.py`; nothing outside imports a submodule directly.** A package's own CLI importing its siblings is the established exception.
- **`__all__` ordering follows ruff's RUF022** — SCREAMING_CASE, then CamelCase, then snake_case, alphabetical within each group — *not* plain `sorted()`. `src/screener/ingest/__init__.py` is the precedent.
- **Comments explain *why*, not *what*.**
- **Pure computation is separated from database work**: `facts.py` opens no connection, `load.py` opens no socket.
- `pyright` must report zero errors, and it checks `tests/` as well as `src/`.
- **Nothing in this cycle scores.** `screener.scoring.CODES` is untouched, no `pillar_score_daily` row changes, `screener.concept` stays, and `illustrative` stays `true`.

## Environment

Tests need a database:

```bash
export DATABASE_URL_TEST="postgresql://postgres:screener@localhost:5432/screener_test"
```

The value committed in `.env` is a unix-socket URL that does **not** reach the Docker container — ignore it. Use `.venv/bin/python -m pytest` and **always pass an explicit worker count**: `-n0` for a single file, `-n 4` for the whole suite. Bare `pytest` uses `-n auto`, which spawns 24+ workers and kills the container's Postgres with OutOfMemory — an environment limit, not a defect in your work.

Baseline before Task 1: **821 passed, 1 skipped.**

---

### Task 1: The migration — `is_input`, the corrected index, and 28 seeded metrics

Nothing can be stored until `metric` rows exist, because `fundamental_fact.metric_id` is a not-null foreign key. This lands first for the same reason the scoring cycle's seed did.

**Files:**
- Create: `migrations/021_fundamental_metrics.sql`
- Test: `tests/test_fundamental_metrics.py`
- Modify: `tests/conftest.py:23` (the migration count in the `db_url` docstring)

**Interfaces:**
- Consumes: nothing.
- Produces: `metric.is_input` (boolean, not null, default false); 28 seeded metric rows whose codes every later task looks up by name; `fundamental_fact_pit_idx2` on `(security_id, metric_id, period_end, period_type, observed_at desc)`; `fundamental_fact_pit_idx` dropped.

- [ ] **Step 1: Write the failing test**

Create `tests/test_fundamental_metrics.py`:

```python
"""Migration 021: the input metrics, the flag, and the corrected index.

`metric.code` has to agree with the code parsing it, so the two ship together.
These tests are what makes "together" checkable.
"""

INPUT_CODES = {
    "revenue", "cost_of_revenue", "gross_profit", "research_and_development",
    "selling_general_admin", "operating_income", "ebit", "interest_expense",
    "pretax_income", "tax_provision", "net_income", "depreciation_amortisation",
    "operating_cash_flow", "capital_expenditure", "total_assets",
    "current_assets", "current_liabilities", "total_liabilities",
    "stockholders_equity", "cash_and_equivalents",
    "cash_and_short_term_investments", "current_debt", "long_term_debt",
    "total_debt", "net_ppe", "shares_basic_avg", "shares_diluted_avg",
    "shares_outstanding",
}


def test_twenty_eight_input_metrics_are_seeded(fresh_db):
    codes = {
        row[0]
        for row in fresh_db.execute(
            "select code from metric where is_input"
        ).fetchall()
    }
    assert codes == INPUT_CODES


def test_the_momentum_metrics_are_not_inputs(fresh_db):
    # The flag is what separates "stored as evidence" from "scored", and the
    # four price metrics are the other side of it.
    rows = fresh_db.execute(
        "select code from metric where not is_input order by code"
    ).fetchall()
    assert [r[0] for r in rows] == [
        "off_52w_high", "ret_12m", "ret_3m", "ret_6m"
    ]


def test_every_input_is_quarterly_cadence_and_a_known_unit(fresh_db):
    rows = fresh_db.execute(
        "select distinct cadence, unit from metric where is_input order by unit"
    ).fetchall()
    assert rows == [("quarterly", "currency"), ("quarterly", "shares")]


def test_the_three_share_counts_are_the_only_shares_unit(fresh_db):
    rows = fresh_db.execute(
        "select code from metric where unit = 'shares' order by code"
    ).fetchall()
    assert [r[0] for r in rows] == [
        "shares_basic_avg", "shares_diluted_avg", "shares_outstanding"
    ]


def test_no_derivation_is_seeded(fresh_db):
    # D3: a figure Yahoo computes from lines we already store is not stored.
    # These are the six that were measured as exactly reproducible.
    codes = {
        row[0] for row in fresh_db.execute("select code from metric").fetchall()
    }
    assert codes.isdisjoint(
        {"ebitda", "net_debt", "working_capital", "invested_capital",
         "tangible_book_value", "total_capitalization", "free_cash_flow"}
    )


def test_the_point_in_time_index_includes_period_type(fresh_db):
    # D6: without period_type a fiscal year collapses into its own Q4.
    definition = fresh_db.execute(
        "select indexdef from pg_indexes where indexname = %s",
        ("fundamental_fact_pit_idx2",),
    ).fetchone()
    assert definition is not None
    text = definition[0]
    assert "period_end, period_type, observed_at DESC" in text


def test_the_old_index_that_omitted_period_type_is_gone(fresh_db):
    row = fresh_db.execute(
        "select indexname from pg_indexes where indexname = %s",
        ("fundamental_fact_pit_idx",),
    ).fetchone()
    assert row is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fundamental_metrics.py -n0 -v`
Expected: FAIL — `UndefinedColumn: column "is_input" does not exist`.

- [ ] **Step 3: Write the migration**

Create `migrations/021_fundamental_metrics.sql`:

```sql
-- The line items fundamentals ingest stores, the flag that says they are not
-- scored, and the point-in-time index corrected.
--
-- Seeded by migration rather than by a command for the reason 019 gives: a
-- `metric.code` has to agree with the code parsing it, so they change together
-- and therefore ship together.

-- `is_input` rather than reusing `is_active`. The two would look
-- interchangeable today -- nothing scores an input, so `is_active = false`
-- would read correctly -- and stop being interchangeable the first time a
-- metric is genuinely retired, at which point "which of these false rows are
-- inputs and which are dead?" has no answer left in the data. The collision is
-- certain rather than hypothetical, and the column is cheap now.
alter table metric add column is_input boolean not null default false;

-- D6. `fundamental_fact`'s unique constraint includes `period_type`; its index
-- did not, and neither did the point-in-time read documented in the schema
-- spec. A fiscal Q4 ends when its fiscal year does -- AAPL reports both
-- annual and quarterly revenue at 2025-09-30, four-fold apart -- so
-- `distinct on (security_id, metric_id, period_end)` collapses the year into
-- its own last quarter and returns whichever was inserted later. Silently, for
-- every company, every year.
--
-- Column order matches the read's `order by` exactly and has to: with
-- period_type ahead of period_end, Postgres cannot satisfy the `distinct on`
-- from this index and would sort anyway, having paid for the index on insert.
create index fundamental_fact_pit_idx2 on fundamental_fact
    (security_id, metric_id, period_end, period_type, observed_at desc);

-- The old one is dropped rather than kept. The only query it serves better
-- than the index above is "the latest observation of one metric for one period
-- end, across both period types" -- which is precisely the query D6 exists to
-- say nobody should run. Keeping it would cost an index write per fact per
-- night in order to serve a mistake.
drop index fundamental_fact_pit_idx;

-- 28 line items. `is_input = true` throughout: stored as evidence, never
-- scored, and no pillar average will ever include one.
--
-- `pillar_id` is a fiction and has to be, because the column is `not null` and
-- an input does not belong to a pillar -- `shares_diluted_avg` feeds Valuation
-- and Quality both, and `revenue` feeds Valuation through P/S and Quality
-- through every margin. Nothing reads it for an input row, and any query that
-- groups inputs by pillar will get a plausible-looking wrong answer.
--
-- `cadence` and `higher_is_better` are equally unread: period granularity
-- lives in `period_type` per fact, and nothing ranks an input. They are
-- not-null columns being filled.
insert into metric (code, name, pillar_id, unit, higher_is_better, cadence, is_input)
select v.code, v.name, p.id, v.unit, true, 'quarterly', true
  from (values
        -- income statement
        ('revenue',                  'Total revenue',              'currency', 'valuation'),
        ('cost_of_revenue',          'Cost of revenue',            'currency', 'quality'),
        ('gross_profit',             'Gross profit',               'currency', 'quality'),
        ('research_and_development', 'Research and development',   'currency', 'quality'),
        ('selling_general_admin',    'Selling, general and admin', 'currency', 'quality'),
        ('operating_income',         'Operating income',           'currency', 'quality'),
        ('ebit',                     'EBIT',                       'currency', 'valuation'),
        ('interest_expense',         'Interest expense',           'currency', 'quality'),
        ('pretax_income',            'Pretax income',              'currency', 'quality'),
        ('tax_provision',            'Tax provision',              'currency', 'quality'),
        ('net_income',               'Net income',                 'currency', 'valuation'),
        -- cash flow
        ('depreciation_amortisation', 'Depreciation and amortisation', 'currency', 'valuation'),
        ('operating_cash_flow',      'Operating cash flow',        'currency', 'quality'),
        ('capital_expenditure',      'Capital expenditure',        'currency', 'quality'),
        -- balance sheet
        ('total_assets',             'Total assets',               'currency', 'quality'),
        ('current_assets',           'Current assets',             'currency', 'quality'),
        ('current_liabilities',      'Current liabilities',        'currency', 'quality'),
        ('total_liabilities',        'Total liabilities',          'currency', 'quality'),
        ('stockholders_equity',      'Stockholders equity',        'currency', 'valuation'),
        ('cash_and_equivalents',     'Cash and equivalents',       'currency', 'quality'),
        ('cash_and_short_term_investments',
                                     'Cash and short-term investments', 'currency', 'quality'),
        ('current_debt',             'Current debt',               'currency', 'quality'),
        ('long_term_debt',           'Long-term debt',             'currency', 'quality'),
        ('total_debt',               'Total debt',                 'currency', 'quality'),
        ('net_ppe',                  'Net property, plant and equipment', 'currency', 'quality'),
        -- share counts. The first two are averages across the period; the
        -- third is the count at the period end, and the names say which
        -- because they were measured a percent apart and a ratio reaching for
        -- the wrong one is wrong by exactly the amount nobody notices.
        ('shares_basic_avg',         'Basic average shares',       'shares',   'valuation'),
        ('shares_diluted_avg',       'Diluted average shares',     'shares',   'valuation'),
        ('shares_outstanding',       'Shares outstanding',         'shares',   'valuation')
       ) as v(code, name, unit, pillar_code)
  join pillar p on p.code = v.pillar_code
on conflict (code) do nothing;
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_fundamental_metrics.py -n0 -v`
Expected: PASS, 7 passed.

- [ ] **Step 5: Update the migration count and run the whole suite**

In `tests/conftest.py`, the `db_url` docstring says "Applying twenty migrations costs about 320ms". Change "twenty" to "twenty-one".

Run: `.venv/bin/python -m pytest -n 4`
Expected: PASS. If `tests/test_derived.py` or `tests/test_point_in_time.py` fail on a dropped index, read the failure — no test should depend on `fundamental_fact_pit_idx` by name, and one that does needs updating to the new name.

- [ ] **Step 6: Commit**

```bash
git add migrations/021_fundamental_metrics.sql tests/test_fundamental_metrics.py tests/conftest.py
git commit -m "Seed the line items, flag them as inputs, and fix the point-in-time index"
```

---

### Task 2: `facts.py` — the pure parser

**Files:**
- Create: `src/screener/ingest/facts.py`
- Modify: `src/screener/ingest/__init__.py`
- Test: `tests/test_ingest_facts.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `SERIES: dict[str, str]` — Yahoo stem → `metric.code`, 28 entries, the same codes migration 021 seeds.
  - `Fact` — frozen dataclass: `metric_code: str`, `period_end: date`, `period_type: str`, `value: Decimal`, `currency: str | None`.
  - `parse(payload: bytes) -> list[Fact]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_ingest_facts.py`:

```python
"""Timeseries JSON to facts. No I/O, no database, no clock.

The awkward cases are the point: Yahoo pads its arrays with nulls, reports a
fiscal Q4 on the same date as its fiscal year, and names its own currency.
"""

import json
from datetime import date
from decimal import Decimal

from screener.ingest import SERIES, Fact, parse_facts as parse


def _payload(*series: dict) -> bytes:
    return json.dumps({"timeseries": {"result": list(series)}}).encode()


def _entry(as_of: str, raw, currency: str | None = "USD") -> dict:
    entry = {"asOfDate": as_of, "reportedValue": {"raw": raw}}
    if currency is not None:
        entry["currencyCode"] = currency
    return entry


def test_the_series_map_matches_the_seeded_metric_codes():
    assert len(SERIES) == 28
    assert SERIES["TotalRevenue"] == "revenue"
    assert SERIES["OrdinarySharesNumber"] == "shares_outstanding"
    assert SERIES["BasicAverageShares"] == "shares_basic_avg"


def test_no_derivation_is_in_the_series_map():
    # D3: EBITDA and free cash flow were measured as exactly reproducible.
    assert "EBITDA" not in SERIES
    assert "FreeCashFlow" not in SERIES


def test_an_annual_series_parses_to_facts():
    payload = _payload({
        "meta": {"symbol": ["AAPL"], "type": ["annualTotalRevenue"]},
        "annualTotalRevenue": [_entry("2025-09-30", 416161000000)],
    })

    facts = parse(payload)

    assert facts == [
        Fact("revenue", date(2025, 9, 30), "A",
             Decimal("416161000000"), "USD")
    ]


def test_the_quarterly_prefix_becomes_period_type_q():
    payload = _payload({
        "meta": {"symbol": ["AAPL"], "type": ["quarterlyTotalRevenue"]},
        "quarterlyTotalRevenue": [_entry("2026-06-30", 109417000000)],
    })

    assert parse(payload)[0].period_type == "Q"


def test_a_quarter_and_a_year_sharing_a_period_end_both_survive():
    # F5: a fiscal Q4 ends when its fiscal year does. Both facts must exist,
    # distinctly, or the collapse D6 describes starts here in the parser.
    payload = _payload(
        {
            "meta": {"type": ["annualTotalRevenue"]},
            "annualTotalRevenue": [_entry("2025-09-30", 416161000000)],
        },
        {
            "meta": {"type": ["quarterlyTotalRevenue"]},
            "quarterlyTotalRevenue": [_entry("2025-09-30", 102466000000)],
        },
    )

    facts = parse(payload)

    assert len(facts) == 2
    assert {f.period_type for f in facts} == {"A", "Q"}
    assert len({f.value for f in facts}) == 2


def test_nulls_in_the_array_are_skipped_not_stored_as_zero():
    # A null becoming 0 would be a fabricated fundamental.
    payload = _payload({
        "meta": {"type": ["annualNetIncome"]},
        "annualNetIncome": [None, _entry("2025-09-30", 112010000000), None],
    })

    facts = parse(payload)

    assert len(facts) == 1
    assert facts[0].value == Decimal("112010000000")


def test_an_entry_with_no_reported_value_is_skipped():
    payload = _payload({
        "meta": {"type": ["annualNetIncome"]},
        "annualNetIncome": [{"asOfDate": "2025-09-30"}],
    })

    assert parse(payload) == []


def test_the_currency_comes_from_the_payload():
    payload = _payload({
        "meta": {"type": ["annualTotalRevenue"]},
        "annualTotalRevenue": [_entry("2025-09-30", 1, currency="GBP")],
    })

    assert parse(payload)[0].currency == "GBP"


def test_a_value_without_a_currency_stores_none_rather_than_usd():
    # A null means "the provider did not say", which is true. Assuming dollars
    # might not be, and the assumption would be unrecoverable.
    payload = _payload({
        "meta": {"type": ["annualTotalRevenue"]},
        "annualTotalRevenue": [_entry("2025-09-30", 1, currency=None)],
    })

    assert parse(payload)[0].currency is None


def test_a_series_we_do_not_want_is_ignored():
    payload = _payload({
        "meta": {"type": ["annualEBITDA"]},
        "annualEBITDA": [_entry("2025-09-30", 144748000000)],
    })

    assert parse(payload) == []


def test_a_negative_value_is_kept_as_reported():
    # Capital expenditure arrives negative. Sign conventions are the consuming
    # ratio's problem, not the fact's.
    payload = _payload({
        "meta": {"type": ["annualCapitalExpenditure"]},
        "annualCapitalExpenditure": [_entry("2025-09-30", -12715000000)],
    })

    assert parse(payload)[0].value == Decimal("-12715000000")


def test_an_empty_or_error_payload_parses_to_nothing():
    assert parse(json.dumps({"timeseries": {"result": []}}).encode()) == []
    assert parse(json.dumps({"timeseries": {"error": "nope"}}).encode()) == []


def test_a_non_finite_value_is_refused_rather_than_stored():
    # Python's json decoder accepts bare NaN; `numeric` would take it and every
    # later comparison against it would be false.
    payload = json.dumps(
        {"timeseries": {"result": [{
            "meta": {"type": ["annualTotalRevenue"]},
            "annualTotalRevenue": [
                {"asOfDate": "2025-09-30",
                 "reportedValue": {"raw": float("nan")},
                 "currencyCode": "USD"},
            ],
        }]}}
    ).encode()

    assert parse(payload) == []

```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ingest_facts.py -n0 -v`
Expected: FAIL with `ImportError: cannot import name 'SERIES' from 'screener.ingest'`.

- [ ] **Step 3: Write the implementation**

Create `src/screener/ingest/facts.py`:

```python
"""Timeseries JSON to facts. No I/O, no database, no clock.

Kept pure so the awkward cases -- nulls padding an array, a series we did not
ask for, a fiscal Q4 sharing its date with the fiscal year -- are tested
without a socket.

`Decimal` throughout rather than float, as `parse.py` does: a fundamental is
money and a float that reads 416160999999.99994 is a number nobody reported.
"""

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

# Yahoo's stem -> our `metric.code`, agreeing by hand with
# `migrations/021_fundamental_metrics.sql`. Both prefixes of each stem are
# requested, so this is 28 metrics and 56 `type=` values.
#
# What is absent is as deliberate as what is here: a line Yahoo *reports* is
# stored, a figure Yahoo *computes* from lines we already store is not. EBITDA
# (EBIT + depreciation) and free cash flow (operating cash flow less capital
# expenditure) were both measured as reproducing the reported figure exactly,
# so they are derived at scoring time rather than stored.
SERIES: dict[str, str] = {
    "TotalRevenue": "revenue",
    "CostOfRevenue": "cost_of_revenue",
    "GrossProfit": "gross_profit",
    "ResearchAndDevelopment": "research_and_development",
    "SellingGeneralAndAdministration": "selling_general_admin",
    "OperatingIncome": "operating_income",
    "EBIT": "ebit",
    "InterestExpense": "interest_expense",
    "PretaxIncome": "pretax_income",
    "TaxProvision": "tax_provision",
    "NetIncome": "net_income",
    "DepreciationAndAmortization": "depreciation_amortisation",
    "OperatingCashFlow": "operating_cash_flow",
    "CapitalExpenditure": "capital_expenditure",
    "TotalAssets": "total_assets",
    "CurrentAssets": "current_assets",
    "CurrentLiabilities": "current_liabilities",
    "TotalLiabilitiesNetMinorityInterest": "total_liabilities",
    "StockholdersEquity": "stockholders_equity",
    "CashAndCashEquivalents": "cash_and_equivalents",
    "CashCashEquivalentsAndShortTermInvestments": "cash_and_short_term_investments",
    "CurrentDebt": "current_debt",
    "LongTermDebt": "long_term_debt",
    "TotalDebt": "total_debt",
    "NetPPE": "net_ppe",
    "BasicAverageShares": "shares_basic_avg",
    "DilutedAverageShares": "shares_diluted_avg",
    "OrdinarySharesNumber": "shares_outstanding",
}

_PREFIX = {"annual": "A", "quarterly": "Q"}


@dataclass(frozen=True)
class Fact:
    metric_code: str
    period_end: date
    period_type: str
    value: Decimal
    currency: str | None


def _split(key: str) -> tuple[str, str] | None:
    """`annualTotalRevenue` -> ('revenue', 'A'), or None if not ours."""
    for prefix, period_type in _PREFIX.items():
        if key.startswith(prefix):
            code = SERIES.get(key[len(prefix):])
            return (code, period_type) if code else None
    return None


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        # str() first: Decimal(float) preserves the binary error rather than
        # the number the provider meant.
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    # Python's json decoder accepts bare `NaN` and `Infinity`. `numeric` would
    # take a NaN and then every comparison against it is false, which makes a
    # fact that can never be restated because it never equals itself.
    return number if number.is_finite() else None


def parse(payload: bytes) -> list[Fact]:
    """Every fact in one timeseries response, in the order Yahoo gave them."""
    try:
        body = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return []
    results = (body.get("timeseries") or {}).get("result") or []

    out: list[Fact] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        for key, entries in result.items():
            if key in ("meta", "timestamp") or not isinstance(entries, list):
                continue
            split = _split(key)
            if split is None:
                continue
            code, period_type = split
            for entry in entries:
                # Yahoo pads its arrays with nulls. A null becoming 0 would be
                # a fabricated fundamental, and `value` is not null precisely
                # so a missing number cannot be mistaken for a real one.
                if not isinstance(entry, dict):
                    continue
                value = _decimal((entry.get("reportedValue") or {}).get("raw"))
                as_of = entry.get("asOfDate")
                if value is None or not as_of:
                    continue
                try:
                    period_end = date.fromisoformat(as_of)
                except ValueError:
                    continue
                out.append(
                    Fact(code, period_end, period_type, value,
                         entry.get("currencyCode"))
                )
    return out
```

Add to `src/screener/ingest/__init__.py`:

```python
from screener.ingest.facts import SERIES, Fact, parse as parse_facts
```

and extend `__all__` with `"SERIES"`, `"Fact"` and `"parse_facts"`, keeping RUF022 order. **`parse` is already exported from `parse.py`**, so the fundamentals parser is re-exported as `parse_facts` to avoid shadowing it; inside the package it is `facts.parse`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_ingest_facts.py -n0 -v`
Expected: PASS, 13 passed.

- [ ] **Step 5: Typecheck and commit**

```bash
.venv/bin/pyright
git add src/screener/ingest/facts.py src/screener/ingest/__init__.py tests/test_ingest_facts.py
git commit -m "Parse the timeseries envelope into facts, nulls skipped"
```

Expected: `pyright` reports zero errors.

---

### Task 3: `timeseries.py` — the sessionless client

**Files:**
- Create: `src/screener/ingest/timeseries.py`
- Modify: `src/screener/ingest/__init__.py`
- Test: `tests/test_ingest_timeseries.py`

**Interfaces:**
- Consumes: `SERIES` from Task 2.
- Produces:
  - `TYPES: str` — the 56 comma-joined `type=` values.
  - `TimeseriesClient` — context manager with `fetch(symbol: str) -> bytes | None`, constructor `(*, lanes=None, transport=None, sleep=None, backoff=1.0)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_ingest_timeseries.py`:

```python
"""The fundamentals client: one request per security, no session.

Measured before it was written: a cold client with no cookie jar and no crumb
gets 200 from this endpoint. So this shares `LanePool` with `ChartClient` and
shares nothing at all with `screener.universe.sources.yahoo`, whose whole
cookie-and-crumb apparatus serves a different endpoint.
"""

import httpx
import pytest

from screener.ingest import TYPES, TimeseriesClient


def _transport(handler):
    return httpx.MockTransport(handler)


def test_both_prefixes_of_every_stem_are_requested():
    from screener.ingest import SERIES

    assert len(TYPES.split(",")) == len(SERIES) * 2
    assert "annualTotalRevenue" in TYPES
    assert "quarterlyTotalRevenue" in TYPES


def test_no_derivation_is_requested():
    assert "EBITDA" not in TYPES
    assert "FreeCashFlow" not in TYPES


def test_a_fetch_returns_the_body():
    def handler(request):
        assert "symbol=AAPL" in str(request.url)
        assert "type=" in str(request.url)
        return httpx.Response(200, content=b'{"timeseries":{"result":[]}}')

    with TimeseriesClient(transport=_transport(handler)) as client:
        assert client.fetch("AAPL") == b'{"timeseries":{"result":[]}}'


def test_no_crumb_is_sent():
    # The endpoint needs none, and sending one would imply a session this
    # client deliberately does not hold.
    def handler(request):
        assert "crumb" not in str(request.url)
        return httpx.Response(200, content=b"{}")

    with TimeseriesClient(transport=_transport(handler)) as client:
        client.fetch("AAPL")


def test_a_404_is_none_rather_than_an_exception():
    # One delisted symbol is one security's problem, not the night's.
    def handler(request):
        return httpx.Response(404)

    with TimeseriesClient(transport=_transport(handler)) as client:
        assert client.fetch("GONE") is None


def test_a_symbol_with_a_slash_does_not_build_an_invalid_url():
    # `InvalidURL` descends from Exception, not HTTPError, so it escapes the
    # transport guard entirely. The symbol lands in the URL path, so it is
    # escaped there as `chart.py` learned to do.
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, content=b"{}")

    with TimeseriesClient(transport=_transport(handler)) as client:
        client.fetch("BRK/B")

    assert "BRK%2FB" in seen["url"]


def test_a_transport_error_is_none_rather_than_raising():
    def handler(request):
        raise httpx.ConnectError("down")

    with TimeseriesClient(transport=_transport(handler)) as client:
        assert client.fetch("AAPL") is None


def test_a_429_parks_the_lane_and_retries():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429)
        return httpx.Response(200, content=b"{}")

    slept = []
    with TimeseriesClient(
        transport=_transport(handler), sleep=slept.append, backoff=0.01
    ) as client:
        assert client.fetch("AAPL") == b"{}"
    assert calls["n"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ingest_timeseries.py -n0 -v`
Expected: FAIL with `ImportError: cannot import name 'TYPES' from 'screener.ingest'`.

- [ ] **Step 3: Write the implementation**

Create `src/screener/ingest/timeseries.py`:

```python
"""Fundamentals from Yahoo's timeseries endpoint, over a lane.

Measured before this was written: a cold `httpx.Client` with no cookie jar and
no crumb parameter gets 200. So this is `ChartClient`'s shape rather than
`screener.universe.sources.yahoo`'s -- the cookie-and-crumb apparatus exists
because a crumb is only valid alongside the cookie issued with it, and an
endpoint that needs neither should not pay for machinery serving one that does.

`PLAN.md` says the Yahoo path holds one session for the run. That is true of
`quoteSummary` and false here, and the spec's F4 records the measurement.
"""

import time
from collections.abc import Callable
from urllib.parse import quote

import httpx

from screener.fetch import Lane, LanePool
from screener.ingest.facts import SERIES

BASE = "https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries"
TIMEOUT = 40.0
_SPARE_ATTEMPTS = 3

# Far enough back to take everything Yahoo holds. It returns four annual and
# five quarterly periods whatever is asked for, so this is a formality until
# the shape of the response changes.
PERIOD1 = 1420070400  # 2015-01-01
PERIOD2 = 4102444800  # 2100-01-01

BROWSER = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# Both prefixes of every stem, in one request: 28 metrics, 56 values.
TYPES = ",".join(
    f"{prefix}{stem}" for stem in SERIES for prefix in ("annual", "quarterly")
)


class TimeseriesClient:
    def __init__(
        self,
        *,
        lanes: LanePool | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
        backoff: float = 1.0,
    ) -> None:
        self._owned = lanes is None
        self.lanes = lanes or LanePool.from_env(
            headers=BROWSER,
            timeout=TIMEOUT,
            transport=transport,
            fallback_to_direct=True,
        )
        self._sleep = sleep or time.sleep
        self._backoff = backoff

    def close(self) -> None:
        if self._owned:
            self.lanes.close()

    def __enter__(self) -> "TimeseriesClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _request(self, lane: Lane, url: str) -> httpx.Response | None:
        try:
            return lane.get(url)
        except httpx.HTTPError:
            return None

    def fetch(self, symbol: str) -> bytes | None:
        """One security's fundamentals, or None if this security failed."""
        # The symbol lands in the URL *path*, so it is escaped: a symbol
        # carrying a slash builds a URL httpx rejects with `InvalidURL`, which
        # descends from Exception rather than HTTPError and so escapes the
        # guard in `_request` entirely.
        safe = quote(symbol, safe="")
        url = (
            f"{BASE}/{safe}?symbol={safe}&type={TYPES}"
            f"&period1={PERIOD1}&period2={PERIOD2}"
        )
        backoff = self._backoff
        for _ in range(len(self.lanes) + _SPARE_ATTEMPTS):
            lane = self.lanes.acquire()
            if lane.parked_for:
                # Every lane is on cooldown. How long to give a source is the
                # source's business, which is why the wait is here and not in
                # the fetch layer.
                self._sleep(backoff)
                backoff *= 2
            raw = self._request(lane, url)
            if raw is None:
                return None
            if raw.status_code == 429:
                lane.park(backoff)
                backoff *= 2
                continue
            if raw.status_code != 200:
                return None
            return raw.content
        return None
```

Add `from screener.ingest.timeseries import TYPES, TimeseriesClient` to `src/screener/ingest/__init__.py` and extend `__all__`, keeping RUF022 order.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_ingest_timeseries.py -n0 -v`
Expected: PASS, 8 passed. If `lane.get` or `lane.parked_for` do not exist under those names, read `src/screener/fetch/lanes.py` and match `chart.py`'s usage exactly rather than inventing an interface.

- [ ] **Step 5: Typecheck and commit**

```bash
.venv/bin/pyright
git add src/screener/ingest/timeseries.py src/screener/ingest/__init__.py tests/test_ingest_timeseries.py
git commit -m "Fetch fundamentals with no session, because the endpoint needs none"
```

---

### Task 4: `load.py` — the observation's clock, and appending on change

`observed_at` is the column this whole cycle exists to prove correct, so it gets its own task and its own review gate.

**Files:**
- Modify: `src/screener/ingest/load.py` (extend `record_observation`; add `metric_ids`, `latest_values`, `insert_facts`)
- Modify: `src/screener/ingest/run.py:182` (the one existing caller of `record_observation`)
- Modify: `src/screener/ingest/__init__.py`
- Test: `tests/test_ingest_facts_load.py`

**Interfaces:**
- Consumes: `Fact` from Task 2.
- Produces:
  - `record_observation(...) -> tuple[int, datetime]` — **changed from `-> int`**. Returns `(id, fetched_at)`.
  - `metric_ids(cur) -> dict[str, int]` — `metric.code` → id, inputs only.
  - `Held` — frozen dataclass: `fact_id: int`, `value: Decimal`.
  - `latest_values(cur, security_id) -> dict[tuple[str, date, str], Held]` — keyed by `(metric_code, period_end, period_type)`.
  - `insert_facts(cur, security_id, observation_id, observed_at, facts, held, ids) -> int` — returns the number inserted.

- [ ] **Step 1: Write the failing test**

Create `tests/test_ingest_facts_load.py`:

```python
"""Writing facts: the clock, and what counts as new.

The two rules this file exists to hold: every fact from one payload carries its
observation's `fetched_at`, and a fact is inserted only when its value differs
from the latest already held.
"""

from datetime import date
from decimal import Decimal

import pytest

from screener.ingest import Fact, insert_facts, latest_values, metric_ids, record_observation


@pytest.fixture
def ctx(fresh_db):
    """One security, and the run it will hang observations from."""
    security = fresh_db.execute(
        """insert into security
           (name, mic, currency, country, primary_symbol, first_seen)
           values ('Alpha', 'XNAS', 'USD', 'US', 'AAA', '2020-01-01')
           returning id"""
    ).fetchone()[0]
    source = fresh_db.execute(
        "insert into data_source (code, name) values ('yahoo', 'Yahoo') "
        "on conflict (code) do update set name = excluded.name returning id"
    ).fetchone()[0]
    run = fresh_db.execute(
        "insert into ingest_run (source_id, endpoint, started_at, status) "
        "values (%s, 'timeseries', now(), 'running') returning id",
        (source,),
    ).fetchone()[0]
    return security, run


def _observe(conn, run_id, security_id, digest=b"\x01" * 32):
    with conn.cursor() as cur:
        return record_observation(
            cur,
            ingest_run_id=run_id,
            security_id=security_id,
            content_hash=digest,
            blob_path="yahoo/timeseries/2026-09-06/1.json.gz",
            is_new_payload=True,
            payload_bytes=100,
        )


def _fact(code="revenue", period_end=date(2025, 9, 30), period_type="A",
          value="100", currency="USD"):
    return Fact(code, period_end, period_type, Decimal(value), currency)


def test_metric_ids_returns_only_inputs(fresh_db):
    with fresh_db.cursor() as cur:
        ids = metric_ids(cur)
    assert "revenue" in ids and "ret_12m" not in ids
    assert len(ids) == 28


def test_record_observation_returns_its_fetched_at(fresh_db, ctx):
    security, run = ctx
    observation_id, fetched_at = _observe(fresh_db, run, security)

    stored = fresh_db.execute(
        "select fetched_at from ingest_observation where id = %s",
        (observation_id,),
    ).fetchone()[0]
    assert fetched_at == stored


def test_every_fact_from_one_payload_shares_the_observation_clock(fresh_db, ctx):
    # D4. A per-row clock would make two facts from one response sort
    # non-deterministically and let restates_id point at a later row than the
    # one superseding it.
    security, run = ctx
    observation_id, fetched_at = _observe(fresh_db, run, security)
    facts = [
        _fact("revenue", date(2025, 9, 30), "A", "100"),
        _fact("net_income", date(2025, 9, 30), "A", "10"),
        _fact("revenue", date(2025, 6, 30), "Q", "25"),
    ]
    with fresh_db.cursor() as cur:
        ids = metric_ids(cur)
        assert insert_facts(cur, security, observation_id, fetched_at, facts, {}, ids) == 3

    stamps = fresh_db.execute(
        "select distinct observed_at from fundamental_fact"
    ).fetchall()
    assert stamps == [(fetched_at,)]


def test_an_unchanged_value_inserts_nothing(fresh_db, ctx):
    security, run = ctx
    first_id, first_at = _observe(fresh_db, run, security)
    with fresh_db.cursor() as cur:
        ids = metric_ids(cur)
        insert_facts(cur, security, first_id, first_at, [_fact()], {}, ids)

    second_id, second_at = _observe(fresh_db, run, security)
    with fresh_db.cursor() as cur:
        held = latest_values(cur, security)
        inserted = insert_facts(
            cur, security, second_id, second_at, [_fact()], held, ids
        )

    assert inserted == 0
    assert fresh_db.execute(
        "select count(*) from fundamental_fact"
    ).fetchone()[0] == 1


def test_a_changed_value_inserts_and_points_at_what_it_supersedes(fresh_db, ctx):
    security, run = ctx
    first_id, first_at = _observe(fresh_db, run, security)
    with fresh_db.cursor() as cur:
        ids = metric_ids(cur)
        insert_facts(cur, security, first_id, first_at, [_fact(value="100")], {}, ids)
        original = cur.execute(
            "select id from fundamental_fact"
        ).fetchone()[0]

    second_id, second_at = _observe(fresh_db, run, security, digest=b"\x02" * 32)
    with fresh_db.cursor() as cur:
        held = latest_values(cur, security)
        assert insert_facts(
            cur, security, second_id, second_at, [_fact(value="110")], held, ids
        ) == 1

    rows = fresh_db.execute(
        "select value, restates_id from fundamental_fact order by observed_at"
    ).fetchall()
    assert [r[0] for r in rows] == [Decimal("100"), Decimal("110")]
    assert rows[0][1] is None
    assert rows[1][1] == original


def test_a_value_that_reverts_inserts_a_third_row(fresh_db, ctx):
    # A -> B -> A. The comparison is against the latest observation, not
    # against every value ever seen, so Wednesday must insert.
    security, run = ctx
    with fresh_db.cursor() as cur:
        ids = metric_ids(cur)
    for value in ("100", "110", "100"):
        obs_id, obs_at = _observe(fresh_db, run, security)
        with fresh_db.cursor() as cur:
            held = latest_values(cur, security)
            insert_facts(
                cur, security, obs_id, obs_at, [_fact(value=value)], held, ids
            )

    rows = fresh_db.execute(
        "select value from fundamental_fact order by observed_at, id"
    ).fetchall()
    assert [r[0] for r in rows] == [Decimal("100"), Decimal("110"), Decimal("100")]


def test_a_quarter_and_a_year_on_one_date_are_two_facts(fresh_db, ctx):
    # F5, at the write layer: they differ only in period_type, and the key
    # `latest_values` uses has to keep them apart.
    security, run = ctx
    obs_id, obs_at = _observe(fresh_db, run, security)
    with fresh_db.cursor() as cur:
        ids = metric_ids(cur)
        inserted = insert_facts(
            cur, security, obs_id, obs_at,
            [
                _fact("revenue", date(2025, 9, 30), "A", "416161"),
                _fact("revenue", date(2025, 9, 30), "Q", "102466"),
            ],
            {}, ids,
        )
    assert inserted == 2

    with fresh_db.cursor() as cur:
        held = latest_values(cur, security)
    assert len(held) == 2
    assert held[("revenue", date(2025, 9, 30), "A")].value == Decimal("416161")
    assert held[("revenue", date(2025, 9, 30), "Q")].value == Decimal("102466")


def test_the_currency_is_stored_as_given(fresh_db, ctx):
    security, run = ctx
    obs_id, obs_at = _observe(fresh_db, run, security)
    with fresh_db.cursor() as cur:
        ids = metric_ids(cur)
        insert_facts(
            cur, security, obs_id, obs_at,
            [_fact(currency="GBP"), _fact("net_income", currency=None)],
            {}, ids,
        )

    rows = fresh_db.execute(
        "select currency from fundamental_fact order by currency nulls last"
    ).fetchall()
    assert rows == [("GBP",), (None,)]


def test_a_fact_naming_an_unknown_metric_is_skipped(fresh_db, ctx):
    # The parser only emits codes from SERIES, so this cannot happen from a
    # real payload -- but a not-null foreign key failing mid-night would take
    # the security down, and skipping is the cheaper contract.
    security, run = ctx
    obs_id, obs_at = _observe(fresh_db, run, security)
    with fresh_db.cursor() as cur:
        ids = metric_ids(cur)
        assert insert_facts(
            cur, security, obs_id, obs_at, [_fact("no_such_metric")], {}, ids
        ) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ingest_facts_load.py -n0 -v`
Expected: FAIL with `ImportError: cannot import name 'insert_facts' from 'screener.ingest'`.

- [ ] **Step 3: Change `record_observation` to return its clock**

In `src/screener/ingest/load.py`, change the function's return annotation to `tuple[int, datetime]`, its query to `returning id, fetched_at`, and its return statement to `return row[0], row[1]`. Add `from datetime import datetime` to the imports if absent. Extend the docstring:

```python
    """Always written, even when the payload was unchanged (schema D4).

    Dropping it when nothing changed would lose the record of what was known on
    a date, which is the whole point of the trail.

    Returns `fetched_at` alongside the id because the fundamentals path stamps
    every fact from one payload with it: `observed_at` has to be one value per
    observation, not a clock read per row, or two facts from one response sort
    non-deterministically against each other and a restatement chain can point
    forwards in time.
    """
```

In `src/screener/ingest/run.py` around line 182, change the call site:

```python
                    observation_id, _ = record_observation(
```

The price path does not use the timestamp — it writes `now()` inline in its own inserts, which is correct for a table with no restatement chain.

- [ ] **Step 4: Add the three new functions**

Append to `src/screener/ingest/load.py`:

```python
@dataclass(frozen=True)
class Held:
    """The latest fact held for one (metric, period_end, period_type)."""

    fact_id: int
    value: Decimal


def metric_ids(cur: psycopg.Cursor) -> dict[str, int]:
    """`metric.code` -> id, for the input metrics only.

    Filtered on `is_input` rather than returning everything, so a fact naming a
    *scored* metric — which would mean a ratio had been stored, against D3 —
    cannot be written by accident.
    """
    cur.execute("select code, id from metric where is_input")
    return {row[0]: row[1] for row in cur.fetchall()}


def latest_values(
    cur: psycopg.Cursor, security_id: int
) -> dict[tuple[str, date, str], Held]:
    """What we already hold for this security, latest observation per period.

    Keyed by `(metric_code, period_end, period_type)` — `period_type` is in the
    key because a fiscal Q4 ends when its fiscal year does, so without it a
    year and its own last quarter collapse into one entry and every night would
    see one of them as changed.
    """
    cur.execute(
        """select distinct on (f.metric_id, f.period_end, f.period_type)
                  m.code, f.period_end, f.period_type, f.id, f.value
             from fundamental_fact f
             join metric m on m.id = f.metric_id
            where f.security_id = %s
         order by f.metric_id, f.period_end, f.period_type, f.observed_at desc""",
        (security_id,),
    )
    return {
        (code, period_end, period_type): Held(fact_id, value)
        for code, period_end, period_type, fact_id, value in cur.fetchall()
    }


def insert_facts(
    cur: psycopg.Cursor,
    security_id: int,
    observation_id: int,
    observed_at: datetime,
    facts: Sequence[Fact],
    held: Mapping[tuple[str, date, str], Held],
    ids: Mapping[str, int],
) -> int:
    """Insert only the facts whose value differs from what is held.

    `observed_at` is the caller's — the observation's `fetched_at` — and is the
    same for every fact from one payload. Never `now()` per row.

    A fact whose value matches writes nothing at all: not a row, and (upstream)
    not a blob. The observation is what records that we looked.
    """
    rows = []
    for fact in facts:
        metric_id = ids.get(fact.metric_code)
        if metric_id is None:
            # The parser only emits codes from SERIES, so this is unreachable
            # from a real payload. Skipping rather than raising keeps a seed
            # that has drifted from costing a whole security.
            continue
        key = (fact.metric_code, fact.period_end, fact.period_type)
        previous = held.get(key)
        if previous is not None and previous.value == fact.value:
            continue
        rows.append(
            (
                security_id, metric_id, fact.period_end, fact.period_type,
                fact.value, fact.currency, observed_at, observation_id,
                previous.fact_id if previous else None,
            )
        )
    if not rows:
        return 0
    cur.executemany(
        """insert into fundamental_fact
           (security_id, metric_id, period_end, period_type, value, currency,
            observed_at, ingest_observation_id, restates_id)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           on conflict (security_id, metric_id, period_end, period_type,
                        observed_at) do nothing""",
        rows,
    )
    return len(rows)
```

Add the imports `facts.py` needs at the top of `load.py`: `from collections.abc import Mapping, Sequence`, `from datetime import datetime`, and `from screener.ingest.facts import Fact`.

Add `from screener.ingest.load import Held, insert_facts, latest_values, metric_ids` to `src/screener/ingest/__init__.py` and extend `__all__`.

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_ingest_facts_load.py tests/test_ingest_load.py tests/test_ingest_run.py -n0 -v`
Expected: PASS. `tests/test_ingest_load.py` may need its `record_observation` call updated to unpack a tuple — if it does, that is this task's change to make.

- [ ] **Step 6: Whole suite, typecheck, commit**

```bash
.venv/bin/python -m pytest -n 4
.venv/bin/pyright
git add src/screener/ingest/load.py src/screener/ingest/run.py src/screener/ingest/__init__.py tests/test_ingest_facts_load.py tests/test_ingest_load.py
git commit -m "Stamp every fact with its observation's clock, and append only on change"
```

---

### Task 5: `read_facts` — the point-in-time read

The task the cycle exists for. Everything before it writes rows; this is what makes "stored correctly" a checkable claim rather than a tautology.

**Files:**
- Modify: `src/screener/ingest/load.py` (add `read_facts`, `HeldFact`)
- Modify: `src/screener/ingest/__init__.py`
- Test: `tests/test_ingest_point_in_time.py`

**Interfaces:**
- Consumes: `visibility_cutoff` from `screener.scoring`.
- Produces:
  - `HeldFact` — frozen dataclass: `metric_code: str`, `period_end: date`, `period_type: str`, `value: Decimal`, `currency: str | None`, `observed_at: datetime`.
  - `read_facts(conn, security_ids, *, as_of, cutoff_offset) -> dict[int, list[HeldFact]]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_ingest_point_in_time.py`:

```python
"""What a scoring date may see of the fact layer.

The bitemporal claim in one file: `observed_at` bounds visibility, a
restatement is an insert rather than an update, and a fiscal year does not
collapse into its own fourth quarter.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from screener.ingest import Fact, insert_facts, latest_values, metric_ids, read_facts, record_observation
from screener.scoring import CUTOFF_OFFSET

AS_OF = date(2026, 3, 2)


@pytest.fixture
def facts_ctx(fresh_db):
    security = fresh_db.execute(
        """insert into security
           (name, mic, currency, country, primary_symbol, first_seen)
           values ('Alpha', 'XNAS', 'USD', 'US', 'AAA', '2020-01-01')
           returning id"""
    ).fetchone()[0]
    source = fresh_db.execute(
        "insert into data_source (code, name) values ('yahoo', 'Yahoo') "
        "on conflict (code) do update set name = excluded.name returning id"
    ).fetchone()[0]
    run = fresh_db.execute(
        "insert into ingest_run (source_id, endpoint, started_at, status) "
        "values (%s, 'timeseries', now(), 'running') returning id",
        (source,),
    ).fetchone()[0]
    return security, run


def _write(conn, run_id, security_id, facts, at: datetime):
    """Write facts stamped at `at`, the way a night at that moment would."""
    with conn.cursor() as cur:
        cur.execute(
            """insert into ingest_observation
               (ingest_run_id, security_id, fetched_at, content_hash,
                blob_path, is_new_payload, payload_bytes)
               values (%s, %s, %s, %s, 'p', true, 1) returning id""",
            (run_id, security_id, at, b"\x00" * 32),
        )
        observation_id = cur.fetchone()[0]
        ids = metric_ids(cur)
        held = latest_values(cur, security_id)
        insert_facts(cur, security_id, observation_id, at, facts, held, ids)


def _fact(value, code="revenue", period_end=date(2025, 12, 31), period_type="Q"):
    return Fact(code, period_end, period_type, Decimal(value), "USD")


def test_a_restatement_is_visible_only_after_it_was_observed(fresh_db, facts_ctx):
    """The load-bearing test of the cycle.

    Q2 is reported, then revised. A read as-of a date between the two must
    return the original -- what we actually knew then -- and a read as-of today
    must return the revision.
    """
    security, run = facts_ctx
    monday = datetime(2026, 1, 5, 2, 0, tzinfo=timezone.utc)
    friday = datetime(2026, 2, 20, 2, 0, tzinfo=timezone.utc)
    _write(fresh_db, run, security, [_fact("100")], monday)
    _write(fresh_db, run, security, [_fact("115")], friday)

    early = read_facts(
        fresh_db, [security], as_of=date(2026, 1, 20), cutoff_offset=CUTOFF_OFFSET
    )
    late = read_facts(
        fresh_db, [security], as_of=AS_OF, cutoff_offset=CUTOFF_OFFSET
    )

    assert [f.value for f in early[security]] == [Decimal("100")]
    assert [f.value for f in late[security]] == [Decimal("115")]


def test_a_year_does_not_collapse_into_its_own_fourth_quarter(fresh_db, facts_ctx):
    """D6, against the shape that produced it.

    AAPL reports FY2025 revenue and Q4 FY2025 revenue both ending 2025-09-30,
    four-fold apart. `distinct on (security_id, metric_id, period_end)` -- the
    read the schema spec documents -- returns one of them and silently discards
    the other.
    """
    security, run = facts_ctx
    at = datetime(2026, 1, 5, 2, 0, tzinfo=timezone.utc)
    _write(
        fresh_db, run, security,
        [
            _fact("416161", period_end=date(2025, 9, 30), period_type="A"),
            _fact("102466", period_end=date(2025, 9, 30), period_type="Q"),
        ],
        at,
    )

    held = read_facts(
        fresh_db, [security], as_of=AS_OF, cutoff_offset=CUTOFF_OFFSET
    )[security]

    assert len(held) == 2
    by_type = {f.period_type: f.value for f in held}
    assert by_type == {"A": Decimal("416161"), "Q": Decimal("102466")}


def test_a_fact_observed_after_the_cutoff_is_not_visible(fresh_db, facts_ctx):
    security, run = facts_ctx
    inside = datetime(2026, 3, 3, 5, 0, tzinfo=timezone.utc)
    outside = datetime(2026, 3, 3, 7, 0, tzinfo=timezone.utc)
    _write(fresh_db, run, security, [_fact("100")], inside)
    _write(fresh_db, run, security, [_fact("999")], outside)

    held = read_facts(
        fresh_db, [security], as_of=AS_OF, cutoff_offset=CUTOFF_OFFSET
    )[security]

    assert [f.value for f in held] == [Decimal("100")]


def test_a_withdrawn_value_leaves_the_last_reported_figure_standing(
    fresh_db, facts_ctx
):
    # D11's second half. Yahoo reporting nothing for a period it once reported
    # writes no row, so the last figure stays the point-in-time answer. That is
    # intended -- an absence is not a restatement -- and this pins it so the
    # behaviour cannot change silently.
    security, run = facts_ctx
    _write(
        fresh_db, run, security, [_fact("100")],
        datetime(2026, 1, 5, 2, 0, tzinfo=timezone.utc),
    )
    _write(
        fresh_db, run, security, [],
        datetime(2026, 2, 5, 2, 0, tzinfo=timezone.utc),
    )

    held = read_facts(
        fresh_db, [security], as_of=AS_OF, cutoff_offset=CUTOFF_OFFSET
    )[security]

    assert [f.value for f in held] == [Decimal("100")]


def test_every_period_of_a_metric_comes_back(fresh_db, facts_ctx):
    # The read is per period, not "the latest period" -- a TTM computed later
    # needs four consecutive quarters, so it cannot collapse to one row.
    security, run = facts_ctx
    at = datetime(2026, 1, 5, 2, 0, tzinfo=timezone.utc)
    _write(
        fresh_db, run, security,
        [
            _fact("10", period_end=date(2025, 3, 31)),
            _fact("11", period_end=date(2025, 6, 30)),
            _fact("12", period_end=date(2025, 9, 30)),
            _fact("13", period_end=date(2025, 12, 31)),
        ],
        at,
    )

    held = read_facts(
        fresh_db, [security], as_of=AS_OF, cutoff_offset=CUTOFF_OFFSET
    )[security]

    assert len(held) == 4
    assert sorted(f.value for f in held) == [
        Decimal("10"), Decimal("11"), Decimal("12"), Decimal("13")
    ]


def test_a_security_with_no_visible_facts_is_absent_from_the_mapping(
    fresh_db, facts_ctx
):
    security, _ = facts_ctx
    assert read_facts(
        fresh_db, [security], as_of=AS_OF, cutoff_offset=CUTOFF_OFFSET
    ) == {}


def test_asking_about_nothing_returns_nothing(fresh_db):
    assert read_facts(
        fresh_db, [], as_of=AS_OF, cutoff_offset=CUTOFF_OFFSET
    ) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ingest_point_in_time.py -n0 -v`
Expected: FAIL with `ImportError: cannot import name 'read_facts' from 'screener.ingest'`.

- [ ] **Step 3: Write the implementation**

Append to `src/screener/ingest/load.py`:

```python
@dataclass(frozen=True)
class HeldFact:
    metric_code: str
    period_end: date
    period_type: str
    value: Decimal
    currency: str | None
    observed_at: datetime


def read_facts(
    conn: psycopg.Connection,
    security_ids: Sequence[int],
    *,
    as_of: date,
    cutoff_offset: timedelta,
) -> dict[int, list[HeldFact]]:
    """What each security's facts were, as known on `as_of`.

    The bitemporal read, and the only thing that can show the writes were
    correct: rows existing proves nothing, because a test asserting the fields
    it just passed in is a tautology.

    **`period_type` is in the `distinct on` and it has to be.** A fiscal Q4
    ends on the same date as its fiscal year -- AAPL reports both at 2025-09-30,
    four-fold apart -- so the read documented in the schema spec,
    `distinct on (security_id, metric_id, period_end)`, returns whichever was
    inserted later and silently discards the other. For every company, every
    year. Migration 021 puts `period_type` into the index too, in the order this
    `order by` uses, so the `distinct on` is satisfied without a sort.

    `cutoff` comes from `screener.scoring.visibility_cutoff` rather than being
    recomputed here, so prices and fundamentals answer to one definition of
    what a scoring date may see.
    """
    if not security_ids:
        return {}
    # Imported here rather than at module scope: `screener.scoring` imports
    # nothing from `screener.ingest`, and keeping the edge one-directional at
    # the top of the file would be a lie about a dependency that only exists
    # inside this function.
    from screener.scoring import visibility_cutoff

    out: dict[int, list[HeldFact]] = {}
    with conn.cursor() as cur:
        cur.execute(
            """select distinct on (f.security_id, f.metric_id, f.period_end,
                                   f.period_type)
                      f.security_id, m.code, f.period_end, f.period_type,
                      f.value, f.currency, f.observed_at
                 from fundamental_fact f
                 join metric m on m.id = f.metric_id
                where f.security_id = any(%(ids)s)
                  and f.observed_at <= %(cutoff)s
             order by f.security_id, f.metric_id, f.period_end, f.period_type,
                      f.observed_at desc""",
            {
                "ids": list(security_ids),
                "cutoff": visibility_cutoff(as_of, cutoff_offset),
            },
        )
        for security_id, code, period_end, period_type, value, currency, observed_at in cur.fetchall():
            out.setdefault(security_id, []).append(
                HeldFact(code, period_end, period_type, value, currency, observed_at)
            )
    return out
```

Add `from datetime import timedelta` to `load.py`'s imports if absent, and `from screener.ingest.load import HeldFact, read_facts` to `src/screener/ingest/__init__.py`, extending `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_ingest_point_in_time.py -n0 -v`
Expected: PASS, 7 passed.

- [ ] **Step 5: Prove the index is used rather than assumed**

Run:

```bash
docker exec stock-aggregator-postgres-1 psql -U postgres -d screener_test -c "
explain (costs off)
select distinct on (f.security_id, f.metric_id, f.period_end, f.period_type)
       f.security_id, f.value
  from fundamental_fact f
 where f.security_id = any(array[1])
   and f.observed_at <= now()
 order by f.security_id, f.metric_id, f.period_end, f.period_type,
          f.observed_at desc"
```

Expected: the plan names `fundamental_fact_pit_idx2` and contains **no `Sort` node**. An empty table may plan a sequential scan regardless — if so, note that in the task report rather than treating it as a failure; the column-order claim is what matters and it is checked by reading the plan, not by the row count.

- [ ] **Step 6: Whole suite, typecheck, commit**

```bash
.venv/bin/python -m pytest -n 4
.venv/bin/pyright
git add src/screener/ingest/load.py src/screener/ingest/__init__.py tests/test_ingest_point_in_time.py
git commit -m "Read the fact layer as it was known, period_type included"
```

---

### Task 6: `run.py` — one night of fundamentals

**Files:**
- Modify: `src/screener/ingest/run.py` (add `FundamentalsReport`, `run_fundamentals`)
- Modify: `src/screener/ingest/__init__.py`
- Test: `tests/test_ingest_fundamentals_run.py`

**Interfaces:**
- Consumes: `TimeseriesClient` (Task 3), `facts.parse` (Task 2), `insert_facts`/`latest_values`/`metric_ids`/`record_observation` (Task 4).
- Produces:
  - `FundamentalsReport` — dataclass: `requested: int`, `ok: int`, `failed: int`, `facts_written: int`, plus `status` property returning `"ok" | "partial" | "failed"`.
  - `run_fundamentals(conn, *, client, blobs, today, securities, run_id=None, delay=0.0) -> FundamentalsReport`

- [ ] **Step 1: Write the failing test**

Create `tests/test_ingest_fundamentals_run.py`:

```python
"""One night of fundamentals, against a real database and a fake client."""

import json
from datetime import date
from decimal import Decimal

import pytest

from screener.blobs import BlobWriteFailed
from screener.ingest import run_fundamentals

TODAY = date(2026, 9, 6)


def _payload(revenue="100", period_end="2025-09-30"):
    return json.dumps({
        "timeseries": {"result": [{
            "meta": {"type": ["annualTotalRevenue"]},
            "annualTotalRevenue": [{
                "asOfDate": period_end,
                "reportedValue": {"raw": float(revenue)},
                "currencyCode": "USD",
            }],
        }]}
    }).encode()


class FakeClient:
    def __init__(self, bodies):
        self.bodies = bodies
        self.asked = []

    def fetch(self, symbol):
        self.asked.append(symbol)
        body = self.bodies.get(symbol, b"")
        if isinstance(body, Exception):
            raise body
        return body


class FakeBlobs:
    def __init__(self, fail=False):
        self.written = {}
        self.fail = fail

    def put(self, path, data):
        if self.fail:
            raise BlobWriteFailed(path)
        self.written[path] = data


@pytest.fixture
def two(fresh_db):
    out = []
    for name, symbol in (("Alpha", "AAA"), ("Beta", "BBB")):
        out.append((
            fresh_db.execute(
                """insert into security
                   (name, mic, currency, country, primary_symbol, first_seen)
                   values (%s, 'XNAS', 'USD', 'US', %s, '2020-01-01')
                   returning id""",
                (name, symbol),
            ).fetchone()[0],
            symbol,
        ))
    return out


def test_a_night_writes_facts_and_an_observation_each(fresh_db, two):
    client = FakeClient({"AAA": _payload("100"), "BBB": _payload("200")})

    report = run_fundamentals(
        fresh_db, client=client, blobs=FakeBlobs(), today=TODAY, securities=two
    )

    assert (report.requested, report.ok, report.failed) == (2, 2, 0)
    assert report.facts_written == 2
    assert fresh_db.execute(
        "select count(*) from ingest_observation"
    ).fetchone()[0] == 2
    assert fresh_db.execute(
        "select endpoint from ingest_run"
    ).fetchone()[0] == "timeseries"


def test_a_second_identical_night_writes_observations_but_no_facts(fresh_db, two):
    bodies = {"AAA": _payload("100"), "BBB": _payload("200")}
    for _ in range(2):
        run_fundamentals(
            fresh_db, client=FakeClient(bodies), blobs=FakeBlobs(),
            today=TODAY, securities=two,
        )

    assert fresh_db.execute(
        "select count(*) from fundamental_fact"
    ).fetchone()[0] == 2
    assert fresh_db.execute(
        "select count(*) from ingest_observation"
    ).fetchone()[0] == 4


def test_every_fact_carries_its_observation_fetched_at(fresh_db, two):
    run_fundamentals(
        fresh_db, client=FakeClient({"AAA": _payload(), "BBB": _payload()}),
        blobs=FakeBlobs(), today=TODAY, securities=two,
    )

    mismatched = fresh_db.execute(
        """select count(*) from fundamental_fact f
             join ingest_observation o on o.id = f.ingest_observation_id
            where f.observed_at <> o.fetched_at"""
    ).fetchone()[0]
    assert mismatched == 0


def test_an_unchanged_payload_writes_no_blob(fresh_db, two):
    bodies = {"AAA": _payload(), "BBB": _payload()}
    blobs = FakeBlobs()
    run_fundamentals(fresh_db, client=FakeClient(bodies), blobs=blobs,
                     today=TODAY, securities=two)
    first = len(blobs.written)

    run_fundamentals(fresh_db, client=FakeClient(bodies), blobs=blobs,
                     today=TODAY, securities=two)

    assert len(blobs.written) == first
    # And the observation still names the object that *was* written.
    paths = fresh_db.execute(
        "select distinct blob_path from ingest_observation"
    ).fetchall()
    assert {p[0] for p in paths} <= set(blobs.written)


def test_one_securitys_failure_does_not_end_the_night(fresh_db, two):
    client = FakeClient({"AAA": _payload(), "BBB": None})

    report = run_fundamentals(
        fresh_db, client=client, blobs=FakeBlobs(), today=TODAY, securities=two
    )

    assert (report.ok, report.failed) == (1, 1)
    assert report.status == "partial"


def test_an_empty_body_is_a_failure_not_a_security_with_no_facts(fresh_db, two):
    client = FakeClient({"AAA": b"", "BBB": _payload()})

    report = run_fundamentals(
        fresh_db, client=client, blobs=FakeBlobs(), today=TODAY, securities=two
    )

    assert report.failed == 1


def test_a_blob_failure_ends_the_run(fresh_db, two):
    # Systemic rather than per-object: continuing would mean observation rows
    # naming objects that were never stored.
    with pytest.raises(BlobWriteFailed):
        run_fundamentals(
            fresh_db, client=FakeClient({"AAA": _payload(), "BBB": _payload()}),
            blobs=FakeBlobs(fail=True), today=TODAY, securities=two,
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ingest_fundamentals_run.py -n0 -v`
Expected: FAIL with `ImportError: cannot import name 'run_fundamentals'`.

- [ ] **Step 3: Write the implementation**

Append to `src/screener/ingest/run.py`:

```python
FUNDAMENTALS_ENDPOINT = "timeseries"


@dataclass
class FundamentalsReport:
    requested: int = 0
    ok: int = 0
    failed: int = 0
    facts_written: int = 0

    @property
    def status(self) -> str:
        if self.ok == 0 and self.requested:
            return "failed"
        return "ok" if self.failed == 0 else "partial"


def open_fundamentals_run(conn: psycopg.Connection, requested: int) -> int:
    """Its own run row.

    `ingest_run.endpoint` is a single text column, so one row cannot describe
    two endpoints — and the halves should not share one anyway: they fail
    independently, and a partial fundamentals night must not touch the
    per-security window derivation that makes a partial *price* night heal.
    """
    with conn.cursor() as cur:
        cur.execute(
            """insert into ingest_run
               (source_id, endpoint, started_at, status, securities_requested)
               values (%s, %s, now(), 'running', %s) returning id""",
            (source_id(conn), FUNDAMENTALS_ENDPOINT, requested),
        )
        row = cur.fetchone()
        assert row is not None
        return row[0]


def run_fundamentals(
    conn: psycopg.Connection,
    *,
    client,
    blobs: BlobStore,
    today: date,
    securities: list[tuple[int, str]],
    run_id: int | None = None,
    delay: float = 0.0,
) -> FundamentalsReport:
    """One night of fundamentals. One transaction per security, as prices does."""
    from screener.ingest.facts import parse as parse_facts
    from screener.ingest.load import insert_facts, latest_values, metric_ids

    report = FundamentalsReport(requested=len(securities))
    owned_run = run_id is None
    run_id = run_id if run_id is not None else open_fundamentals_run(conn, len(securities))

    with conn.cursor() as cur:
        ids = metric_ids(cur)

    for security_id, symbol in securities:
        if delay:
            time.sleep(delay)
        try:
            payload = client.fetch(symbol)
            if payload is None or len(payload) == 0:
                # An empty body cannot be parsed into anything meaningful, so
                # it is a failed security rather than a security with no facts.
                # Treating it as success would record that we looked and
                # learned nothing, which is a different and untrue claim.
                report.failed += 1
                logger.warning("no fundamentals for %s", symbol)
                continue

            content_hash = hashlib.sha256(payload).digest()
            with conn.cursor() as cur:
                previous = previous_hash(cur, security_id, FUNDAMENTALS_ENDPOINT)

            if previous is not None and previous[0] == content_hash:
                is_new = False
                path = previous[1]
            else:
                is_new = True
                path = blob_path(SOURCE, FUNDAMENTALS_ENDPOINT, today, security_id)
                blobs.put(path, gzip.compress(payload))

            parsed = parse_facts(payload)
            with conn.transaction():
                with conn.cursor() as cur:
                    observation_id, fetched_at = record_observation(
                        cur,
                        ingest_run_id=run_id,
                        security_id=security_id,
                        content_hash=content_hash,
                        blob_path=path,
                        is_new_payload=is_new,
                        payload_bytes=len(payload),
                    )
                    held = latest_values(cur, security_id)
                    report.facts_written += insert_facts(
                        cur, security_id, observation_id, fetched_at,
                        parsed, held, ids,
                    )
        except BlobWriteFailed:
            # Systemic rather than per-object, as in the price path: a store
            # that cannot be written to would leave ~1,500 observation rows
            # naming objects that do not exist.
            raise
        except (httpx.HTTPError, httpx.InvalidURL, psycopg.Error) as exc:
            report.failed += 1
            logger.warning(
                "%s failed (%s: %s)", symbol, type(exc).__name__, exc
            )
            continue
        report.ok += 1

    if owned_run:
        close_run(conn, run_id, report)
    return report
```

`previous_hash` currently hard-codes `r.endpoint = 'chart'`. Change its signature to `previous_hash(cur, security_id, endpoint)` and its predicate to `r.endpoint = %s`, updating the one price-path caller to pass `ENDPOINT`. Without this the fundamentals path would compare against the price payload's hash and write a blob every night.

`close_run` takes a report with `.status` and `.ok`, which `FundamentalsReport` provides — no change needed there.

Add `from screener.ingest.run import FundamentalsReport, run_fundamentals` to `src/screener/ingest/__init__.py` and extend `__all__`.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_ingest_fundamentals_run.py tests/test_ingest_run.py -n0 -v`
Expected: PASS.

- [ ] **Step 5: Whole suite, typecheck, commit**

```bash
.venv/bin/python -m pytest -n 4
.venv/bin/pyright
git add src/screener/ingest/run.py src/screener/ingest/load.py src/screener/ingest/__init__.py tests/test_ingest_fundamentals_run.py
git commit -m "Run a night of fundamentals, one transaction per security"
```

---

### Task 7: `cli.py` — `python -m screener.ingest fundamentals`

**Files:**
- Modify: `src/screener/ingest/cli.py`
- Test: `tests/test_ingest_cli.py`

**Interfaces:**
- Consumes: `run_fundamentals`, `TimeseriesClient`.
- Produces: the `fundamentals` choice on the existing parser.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ingest_cli.py`:

```python
def test_fundamentals_is_a_command(capsys):
    import pytest

    from screener.ingest.cli import main

    with pytest.raises(SystemExit):
        main(["--help"])
    assert "fundamentals" in capsys.readouterr().out


def test_fundamentals_accepts_the_same_flags_as_prices():
    from screener.ingest.cli import build_parser

    parsed = build_parser().parse_args(["fundamentals", "--limit", "5"])
    assert parsed.command == "fundamentals"
    assert parsed.limit == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ingest_cli.py -n0 -v`
Expected: FAIL — argparse rejects `fundamentals` as an invalid choice.

- [ ] **Step 3: Wire the command**

In `src/screener/ingest/cli.py`, add `"fundamentals"` to the `choices` tuple and extend the `help` string with:

```
fundamentals: fetch every line item Yahoo reports per period and store what changed.
```

Then, inside the `with psycopg.connect(...)` block, add a branch before the `prices` one. It opens its own client, because `TimeseriesClient` and `ChartClient` are different clients for different endpoints:

```python
            if args.command == "fundamentals":
                with TimeseriesClient() as fundamentals_client:
                    report = run_fundamentals(
                        conn,
                        client=fundamentals_client,
                        blobs=store(),
                        today=today,
                        securities=securities,
                        delay=args.delay,
                    )
                logger.info(
                    "fundamentals: %d requested, %d ok, %d failed, %d facts written",
                    report.requested, report.ok, report.failed, report.facts_written,
                )
                return 0 if report.ok else 1
```

Add `TimeseriesClient` and `run_fundamentals` to the imports at the top of `cli.py`. Note the existing `with ChartClient() as client:` block wraps the prices and sweep branches — the fundamentals branch must sit *outside* it, so a fundamentals run does not open a chart client it never uses.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_ingest_cli.py -n0 -v`
Expected: PASS.

- [ ] **Step 5: Whole suite, typecheck, commit**

```bash
.venv/bin/python -m pytest -n 4
.venv/bin/pyright
git add src/screener/ingest/cli.py tests/test_ingest_cli.py
git commit -m "Wire `python -m screener.ingest fundamentals`"
```

---

### Task 8: Prove it against the real universe, once, by hand

Not a test — a one-off check before the docs claim it works. Nothing is committed from this task except numbers quoted in Task 9.

**Files:** none.

- [ ] **Step 1: Build a scratch database with the universe loaded**

```bash
docker exec stock-aggregator-postgres-1 psql -U postgres -c "create database screener_fund"
export DATABASE_URL="postgresql://postgres:screener@localhost:5432/screener_fund"
.venv/bin/python -m screener.boot migrate
.venv/bin/python -m screener.universe load
```

Expected: migrations through `021_fundamental_metrics`, and 1,504 securities.

- [ ] **Step 2: A smoke run first**

```bash
.venv/bin/python -m screener.ingest fundamentals --limit 20
```

Expected: `20 requested, 20 ok, 0 failed, N facts written`, N in the low thousands — 28 metrics × up to 9 periods × 20 securities is the ceiling, and F7's patchy coverage puts the real number below it.

- [ ] **Step 3: Run it twice and confirm the second night writes no facts**

```bash
.venv/bin/python -m screener.ingest fundamentals --limit 20
docker exec stock-aggregator-postgres-1 psql -U postgres -d screener_fund -c "
select (select count(*) from fundamental_fact) facts,
       (select count(*) from ingest_observation
         where ingest_run_id in (select id from ingest_run where endpoint='timeseries')) obs"
```

Expected: `facts` unchanged from step 2, `obs` doubled to 40. This is D4 working against the real endpoint rather than a fixture.

- [ ] **Step 4: Confirm the Q4/FY collision exists in the real data**

```bash
docker exec stock-aggregator-postgres-1 psql -U postgres -d screener_fund -c "
select m.code, f.period_end, count(*) filter (where f.period_type='A') annual,
       count(*) filter (where f.period_type='Q') quarterly
  from fundamental_fact f join metric m on m.id = f.metric_id
 group by m.code, f.period_end
having count(distinct f.period_type) > 1
 limit 5"
```

Expected: rows. Every one is a period end where a year and a quarter coexist — the shape D6 exists for, now present in real stored data rather than argued from AAPL.

- [ ] **Step 5: Confirm `observed_at` agrees with `fetched_at` everywhere**

```bash
docker exec stock-aggregator-postgres-1 psql -U postgres -d screener_fund -c "
select count(*) from fundamental_fact f
  join ingest_observation o on o.id = f.ingest_observation_id
 where f.observed_at <> o.fetched_at"
```

Expected: `0`.

- [ ] **Step 6: Run the full night, and record what it cost**

```bash
time .venv/bin/python -m screener.ingest fundamentals
docker exec stock-aggregator-postgres-1 psql -U postgres -d screener_fund -c "
select count(*) facts, count(distinct security_id) securities,
       count(*) filter (where currency is null) no_currency
  from fundamental_fact"
```

Record the wall-clock time, the ok/failed counts, the fact total and the securities covered for Task 9. Then drop the scratch database:

```bash
docker exec stock-aggregator-postgres-1 psql -U postgres -c "drop database screener_fund"
```

---

### Task 9: Documentation, and the errata this cycle owes

**Files:**
- Modify: `CLAUDE.md`, `PLAN.md`, `README.md`, `DESIGN.md`, `docs/architecture.md`
- Modify: `docs/specs/2026-09-04-database-schema-design.md`
- Modify: `docs/specs/2026-09-06-fundamentals-ingest.md` (status line)

- [ ] **Step 1: Write the three errata §11 names**

In `DESIGN.md`, beneath the `quoteSummary` description, add an indented erratum: its statement modules now return `endDate` and `maxAge` only, measured 2026-09-06, so the 43/29/28 stability split and the per-module hashing proposal describe an endpoint the fundamentals cycle does not use. The reasoning stands for anyone reaching for `quoteSummary` again.

In `PLAN.md`, beneath "the Yahoo path holds one session for the run", note that this is true of `quoteSummary` and false of the timeseries endpoint, which a cold client reaches with no cookie and no crumb.

In `docs/specs/2026-09-04-database-schema-design.md`, beneath the documented point-in-time read, add an erratum: `period_type` belongs in the `distinct on`, because a fiscal Q4 shares its `period_end` with its fiscal year — AAPL reports both at 2025-09-30, four-fold apart — so the read as documented returns whichever was inserted later. Migration 021 replaces the index that encoded the same assumption.

- [ ] **Step 2: Add `screener.ingest`'s fundamentals half to CLAUDE.md**

Extend the existing `screener.ingest` entry in the "Infrastructure layout" list:

```markdown
  Fundamentals are the second half, from Yahoo's `fundamentals-timeseries`
  endpoint rather than `quoteSummary`, whose statement modules are now empty.
  It needs no crumb and no cookie, so the client is `ChartClient`'s shape.
  Twenty-eight reported line items are stored; anything Yahoo *computes* from
  lines already stored — EBITDA, free cash flow — is derived at scoring time
  instead. A fact is appended only when its value differs from the latest held,
  and every fact from one payload carries its observation's `fetched_at` as
  `observed_at`, because a per-row clock would let a restatement chain point
  forwards in time. `read_facts` is the point-in-time read and **includes
  `period_type`**: a fiscal Q4 ends when its fiscal year does, so without it a
  year collapses into its own last quarter.
```

Under "Commands", after the ingest prices entry:

```markdown
- Ingest fundamentals: `python -m screener.ingest fundamentals` — every line item
  Yahoo reports per period, appended only where a value changed. Safe to re-run:
  an unchanged night writes observations and no facts.
```

Update the "Status" paragraph: fundamentals ingest exists, no ratio consumes it yet.

- [ ] **Step 3: Move fundamentals into PLAN.md's Done section**

Item 2's remaining half is built. Write a "Done" entry with the numbers from Task 8, and say plainly what §9 of the spec says: the payoff is that the next cycle cannot be wrong quietly, and this cycle and the ratios cycle should be planned as a pair, because facts accumulating with no consumer is where an `observed_at` bug survives a hundred nights of history that cannot be corrected.

Add to "Carried forward": **the ratios cycle needs a staleness rule, not only a coverage rule** — schema D9's `coverage` answers a missing metric, not a missing period, and the point-in-time read will happily pair 2025 revenue with 2023 interest expense. Quote F7's measurement: interest expense recently present for 54/60 sampled securities, `CurrentDebt` for 43/60.

- [ ] **Step 4: README and architecture**

Add the `fundamentals` command to `README.md`'s command list. In `docs/architecture.md`, add the fundamentals path to the pipeline beside prices, and `fundamental_fact` to the ER section for the fact layer.

- [ ] **Step 5: Update the spec's status line**

Change `Status: agreed, not implemented. Written 2026-09-06.` to record that it is implemented, with the date.

- [ ] **Step 6: Run the suite and commit**

```bash
.venv/bin/python -m pytest -n 4
git add CLAUDE.md PLAN.md README.md DESIGN.md docs/architecture.md docs/specs/
git commit -m "Say that fundamentals ingest exists, and correct three documents it disproves"
```

---

## Notes for the reviewer

- **Nothing scores.** `screener.scoring` is untouched, `CODES` is still the four momentum codes, and no `snapshot_daily` row changes. The dashboard still draws `screener.concept` and still says so.
- **One existing signature changed.** `record_observation` returns `(id, fetched_at)` instead of `id`, and `previous_hash` takes an `endpoint`. Both are Task 4 and Task 6, both have exactly one existing caller, and both changes exist because the fundamentals path needs something the price path did not.
- **One index is dropped.** `fundamental_fact_pit_idx` encoded the missing `period_type`. That is the only thing this cycle changes about a settled table.
