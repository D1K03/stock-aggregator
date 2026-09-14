# UI Swap Piece (a): Scoring Explains Itself — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give scoring's market-cap, ratio and momentum functions an explaining form that says why a metric is absent, while proving that what scoring writes does not change.

**Architecture:** A golden test first captures scoring's full output (`metric_daily`, `pillar_score_daily`, `snapshot_daily`, `peer_group_stat`) for a fixture that exercises every absence path, from the code as it stands. Then `basis.py`, `ratios.py` and `metrics.py` each gain an explaining form returning a value or `Absent(reason)`, and the existing functions become filters over it with unchanged signatures. Nothing reads the explaining forms yet — piece (b) will.

**Tech Stack:** Python 3.11+, `psycopg` 3, Postgres 16, `pytest`, `pyright`. No new dependencies.

**Spec:** `docs/specs/2026-09-13-ui-swap.md` — this plan implements D12, §8 piece (a) and §12 step 1.

## Global Constraints

- **What scoring writes must not change.** The golden test captured in Task 1 must pass unchanged after every later task, and these files must pass with **no edits**: `tests/test_scoring_ratios.py`, `tests/test_scoring_basis.py`, `tests/test_scoring_metrics.py`, `tests/test_scoring_run.py`, `tests/test_scoring_run_ratios.py`.
- **When several conditions would each make a metric absent, the first in the formula's evaluation order is reported** (spec D12).
- **Existing function signatures do not change:** `index_facts`, `market_cap`, `compute_ratios`, `compute`.
- **A missing value is absent, never imputed and never zero.**
- **Pure modules open no connection:** `basis.py`, `ratios.py`, `metrics.py`.
- **Each package has a small public surface through `__init__.py`; tests import from `screener.scoring`, never a submodule.**
- **`__all__` ordering follows ruff's RUF022** — SCREAMING_CASE, then CamelCase, then snake_case, alphabetical within each group — not plain `sorted()`.
- **psycopg query strings are single literals with placeholders** — never f-strings or concatenation, in `src/` or `tests/`.
- **Comments explain why, not what.** `pyright` must report zero errors, and it checks `tests/`.

## Amendment to the spec

- **P1 — An empty 52-week window no longer crashes the night.** `metrics.compute` calls `max()` on the closes inside the 52-week window whenever the series *starts* before that window. `read_bars` reads 13 months back, so a security whose bars all fall between 52 weeks and 13 months old — prices that stopped about a year ago while the security is still active — reaches `max([])` and raises `ValueError`, failing the whole night's transaction. Reproduced on 2026-09-13. The explaining form has to decide that branch anyway; it becomes `Absent("no bars in the 52-week window")`. No successful night's output changes: such a night currently writes nothing.

## Environment

Docker Desktop must be running.

```bash
cd /home/daniel/projects/stock-aggregator
docker compose -f compose.yaml up -d          # project stock-aggregator-test, port 5432
export DATABASE_URL_TEST="postgresql://postgres:screener@localhost:5432/screener_test"
```

**Export the variable** — an unset one silently skips every database test. Use `.venv/bin/python -m pytest <file> -n0` for one file. The full suite has 5 known failures in `tests/test_magpie_reachable.py` and `tests/test_magpie_acquire.py` in sandboxes without DNS; any other failure is real.

---

### Task 1: The golden test — scoring's output, captured before anything changes

**Files:**
- Create: `tests/test_scoring_golden.py`
- Create: `tests/golden/scoring_output.json` (generated in Step 3, then committed)

**Interfaces:**
- Consumes: `screener.scoring.run_scoring(conn, *, as_of)`; `screener.ingest.Fact`, `insert_facts`, `latest_values`, `metric_ids`; fixtures `fresh_db`, `an_observation` from `tests/conftest.py`.
- Produces: `tests/golden/scoring_output.json` — the reference every later task must still match.

**No production code changes in this task.** The capture must come from the code as it stands.

- [ ] **Step 1: Write the golden test**

Create `tests/test_scoring_golden.py`:

```python
"""What scoring writes, pinned before its functions learned to explain themselves.

Captured from the code as it stood before the explaining-form refactor (ui-swap
spec D12) and committed. That refactor must leave every row identical -- it
changes how a function reports an absence, never what is written. Regenerate
only for an intended change to what scoring computes, which also bumps the logic
version:

    CAPTURE_SCORING_GOLDEN=1 .venv/bin/python -m pytest tests/test_scoring_golden.py -n0

The fixture is built to reach every absence path the refactor touches: a bank,
a REIT, a thin bucket, a split inside its market-cap window, a stale close,
negative equity and a fact in another currency. The second test checks it still
does, so the golden file cannot quietly become a record of a trivial night.
"""

import json
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from screener.ingest import Fact, insert_facts, latest_values, metric_ids
from screener.scoring import run_scoring

GOLDEN = Path(__file__).resolve().parent / "golden" / "scoring_output.json"
AS_OF = date(2026, 3, 2)
SEEN = datetime(2026, 1, 1, tzinfo=timezone.utc)
# Inside price_daily's 2025 partition and read_bars' 13-month window.
OFFSETS = (380, 200, 100, 30, 0)
STALE_OFFSETS = (380, 200, 100, 30, 10)
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


def _facts(
    scale: str,
    *,
    net_income_currency: str = "USD",
    overrides: dict[str, str] | None = None,
) -> list[Fact]:
    """Four quarters of every flow and one balance sheet, scaled per company.

    Scaling spreads the companies' ratios apart so percentiles are not all ties;
    share counts are not scaled, so market cap moves with the close alone.
    """
    factor = Decimal(scale)
    overrides = overrides or {}
    out: list[Fact] = []
    for code, value in FLOWS.items():
        amount = Decimal(overrides.get(code, value)) * factor
        currency = net_income_currency if code == "net_income" else "USD"
        out += [Fact(code, end, "Q", amount, currency) for end in QUARTERS]
    for code, value in BALANCES.items():
        amount = Decimal(overrides.get(code, value))
        if code != "shares_outstanding":
            amount *= factor
        out.append(Fact(code, QUARTERS[0], "Q", amount, "USD"))
    return out


@pytest.fixture
def universe(fresh_db, an_observation):
    with fresh_db.cursor() as cur:
        cur.execute(
            "insert into sector_scheme (code, name) values ('yfinance', 'yfinance') returning id"
        )
        scheme = cur.fetchone()[0]
        cur.execute(
            "insert into peer_group (scheme_id, sector_node_id, level, code)"
            " values (%s, null, 0, 'market')",
            (scheme,),
        )

    industries: dict[str, int] = {}
    sectors: dict[str, int] = {}
    for sector_code, industry_code in (
        ("technology", "software"),
        ("financial-services", "banks-regional"),
        ("real-estate", "reit-retail"),
        ("real-estate", "real-estate-services"),
    ):
        with fresh_db.cursor() as cur:
            if sector_code not in sectors:
                cur.execute(
                    "insert into sector_node (scheme_id, level, code, name)"
                    " values (%s, 1, %s, %s) returning id",
                    (scheme, sector_code, sector_code),
                )
                sectors[sector_code] = cur.fetchone()[0]
                cur.execute(
                    "insert into peer_group (scheme_id, sector_node_id, level, code)"
                    " values (%s, %s, 1, %s)",
                    (scheme, sectors[sector_code], sector_code),
                )
            cur.execute(
                "insert into sector_node (scheme_id, parent_id, level, code, name)"
                " values (%s, %s, 2, %s, %s) returning id",
                (scheme, sectors[sector_code], industry_code, industry_code),
            )
            industries[industry_code] = cur.fetchone()[0]

    def company(symbol, industry, bump, facts, offsets=OFFSETS):
        security = fresh_db.execute(
            """insert into security
               (name, mic, currency, country, primary_symbol, first_seen)
               values (%s, 'XNAS', 'USD', 'US', %s, '2020-01-01') returning id""",
            (f"{symbol} Inc", symbol),
        ).fetchone()[0]
        fresh_db.execute(
            """insert into security_sector (security_id, sector_node_id, valid_from, source)
               values (%s, %s, '2020-01-01', 'yfinance')""",
            (security, industries[industry]),
        )
        observation = an_observation(security)
        for offset in offsets:
            close = Decimal(100 + bump) if offset == offsets[-1] else Decimal(100)
            fresh_db.execute(
                """insert into price_daily
                   (security_id, trade_date, open, high, low, close, volume,
                    observed_at, ingest_observation_id)
                   values (%s, %s, %s, %s, %s, %s, 1, %s, %s)""",
                (security, AS_OF - timedelta(days=offset), close, close, close, close,
                 SEEN, observation),
            )
        with fresh_db.cursor() as cur:
            insert_facts(
                cur, security, observation, SEEN, facts,
                latest_values(cur, security), metric_ids(cur),
            )
        return security, observation

    for i in range(20):
        company(f"S{i:02d}", "software", i, _facts(str(Decimal(1) + Decimal(i) / 10)))
    company("BANK", "banks-regional", 3, _facts("1.2"))
    company("REIT", "reit-retail", 5, _facts("0.8"))
    company("SVCS", "real-estate-services", 7, _facts("1.5"))
    split, observation = company("SPLT", "software", 9, _facts("1.1"))
    fresh_db.execute(
        """insert into corporate_action
           (security_id, effective_date, action_type, ratio, observed_at, ingest_observation_id)
           values (%s, %s, 'split', 2, %s, %s)""",
        (split, AS_OF - timedelta(days=3), SEEN, observation),
    )
    company("STAL", "software", 4, _facts("0.9"), offsets=STALE_OFFSETS)
    company("NEGQ", "software", 6, _facts("1", overrides={"stockholders_equity": "-100"}))
    company("FRGN", "software", 8, _facts("1", net_income_currency="EUR"))


def _output(conn) -> dict[str, list[list]]:
    """Every row scoring wrote, keyed by stable codes rather than generated ids."""
    return {
        "metric_daily": [list(row) for row in conn.execute(
            """select s.primary_symbol, m.code, md.raw_value::text, md.percentile::text,
                      pg.code, md.peer_count, md.fallback_level, md.period_basis,
                      md.period_end::text
                 from metric_daily md
                 join security s on s.id = md.security_id
                 join metric m on m.id = md.metric_id
                 join peer_group pg on pg.id = md.peer_group_id
             order by 1, 2"""
        ).fetchall()],
        "pillar_score_daily": [list(row) for row in conn.execute(
            """select s.primary_symbol, p.code, ps.score::text, ps.metric_count,
                      ps.coverage::text
                 from pillar_score_daily ps
                 join security s on s.id = ps.security_id
                 join pillar p on p.id = ps.pillar_id
             order by 1, 2"""
        ).fetchall()],
        "snapshot_daily": [list(row) for row in conn.execute(
            """select s.primary_symbol, sd.blended_score::text, sd.pillar_agreement,
                      sd.min_coverage::text, sd.worst_fallback_level
                 from snapshot_daily sd
                 join security s on s.id = sd.security_id
             order by 1"""
        ).fetchall()],
        "peer_group_stat": [list(row) for row in conn.execute(
            """select pg.code, m.code, st.member_count, st.deciles::text
                 from peer_group_stat st
                 join peer_group pg on pg.id = st.peer_group_id
                 join metric m on m.id = st.metric_id
             order by 1, 2"""
        ).fetchall()],
    }


def test_scoring_writes_exactly_what_it_wrote_before_the_explaining_form(fresh_db, universe):
    run_scoring(fresh_db, as_of=AS_OF)

    got = _output(fresh_db)

    if os.environ.get("CAPTURE_SCORING_GOLDEN"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
        pytest.skip(f"captured {GOLDEN}; review and commit it")

    assert got == json.loads(GOLDEN.read_text())


def test_the_golden_fixture_exercises_every_absence_path(fresh_db, universe):
    run_scoring(fresh_db, as_of=AS_OF)

    rows = _output(fresh_db)["metric_daily"]
    codes = {(symbol, code) for symbol, code, *_ in rows}
    market_ranked = {(symbol, code) for symbol, code, _, _, group, _, level, _, _ in rows if level == 0}
    valuation = {"earnings_yield", "ebitda_ev", "fcf_yield", "book_yield", "ffo_yield"}

    assert ("BANK", "book_yield") in codes and ("BANK", "roe") in codes
    assert ("REIT", "ffo_yield") in codes and ("REIT", "earnings_yield") not in codes
    assert ("SVCS", "earnings_yield") in market_ranked
    assert not {code for symbol, code in codes if symbol == "SPLT"} & valuation
    assert not {code for symbol, code in codes if symbol == "STAL"} & valuation
    assert ("NEGQ", "debt_to_equity") not in codes and ("NEGQ", "roic") in codes
    assert ("FRGN", "earnings_yield") not in codes and ("FRGN", "ebitda_ev") in codes
```

- [ ] **Step 2: Run the coverage test to verify the fixture reaches every path**

Run: `.venv/bin/python -m pytest tests/test_scoring_golden.py::test_the_golden_fixture_exercises_every_absence_path -n0 -v`
Expected: PASS. If an assertion fails, the fixture does not reach that path — fix the fixture, not the assertion, before capturing.

- [ ] **Step 3: Capture the golden output from the unchanged code**

```bash
git diff --stat src/   # must print nothing: the capture has to come from the code as it stands
CAPTURE_SCORING_GOLDEN=1 .venv/bin/python -m pytest tests/test_scoring_golden.py::test_scoring_writes_exactly_what_it_wrote_before_the_explaining_form -n0 -q
python3 -c "import json; d = json.load(open('tests/golden/scoring_output.json')); print({k: len(v) for k, v in d.items()})"
```

Expected: the test reports `1 skipped` ("captured …"), and the counts are non-trivial — `metric_daily` well over 200 rows, `snapshot_daily` 27 rows (one per company), `peer_group_stat` several rows including `market`.

- [ ] **Step 4: Run the golden test against the captured file**

Run: `.venv/bin/python -m pytest tests/test_scoring_golden.py -n0 -q`
Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add tests/test_scoring_golden.py tests/golden/scoring_output.json
git commit -m "Pin everything scoring writes before its functions learn to explain themselves"
```

---

### Task 2: `basis.py` explains a missing market cap and a dropped currency

**Files:**
- Modify: `src/screener/scoring/basis.py`
- Modify: `src/screener/scoring/__init__.py`
- Create: `tests/test_scoring_explain.py`

**Interfaces:**
- Consumes: existing `basis.py` (`Item`, `Held`, `index_facts`, `newest_balance`, `balance_at`, `_balance_on`, `market_cap`, `CLOSE_MAX_AGE_DAYS`, `SPLIT_WINDOW_DAYS`).
- Produces (exported from `screener.scoring`):
  - `Absent` — frozen dataclass with `reason: str`.
  - `index_facts_explained(items: Iterable[Item], *, currency: str) -> tuple[dict[str, list[Item]], dict[str, frozenset[str]]]` — held facts, and per code the currencies dropped (a fact with no currency is recorded as `"no currency"`).
  - `explain_market_cap(held: Held, *, close: Decimal | None, close_date: date | None, split_dates: Sequence[date], as_of: date) -> Decimal | Absent`
  - `first_missing_balance(held: Held, codes: Sequence[str], day: date) -> str | None`
  - `index_facts` and `market_cap` keep their signatures and results.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_scoring_explain.py`:

```python
"""Why a metric is absent, from the same functions that decided it (ui-swap D12).

Nothing here changes what scoring writes -- tests/test_scoring_golden.py pins
that. These pin the reasons: one test per template, and the rule that when
several conditions would each make a metric absent, the first one in the
formula's evaluation order is the one reported.
"""

from datetime import date, timedelta
from decimal import Decimal

from screener.scoring import (
    Absent,
    Item,
    explain_market_cap,
    first_missing_balance,
    index_facts,
    index_facts_explained,
    market_cap,
)

AS_OF = date(2026, 3, 2)
QUARTER = date(2025, 12, 31)


def shares(value: str = "100"):
    return index_facts([Item("shares_outstanding", QUARTER, "Q", Decimal(value), "USD")], currency="USD")


def cap(held=None, *, close="20", close_date=AS_OF, split_dates=()):
    return explain_market_cap(
        shares() if held is None else held,
        close=Decimal(close) if close is not None else None,
        close_date=close_date,
        split_dates=list(split_dates),
        as_of=AS_OF,
    )


# -- basis: index_facts ----------------------------------------------------------


def test_index_facts_explained_reports_the_currencies_it_dropped_per_code():
    items = [
        Item("net_income", QUARTER, "Q", Decimal(1), "EUR"),
        Item("net_income", date(2025, 9, 30), "Q", Decimal(1), "USD"),
        Item("revenue", QUARTER, "Q", Decimal(1), "AUD"),
        Item("revenue", date(2025, 9, 30), "Q", Decimal(1), None),
    ]

    held, foreign = index_facts_explained(items, currency="USD")

    assert [item.period_end for item in held["net_income"]] == [date(2025, 9, 30)]
    assert "revenue" not in held
    assert foreign == {"net_income": frozenset({"EUR"}), "revenue": frozenset({"AUD", "no currency"})}
    assert index_facts(items, currency="USD") == held


# -- basis: market cap -----------------------------------------------------------


def test_a_market_cap_that_can_be_computed_is_the_value():
    assert cap() == Decimal(2000)
    assert market_cap(shares(), close=Decimal(20), close_date=AS_OF, split_dates=[], as_of=AS_OF) == Decimal(2000)


def test_no_close_explains_itself():
    assert cap(close=None) == Absent("no market cap: no close visible")
    assert cap(close_date=None) == Absent("no market cap: no close visible")


def test_a_stale_close_says_how_old_it_is():
    assert cap(close_date=AS_OF - timedelta(days=8)) == Absent("no market cap: latest close is 8 days old")
    assert cap(close_date=AS_OF - timedelta(days=7)) == Decimal(2000)


def test_a_split_inside_its_window_names_the_split():
    assert cap(split_dates=[date(2026, 2, 28)]) == Absent("no market cap: split on 2026-02-28")


def test_no_recent_share_count_explains_itself():
    stale = index_facts([Item("shares_outstanding", date(2024, 9, 30), "Q", Decimal(100), "USD")], currency="USD")

    assert cap(stale) == Absent("no market cap: no share count within 15 months")


def test_a_non_positive_market_cap_explains_itself():
    assert cap(shares("0")) == Absent("no market cap: market cap ≤ 0")


def test_the_first_reason_wins_for_market_cap():
    # A stale close and a split in its window: the close is checked first.
    got = cap(close_date=AS_OF - timedelta(days=9), split_dates=[date(2026, 2, 28)])

    assert got == Absent("no market cap: latest close is 9 days old")


def test_first_missing_balance_names_the_first_item_absent_at_a_date():
    held = index_facts([Item("total_debt", QUARTER, "Q", Decimal(1), "USD")], currency="USD")

    assert first_missing_balance(held, ("total_debt", "stockholders_equity"), QUARTER) == "stockholders_equity"
    assert first_missing_balance(held, ("total_debt",), QUARTER) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_scoring_explain.py -n0 -q`
Expected: collection ERROR — `ImportError: cannot import name 'Absent' from 'screener.scoring'`.

- [ ] **Step 3: Add the explaining forms to `basis.py`**

In `src/screener/scoring/basis.py`:

(a) Directly after the `Basis` dataclass, add:

```python
@dataclass(frozen=True)
class Absent:
    """Why an applicable metric has no value (ui-swap spec D12).

    Never written. Scoring discards it; the traceability panel reproduces it on
    demand from these same functions, so the reason cannot drift from the rule
    that produced the absence.
    """

    reason: str
```

(b) Replace `index_facts` with:

```python
def index_facts_explained(
    items: Iterable[Item], *, currency: str
) -> tuple[dict[str, list[Item]], dict[str, frozenset[str]]]:
    """Facts by code without any not in the security's currency, and what was dropped.

    Dropped here rather than checked per ratio, so a figure in another currency
    cannot reach a formula by any route (spec D9). The second mapping is what lets
    an absence say "reported in EUR" instead of the misleading "no figure".
    """
    held: dict[str, list[Item]] = {}
    dropped: dict[str, set[str]] = {}
    for item in items:
        if item.currency == currency:
            held.setdefault(item.code, []).append(item)
        else:
            dropped.setdefault(item.code, set()).add(item.currency or "no currency")
    return held, {code: frozenset(found) for code, found in dropped.items()}


def index_facts(items: Iterable[Item], *, currency: str) -> dict[str, list[Item]]:
    """Facts by code, without any not in the security's currency (spec D9)."""
    return index_facts_explained(items, currency=currency)[0]
```

(c) Directly after `balance_at`, add:

```python
def first_missing_balance(held: Held, codes: Sequence[str], day: date) -> str | None:
    """The first code with no balance on `day`, for an absence to name."""
    for code in codes:
        if _balance_on(held, code, day) is None:
            return code
    return None
```

(d) Replace the whole `market_cap` function with:

```python
def explain_market_cap(
    held: Held,
    *,
    close: Decimal | None,
    close_date: date | None,
    split_dates: Sequence[date],
    as_of: date,
) -> Decimal | Absent:
    """Close times Yahoo's share count as it stands, or why there is none (spec D8).

    **No split factor is ever applied.** Yahoo restates share counts for splits,
    so multiplying by the ratio again doubles the cap (F4). What is unsafe is the
    gap before it restates, so for a week after a split there is no market cap --
    a missing Valuation rather than a wrong one.

    The close itself is bounded too (spec amendment A5): a security whose bars
    stopped before a split pairs a pre-split close with a restated share count,
    a failing price ingest never writes split rows so that window never
    triggers, and a halted or acquired security stays valued on a months-old
    price. A close more than `CLOSE_MAX_AGE_DAYS` before `as_of` gives no
    market cap rather than a wrong one.

    Checked in this order, and the first failure is the reason reported.
    """
    if close is None or close_date is None:
        return Absent("no market cap: no close visible")
    age = (as_of - close_date).days
    if age > CLOSE_MAX_AGE_DAYS:
        return Absent(f"no market cap: latest close is {age} days old")
    window = timedelta(days=SPLIT_WINDOW_DAYS)
    for split in sorted(split_dates):
        if split <= as_of < split + window:
            return Absent(f"no market cap: split on {split.isoformat()}")
    found = newest_balance(held, ("shares_outstanding",), as_of)
    if found is None:
        return Absent("no market cap: no share count within 15 months")
    cap = close * found[1]["shares_outstanding"]
    if cap <= 0:
        return Absent("no market cap: market cap ≤ 0")
    return cap


def market_cap(
    held: Held,
    *,
    close: Decimal | None,
    close_date: date | None,
    split_dates: Sequence[date],
    as_of: date,
) -> Decimal | None:
    """The market cap, or None -- `explain_market_cap` without the reason."""
    value = explain_market_cap(
        held, close=close, close_date=close_date, split_dates=split_dates, as_of=as_of
    )
    return None if isinstance(value, Absent) else value
```

- [ ] **Step 4: Export from the package**

In `src/screener/scoring/__init__.py`, extend the `from screener.scoring.basis import (...)` block with `Absent`, `explain_market_cap`, `first_missing_balance`, `index_facts_explained`, and add them to `__all__` in RUF022 order: `"Absent"` in the CamelCase group (alphabetically before `"Action"`); `"explain_market_cap"`, `"first_missing_balance"`, `"index_facts_explained"` in the snake_case group at their alphabetical positions.

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_scoring_explain.py tests/test_scoring_basis.py tests/test_scoring_golden.py -n0 -q`
Expected: all PASS — the new tests, the unedited basis tests, and the golden test.

- [ ] **Step 6: Typecheck and commit**

Run: `.venv/bin/pyright src/screener/scoring/basis.py tests/test_scoring_explain.py`
Expected: `0 errors`.

```bash
git add src/screener/scoring/basis.py src/screener/scoring/__init__.py tests/test_scoring_explain.py
git commit -m "Say why there is no market cap, and which currencies a fact index dropped"
```

---

### Task 3: `ratios.py` explains every absent ratio

**Files:**
- Modify: `src/screener/scoring/ratios.py`
- Modify: `src/screener/scoring/__init__.py`
- Modify: `tests/test_scoring_explain.py` (append)

**Interfaces:**
- Consumes (Task 2): `Absent`, `explain_market_cap`, `first_missing_balance`; existing `flow_basis`, `newest_balance`, `balance_at`, `Held`, `Basis`.
- Produces (exported from `screener.scoring`):
  - `Explained` — type alias `Ratio | Absent`.
  - `explain_ratios(held: Held, *, industry: str | None, close: Decimal | None, close_date: date | None, split_dates: Sequence[date], as_of: date, currency: str | None = None, foreign: Mapping[str, frozenset[str]] | None = None) -> dict[str, Explained]` — one entry per **applicable** ratio code, in `RATIO_CODES` order.
  - `compute_ratios` — signature and result unchanged.

Reason texts, exactly (spec D12):

| condition | reason |
|---|---|
| market cap absent | the `Absent` from `explain_market_cap`, unchanged |
| any input's facts dropped for currency (checked where a basis or balance is missing) | `facts reported in <sorted currencies, comma-separated>, not <currency>` |
| no basis | `no clean TTM or annual figure for <inputs, comma-separated>` |
| `balance_at` misses | `no <first missing item> at <YYYY-MM-DD>` |
| `newest_balance` misses | `no date within 15 months holding all of <items, comma-separated>` |
| D5 rules | `equity ≤ 0` · `enterprise value ≤ 0` · `invested capital ≤ 0` · `interest expense ≤ 0` · `D&A negative` · `capex positive` · `revenue ≤ 0` |

- [ ] **Step 1: Append the failing tests**

Append to `tests/test_scoring_explain.py` (and add `Ratio`, `explain_ratios` to its `from screener.scoring import (...)` block):

```python
# -- ratios --------------------------------------------------------------------

QUARTERS = (date(2025, 12, 31), date(2025, 9, 30), date(2025, 6, 30), date(2025, 3, 31))
FLOWS = {
    "net_income": "25", "revenue": "250", "gross_profit": "100", "ebit": "40",
    "depreciation_amortisation": "10", "operating_cash_flow": "50",
    "capital_expenditure": "-20", "interest_expense": "5", "tax_provision": "8",
    "pretax_income": "32",
}
BALANCES = {
    "stockholders_equity": "800", "total_debt": "400", "cash_and_equivalents": "200",
    "shares_outstanding": "100",
}


def company_items(*, drop=(), currency_of=None, **overrides):
    """The hand-checkable company of test_scoring_ratios.py, as raw items.

    `drop` removes line items entirely; `currency_of` reports named items in
    another currency; overrides replace per-quarter flow or balance values.
    """
    currency_of = currency_of or {}
    items = []
    for code, value in FLOWS.items():
        if code in drop:
            continue
        for end in QUARTERS:
            items.append(Item(code, end, "Q", Decimal(overrides.get(code, value)), currency_of.get(code, "USD")))
    for code, value in BALANCES.items():
        if code in drop:
            continue
        items.append(Item(code, QUARTERS[0], "Q", Decimal(overrides.get(code, value)), currency_of.get(code, "USD")))
    return items


def explain(industry="software", close="20", *, drop=(), currency_of=None, **overrides):
    held, foreign = index_facts_explained(
        company_items(drop=drop, currency_of=currency_of, **overrides), currency="USD"
    )
    return explain_ratios(
        held,
        industry=industry,
        close=Decimal(close) if close is not None else None,
        close_date=AS_OF,
        split_dates=[],
        as_of=AS_OF,
        currency="USD",
        foreign=foreign,
    )


def test_a_computable_ratio_is_its_ratio_and_only_applicable_codes_appear():
    bank = explain(industry="banks-regional")

    assert set(bank) == {"earnings_yield", "book_yield", "roe"}
    assert bank["earnings_yield"] == Ratio(Decimal("0.05"), "TTM", date(2025, 12, 31))


def test_no_market_cap_is_the_reason_for_every_valuation_ratio_and_quality_stands():
    got = explain(close=None)

    for code in ("earnings_yield", "ebitda_ev", "fcf_yield"):
        assert got[code] == Absent("no market cap: no close visible")
    assert isinstance(got["roic"], Ratio)


def test_the_first_reason_wins_across_market_cap_and_basis():
    # No close and no net income: market cap is checked first.
    got = explain(close=None, drop=("net_income",))

    assert got["earnings_yield"] == Absent("no market cap: no close visible")


def test_a_missing_basis_names_the_inputs():
    got = explain(drop=("interest_expense",))

    assert got["interest_cover"] == Absent("no clean TTM or annual figure for ebit, interest_expense")


def test_facts_in_another_currency_are_the_reason_rather_than_no_figure():
    got = explain(currency_of={"net_income": "EUR"})

    assert got["earnings_yield"] == Absent("facts reported in EUR, not USD")


def test_a_missing_newest_balance_names_every_item_it_needed():
    got = explain(drop=("total_debt",))

    assert got["debt_to_equity"] == Absent(
        "no date within 15 months holding all of total_debt, stockholders_equity"
    )


def test_a_balance_missing_at_the_basis_date_names_the_item_and_date():
    got = explain(drop=("cash_and_equivalents",))

    assert got["roic"] == Absent("no cash_and_equivalents at 2025-12-31")


def test_each_sign_rule_has_its_reason():
    assert explain(stockholders_equity="-100")["debt_to_equity"] == Absent("equity ≤ 0")
    assert explain(cash_and_equivalents="3000")["ebitda_ev"] == Absent("enterprise value ≤ 0")
    assert explain(cash_and_equivalents="3000")["roic"] == Absent("invested capital ≤ 0")
    assert explain(interest_expense="0")["interest_cover"] == Absent("interest expense ≤ 0")
    assert explain(depreciation_amortisation="-10")["ebitda_ev"] == Absent("D&A negative")
    assert explain(industry="reit-retail", depreciation_amortisation="-10")["ffo_yield"] == Absent("D&A negative")
    assert explain(capital_expenditure="20")["fcf_yield"] == Absent("capex positive")
    assert explain(revenue="0")["gross_margin"] == Absent("revenue ≤ 0")
    bank = explain(industry="banks-regional", stockholders_equity="-100")
    assert bank["roe"] == Absent("equity ≤ 0")
    assert bank["book_yield"] == Absent("equity ≤ 0")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_scoring_explain.py -n0 -q`
Expected: collection ERROR — `ImportError: cannot import name 'explain_ratios'`.

- [ ] **Step 3: Rewrite the formulas as explaining forms**

In `src/screener/scoring/ratios.py`:

(a) Change the imports to:

```python
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from screener.scoring.basis import (
    Absent,
    Basis,
    Held,
    balance_at,
    explain_market_cap,
    first_missing_balance,
    flow_basis,
    newest_balance,
)
```

(b) Directly after the `Ratio` dataclass, add:

```python
Explained = Ratio | Absent


@dataclass(frozen=True)
class _Inputs:
    """What every formula reads, and what it needs to explain an absence."""

    held: Held
    as_of: date
    currency: str | None
    foreign: Mapping[str, frozenset[str]]


def _unavailable(inputs: _Inputs, codes: Sequence[str], reason: str) -> Absent:
    # A figure reported in another currency was dropped before any formula saw
    # it, so "no figure" would be the wrong story: say where it went instead.
    dropped = sorted({c for code in codes for c in inputs.foreign.get(code, frozenset())})
    if dropped and inputs.currency is not None:
        return Absent(f"facts reported in {', '.join(dropped)}, not {inputs.currency}")
    return Absent(reason)


def _basis(inputs: _Inputs, codes: Sequence[str]) -> Basis | Absent:
    basis = flow_basis(inputs.held, codes, inputs.as_of)
    if basis is None:
        return _unavailable(
            inputs, codes, f"no clean TTM or annual figure for {', '.join(codes)}"
        )
    return basis


def _newest(inputs: _Inputs, codes: Sequence[str]) -> tuple[date, dict[str, Decimal]] | Absent:
    found = newest_balance(inputs.held, codes, inputs.as_of)
    if found is None:
        return _unavailable(
            inputs, codes, f"no date within 15 months holding all of {', '.join(codes)}"
        )
    return found


def _at(inputs: _Inputs, codes: Sequence[str], day: date) -> dict[str, Decimal] | Absent:
    balances = balance_at(inputs.held, codes, day)
    if balances is None:
        missing = first_missing_balance(inputs.held, codes, day) or ", ".join(codes)
        return _unavailable(inputs, codes, f"no {missing} at {day.isoformat()}")
    return balances
```

(c) Replace every formula from `_earnings_yield` through `_interest_cover`, `_FORMULAS` and `compute_ratios` with the following. **Keep `_tax_rate` exactly as it is.** Every check stays in its original order; only the return of a reason is new.

```python
def _earnings_yield(inputs: _Inputs, cap: Decimal | Absent) -> Explained:
    if isinstance(cap, Absent):
        return cap
    basis = _basis(inputs, ("net_income",))
    if isinstance(basis, Absent):
        return basis
    return Ratio(basis.values["net_income"] / cap, basis.kind, basis.period_end)


def _ffo_yield(inputs: _Inputs, cap: Decimal | Absent) -> Explained:
    # Net income plus D&A: an approximation of funds from operations from stored
    # items, because depreciation on property that generally appreciates is what
    # makes a REIT's net income mislead.
    if isinstance(cap, Absent):
        return cap
    basis = _basis(inputs, ("net_income", "depreciation_amortisation"))
    if isinstance(basis, Absent):
        return basis
    if basis.values["depreciation_amortisation"] < 0:
        return Absent("D&A negative")
    ffo = basis.values["net_income"] + basis.values["depreciation_amortisation"]
    return Ratio(ffo / cap, basis.kind, basis.period_end)


def _ebitda_ev(inputs: _Inputs, cap: Decimal | Absent) -> Explained:
    if isinstance(cap, Absent):
        return cap
    basis = _basis(inputs, ("ebit", "depreciation_amortisation"))
    if isinstance(basis, Absent):
        return basis
    if basis.values["depreciation_amortisation"] < 0:
        return Absent("D&A negative")
    # Current, like the cap: enterprise value describes today (spec D7).
    found = _newest(inputs, ("total_debt", "cash_and_equivalents"))
    if isinstance(found, Absent):
        return found
    balances = found[1]
    enterprise = cap + balances["total_debt"] - balances["cash_and_equivalents"]
    if enterprise <= 0:
        return Absent("enterprise value ≤ 0")
    ebitda = basis.values["ebit"] + basis.values["depreciation_amortisation"]
    return Ratio(ebitda / enterprise, basis.kind, basis.period_end)


def _fcf_yield(inputs: _Inputs, cap: Decimal | Absent) -> Explained:
    if isinstance(cap, Absent):
        return cap
    basis = _basis(inputs, ("operating_cash_flow", "capital_expenditure"))
    if isinstance(basis, Absent):
        return basis
    # Yahoo reports capex negative for every security (F3), so free cash flow
    # *adds* it. A positive value means the convention moved, and should show as
    # lost coverage rather than silently halve FCF.
    if basis.values["capital_expenditure"] > 0:
        return Absent("capex positive")
    fcf = basis.values["operating_cash_flow"] + basis.values["capital_expenditure"]
    return Ratio(fcf / cap, basis.kind, basis.period_end)


def _book_yield(inputs: _Inputs, cap: Decimal | Absent) -> Explained:
    if isinstance(cap, Absent):
        return cap
    found = _newest(inputs, ("stockholders_equity",))
    if isinstance(found, Absent):
        return found
    day, balances = found
    if balances["stockholders_equity"] <= 0:
        return Absent("equity ≤ 0")
    return Ratio(balances["stockholders_equity"] / cap, None, day)


def _roic(inputs: _Inputs, cap: Decimal | Absent) -> Explained:
    basis = _basis(inputs, ("ebit", "tax_provision", "pretax_income"))
    if isinstance(basis, Absent):
        return basis
    # At the basis's own date, so a return is measured on the capital of the
    # period that earned it (spec D7).
    balances = _at(
        inputs, ("stockholders_equity", "total_debt", "cash_and_equivalents"), basis.period_end
    )
    if isinstance(balances, Absent):
        return balances
    invested = (
        balances["stockholders_equity"]
        + balances["total_debt"]
        - balances["cash_and_equivalents"]
    )
    if invested <= 0:
        return Absent("invested capital ≤ 0")
    rate = _tax_rate(basis.values["tax_provision"], basis.values["pretax_income"])
    return Ratio(
        basis.values["ebit"] * (Decimal(1) - rate) / invested, basis.kind, basis.period_end
    )


def _roe(inputs: _Inputs, cap: Decimal | Absent) -> Explained:
    basis = _basis(inputs, ("net_income",))
    if isinstance(basis, Absent):
        return basis
    balances = _at(inputs, ("stockholders_equity",), basis.period_end)
    if isinstance(balances, Absent):
        return balances
    if balances["stockholders_equity"] <= 0:
        return Absent("equity ≤ 0")
    return Ratio(
        basis.values["net_income"] / balances["stockholders_equity"], basis.kind, basis.period_end
    )


def _gross_margin(inputs: _Inputs, cap: Decimal | Absent) -> Explained:
    basis = _basis(inputs, ("gross_profit", "revenue"))
    if isinstance(basis, Absent):
        return basis
    if basis.values["revenue"] <= 0:
        return Absent("revenue ≤ 0")
    return Ratio(
        basis.values["gross_profit"] / basis.values["revenue"], basis.kind, basis.period_end
    )


def _debt_to_equity(inputs: _Inputs, cap: Decimal | Absent) -> Explained:
    # The newest date both are held, within 456 days (plan amendment A4). A
    # negative equity would rank a heavily indebted company as the least
    # leveraged, so it is absent instead.
    found = _newest(inputs, ("total_debt", "stockholders_equity"))
    if isinstance(found, Absent):
        return found
    day, balances = found
    if balances["stockholders_equity"] <= 0:
        return Absent("equity ≤ 0")
    return Ratio(balances["total_debt"] / balances["stockholders_equity"], None, day)


def _interest_cover(inputs: _Inputs, cap: Decimal | Absent) -> Explained:
    # A debt-free company loses this metric, and the signal survives in the
    # pillar only while Yahoo still reports total_debt as 0 -- once it stops
    # publishing the series, debt_to_equity is absent too rather than zero.
    basis = _basis(inputs, ("ebit", "interest_expense"))
    if isinstance(basis, Absent):
        return basis
    if basis.values["interest_expense"] <= 0:
        return Absent("interest expense ≤ 0")
    return Ratio(
        basis.values["ebit"] / basis.values["interest_expense"], basis.kind, basis.period_end
    )


_FORMULAS: dict[str, Callable[[_Inputs, Decimal | Absent], Explained]] = {
    "earnings_yield": _earnings_yield,
    "ebitda_ev": _ebitda_ev,
    "fcf_yield": _fcf_yield,
    "book_yield": _book_yield,
    "ffo_yield": _ffo_yield,
    "roic": _roic,
    "roe": _roe,
    "gross_margin": _gross_margin,
    "debt_to_equity": _debt_to_equity,
    "interest_cover": _interest_cover,
}


def explain_ratios(
    held: Held,
    *,
    industry: str | None,
    close: Decimal | None,
    close_date: date | None,
    split_dates: Sequence[date],
    as_of: date,
    currency: str | None = None,
    foreign: Mapping[str, frozenset[str]] | None = None,
) -> dict[str, Explained]:
    """Every applicable ratio, as its value or the reason it has none (ui-swap D12).

    `currency` and `foreign` come from `index_facts_explained`; without them an
    absence caused by a dropped currency reads as a missing figure, which is all
    scoring needs and all `compute_ratios` passes.
    """
    wanted = {code for codes in applicable(industry).values() for code in codes}
    cap = explain_market_cap(
        held, close=close, close_date=close_date, split_dates=split_dates, as_of=as_of
    )
    inputs = _Inputs(held, as_of, currency, foreign or {})
    return {code: _FORMULAS[code](inputs, cap) for code in RATIO_CODES if code in wanted}


def compute_ratios(
    held: Held,
    *,
    industry: str | None,
    close: Decimal | None,
    close_date: date | None,
    split_dates: Sequence[date],
    as_of: date,
) -> dict[str, Ratio]:
    """Every applicable ratio that can be honestly computed, keyed by code."""
    explained = explain_ratios(
        held,
        industry=industry,
        close=close,
        close_date=close_date,
        split_dates=split_dates,
        as_of=as_of,
    )
    return {code: value for code, value in explained.items() if isinstance(value, Ratio)}
```

(d) Remove `market_cap` from the old `from screener.scoring.basis import …` line if it is still imported and now unused (pyright reports an unused import only as a warning; delete it anyway).

- [ ] **Step 4: Export from the package**

In `src/screener/scoring/__init__.py`, extend the `from screener.scoring.ratios import (...)` block with `Explained` and `explain_ratios`; add `"Explained"` to the CamelCase group and `"explain_ratios"` to the snake_case group of `__all__`, each at its alphabetical position.

- [ ] **Step 5: Run the tests — new, unedited and golden**

Run: `.venv/bin/python -m pytest tests/test_scoring_explain.py tests/test_scoring_ratios.py tests/test_scoring_basis.py tests/test_scoring_run_ratios.py tests/test_scoring_golden.py -n0 -q`
Expected: all PASS. **If the golden test fails, the refactor changed what scoring writes: fix the code, never the golden file.**

- [ ] **Step 6: Typecheck and commit**

Run: `.venv/bin/pyright src/screener/scoring tests/test_scoring_explain.py`
Expected: `0 errors`.

```bash
git add src/screener/scoring/ratios.py src/screener/scoring/__init__.py tests/test_scoring_explain.py
git commit -m "Make every ratio say why it is absent, with scoring's output unchanged"
```

---

### Task 4: `metrics.py` explains momentum, and an empty 52-week window stops crashing the night

**Files:**
- Modify: `src/screener/scoring/metrics.py`
- Modify: `src/screener/scoring/__init__.py`
- Modify: `tests/test_scoring_explain.py` (append)
- Modify: `CLAUDE.md` (the `screener.scoring` bullet)

**Interfaces:**
- Consumes (Task 2): `Absent` from `screener.scoring.basis`.
- Produces (exported from `screener.scoring`):
  - `explain_momentum(series: Sequence[tuple[date, Decimal]], as_of: date) -> dict[str, Decimal | Absent]` — all four `CODES`, in `CODES` order.
  - `compute` — signature unchanged; results unchanged except plan amendment P1.

Reason texts, exactly: `no visible bars` · `price history shorter than the <n>-month window` (n = 3, 6, 12) · `a past close ≤ 0` · `price history shorter than the 52-week window` · `no bars in the 52-week window` · `52-week high ≤ 0`.

- [ ] **Step 1: Append the failing tests**

Append to `tests/test_scoring_explain.py` (add `compute`, `explain_momentum` to its imports):

```python
# -- momentum ------------------------------------------------------------------


def bars(*pairs):
    """(days before AS_OF, close) pairs into a series."""
    return [(AS_OF - timedelta(days=days), Decimal(close)) for days, close in pairs]


def test_no_visible_bars_is_the_reason_for_all_four():
    assert explain_momentum([], AS_OF) == {
        code: Absent("no visible bars") for code in ("ret_3m", "ret_6m", "ret_12m", "off_52w_high")
    }


def test_a_short_history_names_each_window_it_does_not_cover():
    got = explain_momentum(bars((100, "100"), (0, "110")), AS_OF)

    assert got["ret_3m"] == Decimal("0.1")
    assert got["ret_6m"] == Absent("price history shorter than the 6-month window")
    assert got["ret_12m"] == Absent("price history shorter than the 12-month window")
    assert got["off_52w_high"] == Absent("price history shorter than the 52-week window")


def test_a_non_positive_past_close_or_high_has_its_reason():
    got = explain_momentum(bars((380, "0"), (200, "0"), (100, "0"), (0, "0")), AS_OF)

    assert got["ret_3m"] == Absent("a past close ≤ 0")
    assert got["off_52w_high"] == Absent("52-week high ≤ 0")


def test_an_empty_52_week_window_is_absent_instead_of_crashing_the_night():
    # Bars only between 52 weeks and 13 months old: inside read_bars' window,
    # outside the 52-week one. Before plan amendment P1 this raised ValueError
    # from max() and failed every security's night.
    series = bars((380, "100"), (370, "101"))

    got = explain_momentum(series, AS_OF)

    assert got["off_52w_high"] == Absent("no bars in the 52-week window")
    assert "off_52w_high" not in compute(series, AS_OF)


def test_compute_is_explain_momentum_without_the_absences():
    series = bars((380, "100"), (200, "90"), (100, "95"), (30, "120"), (0, "110"))

    explained = explain_momentum(series, AS_OF)

    assert all(isinstance(value, Decimal) for value in explained.values())
    assert compute(series, AS_OF) == explained
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_scoring_explain.py -n0 -q`
Expected: collection ERROR — `ImportError: cannot import name 'explain_momentum'`.

Then confirm the crash the amendment fixes exists before fixing it:

Run: `.venv/bin/python -c "from datetime import date, timedelta; from decimal import Decimal; from screener.scoring import compute; d = date(2026, 3, 2); compute([(d - timedelta(days=380), Decimal(100)), (d - timedelta(days=370), Decimal(101))], d)"`
Expected: `ValueError: max() iterable argument is empty`.

- [ ] **Step 3: Rewrite `compute` over an explaining form**

In `src/screener/scoring/metrics.py`, add `from screener.scoring.basis import Absent` to the imports, and replace the whole `compute` function with:

```python
def explain_momentum(
    series: Sequence[tuple[date, Decimal]], as_of: date
) -> dict[str, Decimal | Absent]:
    """All four momentum metrics, each as its value or the reason it has none.

    The coverage rule is spec D8's: a window the history does not reach gives no
    value rather than one computed from less history than it claims.
    """
    ordered = sorted(series)
    latest = _at_or_before(ordered, as_of)
    if latest is None:
        return {code: Absent("no visible bars") for code in CODES}

    out: dict[str, Decimal | Absent] = {}
    for code, months in _RETURN_MONTHS.items():
        past = _at_or_before(ordered, months_before(as_of, months))
        if past is None:
            out[code] = Absent(f"price history shorter than the {months}-month window")
        elif past <= 0:
            out[code] = Absent("a past close ≤ 0")
        else:
            out[code] = latest / past - 1

    window_start = as_of - timedelta(days=_52W_DAYS)
    # The same coverage rule as the return windows: a series that begins inside
    # the window would otherwise report "at its 52-week high" off a fortnight.
    if ordered[0][0] > window_start:
        out["off_52w_high"] = Absent("price history shorter than the 52-week window")
    else:
        window = [close for day, close in ordered if window_start <= day <= as_of]
        # A series can start before the window and still have nothing in it:
        # read_bars reaches back 13 months, so prices that stopped about a year
        # ago land here. `max([])` used to raise and fail the whole night (plan
        # amendment P1).
        if not window:
            out["off_52w_high"] = Absent("no bars in the 52-week window")
        else:
            high = max(window)
            out["off_52w_high"] = latest / high - 1 if high > 0 else Absent("52-week high ≤ 0")
    return {code: out[code] for code in CODES}


def compute(
    series: Sequence[tuple[date, Decimal]], as_of: date
) -> dict[str, Decimal]:
    """The computable metrics for one security, keyed by `metric.code`."""
    return {
        code: value
        for code, value in explain_momentum(series, as_of).items()
        if not isinstance(value, Absent)
    }
```

- [ ] **Step 4: Export from the package**

In `src/screener/scoring/__init__.py`, change `from screener.scoring.metrics import CODES, compute, months_before` to include `explain_momentum`, and add `"explain_momentum"` to the snake_case group of `__all__` at its alphabetical position.

- [ ] **Step 5: Run every guard**

```bash
.venv/bin/python -m pytest tests/test_scoring_explain.py tests/test_scoring_metrics.py tests/test_scoring_ratios.py tests/test_scoring_basis.py tests/test_scoring_run.py tests/test_scoring_run_ratios.py tests/test_scoring_golden.py -n0 -q
CAPTURED=$(git log --format=%h -1 -- tests/golden/scoring_output.json)
git diff "$CAPTURED" -- tests/test_scoring_ratios.py tests/test_scoring_basis.py tests/test_scoring_metrics.py tests/test_scoring_run.py tests/test_scoring_run_ratios.py tests/golden/scoring_output.json | wc -l
.venv/bin/python -m pytest -q 2>&1 | tail -1
.venv/bin/pyright 2>&1 | grep errors
```

Expected: the focused run all PASS; the `git diff … | wc -l` prints **`0`** — measured from the commit that captured the golden output, so it proves neither the guard tests nor the golden file changed during the refactor; the full suite shows only the 5 known magpie DNS failures; pyright `0 errors`.

- [ ] **Step 6: Update `CLAUDE.md`**

In `CLAUDE.md`'s `screener.scoring` bullet, after the sentence ending "`min_coverage` counts a weighted pillar that produced nothing as 0.", add:

```
The market-cap, ratio and momentum functions each have an explaining form (`explain_market_cap`,
`explain_ratios`, `explain_momentum`) that returns a value or `Absent(reason)`; the scoring functions
are filters over them, so a reason can never drift from the rule that produced the absence, and
`tests/test_scoring_golden.py` pins that what scoring writes did not change.
```

Rewrap it to the bullet's existing width and two-space continuation indent.

- [ ] **Step 7: Commit**

```bash
git add src/screener/scoring/metrics.py src/screener/scoring/__init__.py tests/test_scoring_explain.py CLAUDE.md
git commit -m "Explain absent momentum, and stop an empty 52-week window crashing the night"
```
