# UI Swap Piece (c): The Page and Steven — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the dashboard's invented Overview and Steven's invented chart with the real v2 screen, one click from every score's raw inputs, and delete the concept data.

**Architecture:** Piece (b)'s `screener.screen` and its two endpoints already serve the screen. This piece has three parts:
- **The read path, amended.** The three findings the spec settled in §13 go in first, because both new consumers depend on the read path.
- **Steven.** His `chart` tool reads real adjusted closes and the latest night's scores through `playground.select`, as his read-only role, using `screener.screen`'s own statements and row parsers.
- **The web app.** The one chart renderer gains a price mode. A typed client (`web/lib/screen.ts`) feeds a rewritten Overview: filters, tiles, a paged table, a price chart and a "Why this score" panel. `screener.concept`, `web/lib/data.ts`, `AlertFeed` and `ScoreChart` are deleted.

**Tech Stack:** Python 3.11+, `psycopg` 3, Postgres 16, `pytest`, `pyright`; Next 16, React 19, TypeScript 5, ESLint 9. No new dependencies in either language.

**Spec:** `docs/specs/2026-09-13-ui-swap.md`. This plan implements D1–D5 as they bear on the page, D15–D18, the §13 amendment, §7's page and Steven rows, §8 piece (c), §11's remaining errata and §12 step 3. Read D9 and D10 for the exact JSON the page consumes.

## Global Constraints

- **Nothing scoring computes or writes changes, and there are no schema changes.** No file under `src/screener/scoring/` or `migrations/` is edited, and `tests/golden/scoring_output.json` is unchanged.
- **One chart renderer.** Every chart on the page, in the chat and in Discord is `chartSvg` from `web/lib/chart-svg.ts`. The table's sparkline stays a bare polyline. Do not add a second way to draw a chart.
- **Score mode is untouched.** A `ChartSpec` with no `kind` must render byte-identical SVG before and after Task 6, because chat threads saved in browser storage hold such specs, including ones with `crossing` marks.
- **Nothing claims to be illustrative any more, and nothing offers a crossing.** Not the page, the palette context, Steven's prompt, his tool description or his tool results. Crossings return with score history and alerting.
- **Steven reads only through `playground.select`**, as `playground_bot`, with at most three `select` calls per chart (D17). SQL stays literal, with bound parameters only.
- **Money and ratios reach the browser as decimal strings** (D5). The page formats them and never computes a score. Units and precision:

  | value | shown as |
  |---|---|
  | percent metrics | percent, one decimal |
  | `book_yield`, `debt_to_equity`, `interest_cover` | multiple, two decimals, `×` |
  | percentiles | integer 0–100 |
  | blended and pillar scores | one decimal, as sent |
  | closes | two decimals below 1,000, none at or above |
- **No investment language.** No "buy", "sell", "strong", "opportunity" or "signal" in copy.
- **Each Python package keeps a small public surface through `__init__.py`.** Nothing outside `screener.screen` imports `screener.screen.queries` or `screener.screen.rows`; `screener.bot.tools.charts` is imported by tests as it is today.
- **`__all__` ordering follows ruff's RUF022:** SCREAMING_CASE, then CamelCase, then snake_case, alphabetical within each group.
- **`pyright` reports zero errors. `npm run lint` reports zero errors, and `npx tsc --noEmit` is clean, in `web/`.**
- **Comments explain why, not what**, in the voice of the surrounding file.
- **Open parameters (spec §10):** page size 50; 30 closes for a sparkline, 60 for a chart; price-mode padding 5% and three ticks.

## Amendments to the spec

- **C1: Steven's three reads.** D17 lists three reads. They are these three statements:
  1. `CHART_SECURITY`, which is new. It returns every security holding the symbol, each with the latest qualifying run and that security's snapshot and pillar scores on it, so §13's "prefer the active match" is applied in Python over all matches.
  2. `CLOSES`, from piece (b).
  3. `ACTIONS`, from piece (b).

  The chart's closes are `screen.closes`' output, already rounded to D5, so Steven and the dashboard draw the same numbers for the same bars.
- **C2: no run yet.** With no qualifying run, Steven's closes are read up to today's UTC date. D11 needs no run.
- **C3: lint in CI.** `npm run lint` reports 12 existing errors, all `react-hooks/set-state-in-effect`, in files this piece does not otherwise touch. Each existing site gets a targeted `eslint-disable-next-line` comment giving the reason. The rule stays an error, so new code must pass it.
- **C4: the web CI job also runs `npx tsc --noEmit`.** It costs seconds. Today the web app is only typechecked inside the image build, and that build runs on pull requests only.
- **C5: the dashboard's chart is `ChatChart` itself, in price mode**, rather than a new component. The page, the chat and Discord then share the hover as well as the drawing.
- **C6: the prompt's closing "If asked" line changes.** It names the three pillars that are scored, and describes alerts as not built yet, wording that avoids "crossing". §8 requires the prompt to contain neither "illustrative" nor "crossing", and that line had both a false pillar list and the word.
- **C7: `charts.annotate` becomes public** within its module (not re-exported from `screener.bot.tools`), so the mark rules are tested on a fixed series without a database.

## Environment

Docker Desktop must be running.

```bash
cd /home/daniel/projects/stock-aggregator
docker compose -f compose.yaml up -d          # project stock-aggregator-test, port 5432
export DATABASE_URL_TEST="postgresql://postgres:screener@localhost:5432/screener_test"
cd web && npm ci && cd ..                     # once; node_modules is git-ignored
```

**Export the variable.** An unset one silently skips every database test. Use `.venv/bin/python -m pytest <file> -n0` for one file. The full suite has 5 known failures in `tests/test_magpie_reachable.py` and `tests/test_magpie_acquire.py` in sandboxes without DNS; any other failure is real.

Work on branch `ui-swap-page`, which already exists and holds the §13 spec amendment.

## File map

| file | responsibility | task |
|---|---|---|
| `src/screener/screen/queries.py` | `REFRESHED` bars only; `CHART_SECURITY` | 1, 3 |
| `src/screener/screen/explain.py` | only prices refresh | 1 |
| `src/screener/screen/read.py` | `choose_match` | 1 |
| `src/screener/screen/rows.py` | `ChartSecurityRow` | 3 |
| `src/screener/screen/__init__.py` | public surface | 1, 3 |
| `src/screener/playground/engine.py` | `select` takes named parameters | 2 |
| `src/screener/bot/tools/charts.py` | the tool on real data | 3 |
| `src/screener/concept/` | deleted | 3 |
| `src/screener/bot/agent.py` | prompt | 4 |
| `tests/test_screen_match.py` | `choose_match`, pure | 1 |
| `tests/test_screen_explain.py`, `tests/test_screen_security.py` | §13 behaviour | 1 |
| `tests/test_playground.py` | named parameters | 2 |
| `tests/test_charts.py` | rewritten | 3 |
| `tests/test_bot.py` | prompt test and budget comment | 4 |
| `.github/workflows/ci.yml` | web lint and typecheck job | 5 |
| 9 web files with existing lint errors | disable comments | 5 |
| `web/lib/threads.ts` | `ChartSpec.kind`, optional score lines | 6 |
| `web/lib/chart-svg.ts` | price mode | 6 |
| `web/components/ChatChart.tsx` | hover text by kind | 6 |
| `web/lib/screen.ts` | types, fetchers, formatters | 7 |
| `web/components/WhyPanel.tsx` | the traceability panel | 8 |
| `web/app/page.tsx` | the Overview | 9 |
| `web/components/StatTiles.tsx`, `UniverseTable.tsx`, `Sparkline.tsx` | real rows | 9 |
| `web/lib/screen-context.tsx` | `illustrative` deleted | 9 |
| `web/app/steven/page.tsx`, `web/app/audit/page.tsx`, `web/lib/skybird.ts` | stale concept wording | 9 |
| `web/app/globals.css` | panel styles in, concept and alert styles out | 8, 9 |
| `web/lib/data.ts`, `web/components/AlertFeed.tsx`, `web/components/ScoreChart.tsx` | deleted | 9 |
| `CLAUDE.md`, `PLAN.md`, `docs/architecture.md` | errata | 10 |

---

### Task 1: The read path's §13 amendment

**Files:**
- Modify: `src/screener/screen/queries.py` (`REFRESHED`)
- Modify: `src/screener/screen/explain.py` (constants, `DEPENDS`, the end of `reproduce`)
- Modify: `src/screener/screen/read.py` (new `choose_match`; `read_security`)
- Modify: `src/screener/screen/__init__.py`
- Create: `tests/test_screen_match.py`
- Modify: `tests/test_screen_explain.py`, `tests/test_screen_security.py`

**Interfaces:**
- Consumes: `SymbolRow(security_id: int, symbol: str, name: str, mic: str, is_active: bool)`, `UnknownSymbol(symbol)`, `AmbiguousSymbol(symbol, exchanges: tuple[str, ...])`, all existing.
- Produces: `screener.screen.choose_match(symbol: str, matches: Sequence[L]) -> L`, where `L` is any row with read-only `mic: str` and `is_active: bool`. It raises `UnknownSymbol` for no match and `AmbiguousSymbol` otherwise when it cannot choose. `screener.screen.FUNDAMENTALS` no longer exists. `DEPENDS` becomes `{"momentum": {PRICE}, "valuation": {PRICE}, "quality": frozenset()}`.

- [ ] **Step 1: Write the failing pure tests for `choose_match`**

Create `tests/test_screen_match.py`:

```python
"""Which security a symbol names when more than one holds it (ui-swap spec D10, §13).

`universe load` leaves a departed security's symbol row open, so a reused ticker
matches the security that left as well as the one trading under it now. The page
and Steven have nothing but the symbol to go on, so refusing that as ambiguous
would make the active security unreachable from both.
"""

import pytest

from screener.screen import AmbiguousSymbol, SymbolRow, UnknownSymbol, choose_match


def listing(security_id: int, mic: str = "XNAS", *, active: bool = True) -> SymbolRow:
    return SymbolRow(security_id, "ABC", "ABC Inc", mic, active)


def test_no_match_is_an_unknown_symbol():
    with pytest.raises(UnknownSymbol):
        choose_match("ABC", [])


def test_a_single_match_is_chosen_whether_or_not_it_is_active():
    assert choose_match("ABC", [listing(1, active=False)]).security_id == 1


def test_the_only_active_match_is_preferred_over_a_departed_one():
    assert choose_match("ABC", [listing(1, active=False), listing(2, "XNYS")]).security_id == 2


def test_two_active_matches_are_ambiguous_and_name_the_active_exchanges():
    with pytest.raises(AmbiguousSymbol) as caught:
        choose_match(
            "ABC", [listing(1, "XNYS"), listing(2, "XNAS"), listing(3, "BATS", active=False)]
        )

    assert caught.value.exchanges == ("XNAS", "XNYS")


def test_several_departed_matches_are_ambiguous():
    with pytest.raises(AmbiguousSymbol) as caught:
        choose_match("ABC", [listing(1, "XNYS", active=False), listing(2, active=False)])

    assert caught.value.exchanges == ("XNAS", "XNYS")
```

- [ ] **Step 2: Rewrite the refreshed-input tests to expect prices only**

In `tests/test_screen_explain.py`, change the import line to:

```python
from screener.screen import DEPENDS, PRICE, Check, Reproduction, RunRow, check, view_offset
```

Replace the whole `@pytest.mark.parametrize("refreshed,pillar,status", ...)` test `test_refreshed_inputs_skip_only_the_metrics_that_read_them` with:

```python
@pytest.mark.parametrize(
    "pillar,status",
    [("momentum", "refreshed"), ("valuation", "refreshed"), ("quality", "ok")],
)
def test_refreshed_prices_skip_only_the_metrics_that_read_a_close(pillar, status):
    reproduction = Reproduction(RUN.started_at, {"m": Decimal(1)}, frozenset({"price"}))

    assert check(pillar=pillar, code="m", stored=Decimal(1), reproduction=reproduction).status == status


def test_only_a_price_input_can_be_refreshed():
    # Facts are appended, never rewritten, so a fact observed after the run is
    # outside its view rather than a change to what it saw (§13).
    assert frozenset().union(*DEPENDS.values()) == {PRICE}
```

In `tests/test_screen_security.py`, replace `test_a_fact_restamped_since_the_run_marks_fundamentals_refreshed` with:

```python
def test_a_fact_appended_since_the_run_is_outside_the_view_rather_than_a_refresh(
    fresh_db, scored, an_observation
):
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    with fresh_db.cursor() as cur:
        written = insert_facts(
            cur, scored["S03"], an_observation(scored["S03"]), later,
            [Fact("net_income", QUARTERS[0], "Q", Decimal("999"), "USD")],
            latest_values(cur, scored["S03"]), metric_ids(cur),
        )

    assert written == 1
    assert _reproduce(fresh_db, scored["S03"], scored["run"]).refreshed == frozenset()
```

Replace `test_a_fact_restamped_since_the_run_leaves_momentum_compared` with:

```python
def test_a_fact_appended_since_the_run_leaves_every_metric_compared(fresh_db, scored, an_observation):
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    with fresh_db.cursor() as cur:
        insert_facts(
            cur, scored["S03"], an_observation(scored["S03"]), later,
            [Fact("net_income", QUARTERS[0], "Q", Decimal("999"), "USD")],
            latest_values(cur, scored["S03"]), metric_ids(cur),
        )

    shown = detail(fresh_db, "S03")

    assert set(statuses(shown).values()) == {"ok"}
    assert shown["reproduction"]["refreshed_inputs"] == []
```

After `test_a_symbol_listed_on_two_exchanges_is_ambiguous`, add:

```python
def test_a_symbol_also_held_by_a_departed_security_resolves_to_the_active_one(fresh_db, scored):
    departed = fresh_db.execute(
        """insert into security
           (name, mic, currency, country, primary_symbol, first_seen, is_active)
           values ('S05 Departed', 'XNYS', 'USD', 'US', 'S05', '2020-01-01', false)
           returning id"""
    ).fetchone()[0]
    fresh_db.execute(
        """insert into security_symbol (security_id, symbol, mic, valid_from, source)
           values (%s, 'S05', 'XNYS', '2020-01-01', 'test')""",
        (departed,),
    )

    shown = detail(fresh_db, "S05")

    assert (shown["scored"], shown["name"]) == (True, "S05 Inc")
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_screen_match.py tests/test_screen_explain.py tests/test_screen_security.py -n0 -q`
Expected: FAIL. The imports fail on `choose_match` and `DEPENDS`/`PRICE`, which is an import error for two files.

- [ ] **Step 4: Make only prices refresh**

In `src/screener/screen/queries.py`, replace the `REFRESHED` statement and its comment with:

```python
# D13, amended (§13): whether a bar the run read was rewritten after it started.
# Bars only, and only inside the momentum window, which is all a run reads of
# them. A fact is appended, never rewritten, so one observed later is outside
# the run's view rather than a change to what it saw.
REFRESHED: LiteralString = """
select exists (select 1 from price_daily
                where security_id = %(id)s
                  and trade_date > %(start)s
                  and trade_date <= %(as_of)s
                  and observed_at > %(started_at)s)
"""
```

In `src/screener/screen/explain.py`, replace the `PRICE`/`FUNDAMENTALS` constants and `DEPENDS` with:

```python
PRICE = "price"

# D13, amended (§13): only a price input can change under a run. Ingest rewrites
# a bar inside the settling window in place, so the close the run read is gone;
# `fundamental_fact` is append-only, so a fact observed after the run is outside
# the view and reproduction still sees exactly what the run saw. Momentum reads
# bars and every Valuation ratio divides by a market cap built from a close;
# Quality reads facts alone and is always compared.
DEPENDS: dict[str, frozenset[str]] = {
    "momentum": frozenset({PRICE}),
    "valuation": frozenset({PRICE}),
    "quality": frozenset(),
}
```

At the end of `reproduce`, replace:

```python
    prices, fundamentals = changed if changed is not None else (False, False)
    refreshed = frozenset(
        name for name, flag in ((PRICE, prices), (FUNDAMENTALS, fundamentals)) if flag
    )
    return Reproduction(visible_through, values, refreshed)
```

with:

```python
    refreshed = frozenset({PRICE}) if changed is not None and changed[0] else frozenset()
    return Reproduction(visible_through, values, refreshed)
```

In `src/screener/screen/__init__.py`, delete `FUNDAMENTALS` from the `explain` import and from `__all__`.

- [ ] **Step 5: Add `choose_match` and use it**

In `src/screener/screen/read.py`, change the typing import to `from typing import Any, LiteralString, Protocol, TypeVar`. Change the `AmbiguousSymbol` docstring to `"""Several securities hold this symbol and none can be preferred (D10, §13)."""`. After `AmbiguousSymbol`, add:

```python
class Listing(Protocol):
    """What choosing between matches needs of a row. Properties, so a frozen
    dataclass satisfies it."""

    @property
    def mic(self) -> str: ...

    @property
    def is_active(self) -> bool: ...


L = TypeVar("L", bound=Listing)


def choose_match(symbol: str, matches: Sequence[L]) -> L:
    """The one security a symbol names, preferring the only active one (D10, §13).

    `universe load` leaves a departed security's symbol row open, so a reused
    ticker matches the security that left as well as the one trading under it
    now. The page and Steven have nothing but the symbol to go on, so refusing
    that as ambiguous would make the active security unreachable from both.
    """
    if not matches:
        raise UnknownSymbol(symbol)
    if len(matches) == 1:
        return matches[0]
    active = [match for match in matches if match.is_active]
    if len(active) == 1:
        return active[0]
    raise AmbiguousSymbol(symbol, tuple(sorted(match.mic for match in (active or matches))))
```

In `read_security`, replace:

```python
    matches = _all(conn, queries.SYMBOL_MATCH, {"symbol": params.symbol}, SymbolRow)
    if not matches:
        raise UnknownSymbol(params.symbol)
    if len(matches) > 1:
        raise AmbiguousSymbol(params.symbol, tuple(sorted(match.mic for match in matches)))
    match = matches[0]
    security_id = match.security_id
```

with:

```python
    match = choose_match(
        params.symbol, _all(conn, queries.SYMBOL_MATCH, {"symbol": params.symbol}, SymbolRow)
    )
    security_id = match.security_id
```

In `src/screener/screen/__init__.py`, add `choose_match` to the `read` import and to `__all__`, between `"check"` and `"closes"`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_screen_match.py tests/test_screen_explain.py tests/test_screen_security.py tests/test_screen_http.py -n0 -q`
Expected: PASS. `test_a_symbol_on_two_exchanges_is_409` still passes, because both of its securities are active.

- [ ] **Step 7: Typecheck and commit**

Run: `pyright` → `0 errors`.

```bash
git add src/screener/screen tests/test_screen_match.py tests/test_screen_explain.py tests/test_screen_security.py
git commit -m "Refresh on prices only and prefer the one active security holding a symbol"
```

---

### Task 2: `playground.select` takes named parameters

**Files:**
- Modify: `src/screener/playground/engine.py` (`select`, `_execute`, the `collections.abc` import)
- Test: `tests/test_playground.py`

**Interfaces:**
- Produces: `playground.select(query: LiteralString, params: Sequence[Any] | Mapping[str, Any] | None = None, limit: int = config.DEFAULT_ROWS) -> Result`.

- [ ] **Step 1: Write the failing test**

In `tests/test_playground.py`, add `select` to the `from screener.playground import (...)` list, in alphabetical position after `run`. After `test_a_second_statement_after_a_semicolon_is_refused`, add:

```python
def test_a_repository_query_takes_named_parameters(steven):
    # `screener.screen`'s statements are named, and Steven's chart reads them
    # through here (ui-swap plan (b) P6).
    assert select("select %(n)s::int + 1 as n", {"n": 41}).rows == ((42,),)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_playground.py::test_a_repository_query_takes_named_parameters -n0 -q`
Expected: it passes at runtime, because psycopg accepts a mapping already, but `pyright` reports `Argument of type "dict[str, int]" cannot be assigned to parameter "params"`. Run `pyright tests/test_playground.py` to see that error; that error is the failure this task fixes.

- [ ] **Step 3: Widen the signature**

In `src/screener/playground/engine.py`, change `from collections.abc import Sequence` to `from collections.abc import Mapping, Sequence`. Change both signatures:

```python
def select(
    query: LiteralString,
    params: Sequence[Any] | Mapping[str, Any] | None = None,
    limit: int = config.DEFAULT_ROWS,
) -> Result:
```

```python
def _execute(
    query: LiteralString, params: Sequence[Any] | Mapping[str, Any] | None, limit: int
) -> Result:
```

Add one sentence to the end of `select`'s docstring: `Parameters may be positional or named; \`screener.screen\`'s statements are named.`

- [ ] **Step 4: Verify**

Run: `pyright` → `0 errors`; `.venv/bin/python -m pytest tests/test_playground.py -n0 -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/screener/playground/engine.py tests/test_playground.py
git commit -m "Let a repository query pass named parameters to the playground"
```

---

### Task 3: Steven's chart draws real adjusted closes

**Files:**
- Modify: `src/screener/screen/queries.py` (append `CHART_SECURITY`)
- Modify: `src/screener/screen/rows.py` (append `ChartSecurityRow`)
- Modify: `src/screener/screen/__init__.py`
- Rewrite: `src/screener/bot/tools/charts.py`
- Rewrite: `tests/test_charts.py`
- Delete: `src/screener/concept/`

**Interfaces:**
- Consumes: `choose_match` (Task 1), named-parameter `playground.select` (Task 2). From piece (b), all exported from `screener.screen`: `closes(bars, actions, count) -> list[list[str]]`, `price(Decimal) -> str`, `one_decimal(Decimal | None) -> str | None`, `partial(Decimal) -> bool`, `parse_all`, `BarRow`, `ActionRow`, `CHART_CLOSES = 60`, `CLOSE_LOOKBACK_DAYS = 120`, `MAX_SYMBOL`. `LOGIC_DESCRIPTION` comes from `screener.scoring`.
- Produces:
  - `screener.screen.CHART_SECURITY`, `CLOSES`, `ACTIONS` (LiteralStrings).
  - `screener.screen.ChartSecurityRow`.
  - `screener.bot.tools.charts`: `MARKS = ("peak", "low", "surge", "drop", "latest")`, `UNAVAILABLE: str`, `annotate(mark: str, values: Sequence[Decimal], days: Sequence[date]) -> tuple[Mark | None, str]`, `Chart(ticker, title, subtitle, series: tuple[Decimal, ...], dates: tuple[str, ...], marks=())`.
  - `Chart.payload()` returns `{"kind": "price", "ticker", "title", "subtitle", "series": list[float], "dates", "marks"}`, with no `median` or `threshold`. Task 6's renderer draws this shape.

- [ ] **Step 1: Write the new `tests/test_charts.py`**

Replace the whole file with:

```python
"""The chart tool: Steven draws a security's adjusted price from real data.

Three things are defended here:
- The mark Steven puts on a chart is computed from the series, never chosen by a
  model. A marker in the wrong place is a lie told precisely.
- The figures are the stored bars, adjusted by scoring's own rule and read as
  Steven's read-only role, captioned with the scores the screen serves
  (ui-swap spec D17).
- A chart still reaches Discord as the same payload the browser draws.

The database fixtures are written here rather than borrowed from
`test_screen_read.py`: this repository keeps no test-helper modules, and Steven
needs a sliver of that world -- a few securities, sixty-odd bars, one night.
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
import pytest
from psycopg.conninfo import make_conninfo

from screener import playground
from screener.bot import render
from screener.bot.tools import TOOLS, Chart, dispatch
from screener.bot.tools.charts import MARKS, UNAVAILABLE, annotate, collecting
from screener.scoring import LOGIC_DESCRIPTION

AS_OF = date(2026, 3, 2)
PASSWORD = "throwaway-for-this-test"


def weekdays(last: date, count: int) -> list[date]:
    """`count` weekdays ending on `last`, oldest first."""
    days: list[date] = []
    day = last
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return days[::-1]


def draw(ticker: str, mark: str = "") -> tuple[str, list[Chart]]:
    with collecting() as drawn:
        said = dispatch("chart", {"ticker": ticker, "mark": mark})
    return said, list(drawn)


# -- marks, on a fixed series ------------------------------------------------

# A rise of 20 from 200 (+10%) and a rise of 10 from 50 (+20%): the surge is
# the second, because a move is measured in percent, not in price points.
SERIES = [Decimal(v) for v in ("200", "220", "215", "50", "55", "60", "58", "57", "56", "55")]
DAYS = weekdays(AS_OF, len(SERIES))


def _day(day: date) -> str:
    return f"{day.day} {day:%b}"


def test_the_tool_offers_price_marks_and_never_a_crossing():
    description = TOOLS["chart"].description
    assert "adjusted price" in description
    for mark in MARKS:
        assert mark in description
    assert "crossing" not in description and "crossing" not in MARKS


def test_a_peak_and_a_low_are_found_in_the_series_and_labelled_as_prices():
    peak, said = annotate("peak", SERIES, DAYS)
    low, _ = annotate("low", SERIES, DAYS)

    assert peak is not None and (peak.kind, peak.index, peak.label) == ("point", 1, "peak 220.00")
    assert said == f"Peak 220.00 on {_day(DAYS[1])}."
    assert low is not None and (low.index, low.label) == (3, "low 50.00")


def test_a_surge_is_the_steepest_percent_rise_and_says_how_long_it_took():
    surge, said = annotate("surge", SERIES, DAYS)

    assert surge is not None and (surge.kind, surge.index, surge.end) == ("span", 3, 5)
    assert surge.label == "+20.0% over 2d"
    assert said == f"Biggest surge +20.0% over 2d, {_day(DAYS[3])} to {_day(DAYS[5])}."


def test_a_drop_is_the_steepest_percent_fall():
    drop, _ = annotate("drop", SERIES, DAYS)

    assert drop is not None and (drop.index, drop.end, drop.label) == (1, 3, "-77.3% over 2d")


def test_latest_marks_the_last_close():
    latest, _ = annotate("latest", SERIES, DAYS)

    assert latest is not None and (latest.index, latest.label) == (9, "now 55.00")


def test_a_close_of_a_thousand_or_more_is_labelled_without_decimals():
    peak, _ = annotate("peak", [Decimal("1500"), Decimal("1601")], DAYS[:2])

    assert peak is not None and peak.label == "peak 1601"


def test_a_flat_series_has_no_surge_to_mark():
    assert annotate("surge", [Decimal("100")] * 5, DAYS[:5]) == (
        None, "No meaningful surge in this window.",
    )


def test_an_unknown_mark_is_refused_before_anything_is_read():
    for mark in ("wibble", "crossing"):
        said, drawn = draw("ABC", mark)
        assert drawn == [] and said.startswith("error: mark must be one of")


def test_the_payload_is_a_price_chart_with_no_score_lines():
    chart = Chart(
        ticker="ABC", title="t", subtitle="s",
        series=(Decimal("272.10"), Decimal("1500")), dates=("2026-02-27", "2026-03-02"),
    )

    body = json.loads(json.dumps(chart.payload()))

    assert body["kind"] == "price"
    assert body["series"] == [272.1, 1500.0]
    assert "median" not in body and "threshold" not in body


# -- the tool, against the database as Steven's role -------------------------


@pytest.fixture
def steven(fresh_db, db_url, monkeypatch):
    """Steven's read-only role, provisioned as `test_playground.py` provisions it:
    through `ensure_password`, so what is tested is the role that ships."""
    monkeypatch.setenv("PLAYGROUND_DB_PASSWORD", PASSWORD)
    monkeypatch.setenv("PLAYGROUND_BOT_DB_PASSWORD", PASSWORD)
    playground.ensure_password(fresh_db)
    monkeypatch.setenv(
        "PLAYGROUND_DATABASE_URL",
        make_conninfo(db_url, user="playground_bot", password=PASSWORD),
    )
    return fresh_db


@dataclass
class Market:
    # `Any`, because the fixture's connection is untyped and a typed one would make
    # every `fetchone()[0]` below an optional-subscript error.
    conn: Any
    observe: Callable[[int], int]

    def security(
        self, symbol: str, *, active: bool = True, mic: str = "XNAS", name: str | None = None
    ) -> int:
        security_id = self.conn.execute(
            """insert into security
               (name, mic, currency, country, primary_symbol, first_seen, is_active)
               values (%s, %s, 'USD', 'US', %s, '2020-01-01', %s) returning id""",
            (name or f"{symbol} Inc", mic, symbol, active),
        ).fetchone()[0]
        self.conn.execute(
            """insert into security_symbol (security_id, symbol, mic, valid_from, source)
               values (%s, %s, %s, '2020-01-01', 'test')""",
            (security_id, symbol, mic),
        )
        return security_id

    def bars(self, security_id: int, closes: Sequence[str], *, last: date = AS_OF) -> list[date]:
        """One bar per weekday ending on `last`, each fetched the evening it closed."""
        observation = self.observe(security_id)
        days = weekdays(last, len(closes))
        for day, close in zip(days, closes):
            value = Decimal(close)
            self.conn.execute(
                """insert into price_daily
                   (security_id, trade_date, open, high, low, close, volume, observed_at,
                    ingest_observation_id)
                   values (%s, %s, %s, %s, %s, %s, 1, %s, %s)""",
                (security_id, day, value, value, value, value,
                 datetime.combine(day, time(22), tzinfo=timezone.utc), observation),
            )
        return days

    def split(self, security_id: int, effective: date, ratio: str) -> None:
        self.conn.execute(
            """insert into corporate_action
               (security_id, effective_date, action_type, ratio, amount, currency,
                observed_at, ingest_observation_id)
               values (%s, %s, 'split', %s, null, 'USD', %s, %s)""",
            (security_id, effective, Decimal(ratio),
             datetime.combine(effective, time(22), tzinfo=timezone.utc),
             self.observe(security_id)),
        )

    def run(self, as_of: date = AS_OF) -> int:
        started = datetime.combine(as_of, time(23), tzinfo=timezone.utc)
        return self.conn.execute(
            """insert into scoring_run
               (as_of_range, cutoff_offset, logic_version_id, weight_version_id, status,
                emits_alerts, git_sha, config_hash, started_at, finished_at, outcome)
               select daterange(%(as_of)s, %(next)s, '[)'), interval '30 hours', l.id, w.id,
                      'live', false, 'abc1234', '\\x9f3a'::bytea, %(started)s, %(started)s, 'ok'
                 from scoring_logic_version l, weight_version w
                where l.description = %(logic)s and w.code = 'v2'
               returning id""",
            {"as_of": as_of, "next": as_of + timedelta(days=1), "started": started,
             "logic": LOGIC_DESCRIPTION},
        ).fetchone()[0]

    def snapshot(
        self, run_id: int, security_id: int, score: str, *,
        min_coverage: str = "1", pillars: dict[str, str], as_of: date = AS_OF,
    ) -> None:
        self.conn.execute(
            """insert into snapshot_daily
               (as_of, scoring_run_id, security_id, blended_score, pillar_agreement,
                min_coverage, worst_fallback_level)
               values (%s, %s, %s, %s, 1, %s, 1)""",
            (as_of, run_id, security_id, Decimal(score), Decimal(min_coverage)),
        )
        for code, pillar_score in pillars.items():
            self.conn.execute(
                """insert into pillar_score_daily
                   (as_of, scoring_run_id, security_id, pillar_id, score, metric_count, coverage)
                   select %s, %s, %s, p.id, %s, 1, 1 from pillar p where p.code = %s""",
                (as_of, run_id, security_id, Decimal(pillar_score), code),
            )


@pytest.fixture
def market(steven, an_observation) -> Market:
    return Market(steven, an_observation)


def test_a_scored_security_is_drawn_from_its_adjusted_closes_with_its_scores(market):
    security = market.security("ABC")
    market.bars(security, [str(100 + i) for i in range(70)])
    run = market.run()
    market.snapshot(
        run, security, "71.2", min_coverage="0.5", pillars={"valuation": "44.5", "momentum": "80"}
    )

    said, drawn = draw("$abc", "latest")

    (chart,) = drawn
    assert chart.ticker == "ABC"
    assert chart.title == "ABC — adjusted close, 60 trading days"
    assert len(chart.series) == 60 and chart.series[-1] == Decimal("169.00")
    assert chart.dates[-1] == AS_OF.isoformat()
    assert chart.subtitle == "ABC Inc · score 71.2 on 2 Mar · V 44.5 Q — M 80.0 · partial"
    assert chart.marks[0].label == "now 169.00"
    assert "169.00" in said and "score 71.2" in said
    assert "llustrative" not in said


def test_before_any_scored_night_the_prices_are_still_drawn(market):
    today = datetime.now(timezone.utc).date()
    market.bars(market.security("ABC"), ["10", "11", "12"], last=today)

    said, drawn = draw("ABC")

    (chart,) = drawn
    assert chart.series == (Decimal("10.00"), Decimal("11.00"), Decimal("12.00"))
    assert "not yet scored" in chart.subtitle and "not yet scored" in said


def test_a_security_the_night_did_not_score_says_so(market):
    market.bars(market.security("ABC"), ["10", "11"])
    market.run()

    _, drawn = draw("ABC")

    (chart,) = drawn
    assert chart.subtitle == "ABC Inc · not scored on 2 Mar"


def test_a_split_inside_the_window_leaves_the_line_continuous(market):
    security = market.security("SPLT")
    days = market.bars(security, ["100"] * 10 + ["50"] * 10)
    market.split(security, days[10], "2")

    _, drawn = draw("SPLT")

    (chart,) = drawn
    assert set(chart.series) == {Decimal("50.00")}


def test_a_security_that_left_the_universe_is_not_drawn(market):
    market.bars(market.security("GONE", active=False), ["10", "11"])

    assert draw("GONE") == ("GONE is no longer in the universe.", [])


def test_an_unknown_symbol_is_not_drawn(market):
    assert draw("ZZZZ") == ("ZZZZ is not a current symbol in the universe.", [])


def test_a_reused_symbol_draws_the_security_trading_under_it_now(market):
    market.bars(market.security("ABC", active=False, mic="XNYS", name="ABC Old"), ["999", "999"])
    market.bars(market.security("ABC"), ["10", "11"])

    _, drawn = draw("ABC")

    (chart,) = drawn
    assert chart.series[-1] == Decimal("11.00")
    assert chart.subtitle.startswith("ABC Inc")


def test_a_chart_costs_at_most_three_reads(market, monkeypatch):
    security = market.security("ABC")
    market.bars(security, ["10", "11"])
    market.snapshot(market.run(), security, "50", pillars={"momentum": "50"})
    seen: list[str] = []
    real = playground.select

    def counted(*args: Any, **kwargs: Any) -> Any:
        seen.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(playground, "select", counted)

    draw("ABC", "peak")

    assert len(seen) == 3


def test_without_a_role_the_chart_says_the_database_cannot_be_reached(monkeypatch):
    monkeypatch.delenv("PLAYGROUND_DATABASE_URL", raising=False)

    assert draw("ABC") == (UNAVAILABLE, [])


def test_an_unreachable_database_says_the_same(monkeypatch):
    monkeypatch.setenv("PLAYGROUND_DATABASE_URL", "postgresql://nobody:hunter2@127.0.0.1:1/none")

    assert draw("ABC") == (UNAVAILABLE, [])


def test_a_surface_that_cannot_draw_is_not_told_a_chart_is_shown(market):
    market.bars(market.security("ABC"), ["10", "12"])

    with collecting(False) as drawn:
        said = dispatch("chart", {"ticker": "ABC", "mark": "peak"})

    assert drawn == []
    assert "chart is shown" not in said
    assert "Peak 12.00" in said


def test_charts_do_not_leak_between_questions(market):
    market.bars(market.security("ABC"), ["10", "11"])
    market.bars(market.security("XYZ"), ["20", "21"])

    _, first = draw("ABC")
    _, second = draw("XYZ")

    assert [c.ticker for c in first] == ["ABC"]
    assert [c.ticker for c in second] == ["XYZ"]


# -- rasterising for Discord -------------------------------------------------


def _chart(ticker: str = "ABC") -> Chart:
    return Chart(
        ticker=ticker, title=f"{ticker} — adjusted close, 2 trading days", subtitle="s",
        series=(Decimal("10.00"), Decimal("11.00")), dates=("2026-02-27", "2026-03-02"),
    )


def test_a_chart_is_posted_to_the_renderer_as_its_own_payload():
    # The same JSON the browser receives, so the PNG cannot be drawn from a
    # different shape than the one on screen.
    sent: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"0" * 40)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        png = render.chart_png(_chart(), client=client)

    assert png is not None and png.startswith(b"\x89PNG")
    assert sent == _chart().payload()


def test_a_renderer_that_fails_costs_the_picture_not_the_answer():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="nope")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert render.chart_png(_chart(), client=client) is None


def test_something_that_is_not_a_png_is_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html="<!doctype html><title>Sign in</title>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert render.chart_png(_chart(), client=client) is None


def test_no_more_than_three_charts_are_attached_to_one_reply():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n")

    charts = tuple(_chart(symbol) for symbol in ("NVDA", "AMD", "MU", "JPM", "CAT"))
    original = httpx.Client
    try:
        httpx.Client = lambda **kw: original(transport=httpx.MockTransport(handler))  # type: ignore[assignment]
        drawn_files = render.chart_pngs(charts)
    finally:
        httpx.Client = original  # type: ignore[assignment]

    assert len(drawn_files) == render.MAX_CHARTS
    # Named for the ticker, so a saved image says what it is.
    assert drawn_files[0][0] == "nvda.png"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_charts.py -n0 -q`
Expected: FAIL at import, because `UNAVAILABLE` and `annotate` do not exist in `screener.bot.tools.charts`.

- [ ] **Step 3: Add the chart's first read and its row**

Append to `src/screener/screen/queries.py`:

```python
# D17 (plan C1): Steven's first read. Every security holding the symbol, each with
# the screen's night and its snapshot and pillar scores on that night, so the
# §13 preference for the one active match is made in Python over all of them.
# The run columns are named because `LATEST_RUN` leaves several unnamed.
CHART_SECURITY: LiteralString = """
with served (id, as_of, started_at, finished_at, git_sha, config_hash, weight_version,
             weight_version_id, cutoff_offset_seconds, logic, emits_alerts) as (
""" + LATEST_RUN + """
)
select distinct on (s.id) s.id, sy.symbol, s.name, sy.mic, s.is_active,
       served.id, served.as_of, snap.blended_score, snap.min_coverage,
       v.score, q.score, m.score
  from security_symbol sy
  join security s on s.id = sy.security_id
  left join served on true
  left join snapshot_daily snap
    on snap.scoring_run_id = served.id and snap.as_of = served.as_of
   and snap.security_id = s.id
  left join pillar_score_daily v
    on v.scoring_run_id = served.id and v.as_of = served.as_of and v.security_id = s.id
   and v.pillar_id = (select id from pillar where code = 'valuation')
  left join pillar_score_daily q
    on q.scoring_run_id = served.id and q.as_of = served.as_of and q.security_id = s.id
   and q.pillar_id = (select id from pillar where code = 'quality')
  left join pillar_score_daily m
    on m.scoring_run_id = served.id and m.as_of = served.as_of and m.security_id = s.id
   and m.pillar_id = (select id from pillar where code = 'momentum')
 where upper(sy.symbol) = upper(%(symbol)s::text)
   and sy.valid_to is null
 order by s.id, sy.mic
"""
```

Append to `src/screener/screen/rows.py`, after `MetricInfoRow`:

```python
@dataclass(frozen=True)
class ChartSecurityRow:
    """A symbol's match with the screen's night, for Steven's chart (D17).

    `run_id` onward is None when no v2 night qualifies; `score` onward, when that
    night did not score this security.
    """

    security_id: int
    symbol: str
    name: str
    mic: str
    is_active: bool
    run_id: int | None
    as_of: date | None
    score: Decimal | None
    min_coverage: Decimal | None
    v_score: Decimal | None
    q_score: Decimal | None
    m_score: Decimal | None
```

In `src/screener/screen/__init__.py`:
- Add `ChartSecurityRow` to the `rows` import.
- Add a new import: `from screener.screen.queries import ACTIONS, CHART_SECURITY, CLOSES`.
- Add to `__all__`, in RUF022 order: `"ACTIONS"` after `"ABSENT"`; `"CHART_SECURITY"` after `"CHART_CLOSES"`; `"CLOSES"` before `"CLOSE_LOOKBACK_DAYS"`; `"ChartSecurityRow"` after `"BarRow"`.
- Extend the module docstring's last sentence with: `Steven reads \`CHART_SECURITY\`, \`CLOSES\` and \`ACTIONS\` through \`playground.select\` and parses them with the same records.`

If `ruff` is installed, run `ruff check --select RUF022 src/screener/screen/__init__.py` and apply its order.

- [ ] **Step 4: Rewrite the tool**

Replace `src/screener/bot/tools/charts.py` with:

```python
"""The chart tool: Steven draws a security's adjusted price and marks a point on it.

Three decisions shape this module.

**The series never reaches the model.** Sixty points is more than the whole tool
budget, and a tool result is context on every subsequent round, so sending the
data would be paid for repeatedly to tell the model something it cannot read as
well as a chart can. The model gets a sentence; the chart travels beside the
reply through `collecting()` and is rendered by whoever asked.

**The model chooses the question, the data answers it.** `mark` names *what* to
find -- a peak, a surge -- and the index is then computed from the real series
here. Letting the model supply coordinates would be asking it to invent where a
marker goes, which is the same failure as inventing a number, and it would be
wrong in the most convincing possible way: drawn on a chart, precisely, in the
wrong place.

**The figures are real, and read as Steven.** The closes are the stored bars,
adjusted by scoring's own rule, and the caption's scores are the night the
dashboard serves. Both are read through `playground.select` as Steven's
read-only role, using `screener.screen`'s own statements and records, in three
reads (ui-swap spec D17). Before this the chart worked without a database; now
it cannot, and that is the point.
"""

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from screener import playground, screen
from screener.bot.tools.registry import tool
from screener.scoring import LOGIC_DESCRIPTION

logger = logging.getLogger(__name__)

# The longest run "biggest surge" is allowed to span. Without a ceiling the
# answer is almost always the whole window, which is a trend rather than a
# surge; with one the label can say how many days it took and be checked.
RUN_DAYS = 10

# What `mark` accepts. Listed in the tool description so the model picks from
# the set rather than guessing a word this module will not recognise. No
# crossing: that needs score history and alerting, and neither exists yet.
MARKS = ("peak", "low", "surge", "drop", "latest")

UNAVAILABLE = "error: the chart is unavailable because the database cannot be reached."


@dataclass(frozen=True, slots=True)
class Mark:
    """Something drawn on the line: one point, or a span between two."""

    kind: str  # "point" | "span"
    index: int
    label: str
    end: int | None = None
    tone: str = "copper"


@dataclass(frozen=True, slots=True)
class Chart:
    """An adjusted price series, ready to draw. Sent to the browser, never to the model."""

    ticker: str
    title: str
    subtitle: str
    series: tuple[Decimal, ...]
    dates: tuple[str, ...]
    marks: tuple[Mark, ...] = field(default_factory=tuple)

    def payload(self) -> dict[str, object]:
        """The shape sent to the browser and to the renderer.

        `kind` is "price", so the renderer draws the data's own range and no
        score threshold or median (D16). Closes travel as floats here, unlike
        the screen endpoints' decimal strings: they are coordinates for a line,
        and D5's precision is already in the labels, written from the Decimals.
        """
        return {
            "kind": "price",
            "ticker": self.ticker,
            "title": self.title,
            "subtitle": self.subtitle,
            "series": [float(v) for v in self.series],
            "dates": list(self.dates),
            "marks": [
                {
                    "kind": m.kind, "index": m.index, "label": m.label,
                    "end": m.end, "tone": m.tone,
                }
                for m in self.marks
            ],
        }


# Charts produced while answering one question. A ContextVar rather than a
# module global because `agent._think` runs on a worker thread per request:
# `asyncio.to_thread` copies the context, so two people asking at once cannot
# be handed each other's charts.
_PENDING: ContextVar[list[Chart] | None] = ContextVar("pending_charts", default=None)


@contextmanager
def collecting(enabled: bool = True) -> Iterator[list[Chart]]:
    """Gather whatever the tools drew while answering one question.

    `enabled=False` still yields a list, always empty. It is how a surface that
    cannot render a chart says so: nothing is collected, and the tool sees that
    and does not tell the model a chart is on screen.
    """
    charts: list[Chart] = []
    token = _PENDING.set(charts if enabled else None)
    try:
        yield charts
    finally:
        _PENDING.reset(token)


def _day(day: date) -> str:
    """`2026-09-05` as `5 Sep`. Short, because it goes in a label and a prompt."""
    return f"{day.day} {day:%b}"


def _peak(values: Sequence[Decimal]) -> int:
    return max(range(len(values)), key=lambda i: values[i])


def _low(values: Sequence[Decimal]) -> int:
    return min(range(len(values)), key=lambda i: values[i])


def _percent(start: Decimal, end: Decimal) -> Decimal:
    return (end / start - 1) * 100


def _run(values: Sequence[Decimal], rising: bool) -> tuple[int, int]:
    """The steepest percent rise (or fall) over at most `RUN_DAYS`, as (start, end).

    Percent rather than price points, so a surge means the same thing for a
    $20 stock as for a $2,000 one.
    """
    best = (0, 0)
    best_move = Decimal(0)
    for start in range(len(values)):
        if values[start] <= 0:
            continue
        for end in range(start + 1, min(start + RUN_DAYS, len(values) - 1) + 1):
            move = _percent(values[start], values[end])
            if (move > best_move) if rising else (move < best_move):
                best_move, best = move, (start, end)
    return best


def annotate(
    mark: str, values: Sequence[Decimal], days: Sequence[date]
) -> tuple[Mark | None, str]:
    """Turn a requested mark into something drawn and something said.

    Returns the annotation and the one-line answer for the model, so the label
    on the chart and the sentence in the reply are computed once from the same
    numbers and cannot disagree.
    """
    if mark == "peak":
        i = _peak(values)
        return (
            Mark("point", i, f"peak {screen.price(values[i])}", tone="copper"),
            f"Peak {screen.price(values[i])} on {_day(days[i])}.",
        )
    if mark == "low":
        i = _low(values)
        return (
            Mark("point", i, f"low {screen.price(values[i])}", tone="blue"),
            f"Low {screen.price(values[i])} on {_day(days[i])}.",
        )
    if mark in ("surge", "drop"):
        start, end = _run(values, rising=mark == "surge")
        if end == start:
            return None, f"No meaningful {mark} in this window."
        move = f"{_percent(values[start], values[end]):+.1f}% over {end - start}d"
        return (
            Mark("span", start, move, end=end, tone="copper" if mark == "surge" else "blue"),
            f"Biggest {mark} {move}, {_day(days[start])} to {_day(days[end])}.",
        )
    if mark == "latest":
        i = len(values) - 1
        return (
            Mark("point", i, f"now {screen.price(values[i])}", tone="copper"),
            f"Latest {screen.price(values[i])} on {_day(days[i])}.",
        )
    return None, ""


def _caption(match: screen.ChartSecurityRow) -> str:
    """The scores on the screen's night, as the dashboard would show them (D17)."""
    if match.run_id is None or match.as_of is None:
        return "not yet scored"
    if match.score is None:
        return f"not scored on {_day(match.as_of)}"
    pillars = " ".join(
        f"{key} {screen.one_decimal(value) or '—'}"
        for key, value in (("V", match.v_score), ("Q", match.q_score), ("M", match.m_score))
    )
    partial = (
        " · partial"
        if match.min_coverage is not None and screen.partial(match.min_coverage)
        else ""
    )
    return f"score {screen.one_decimal(match.score)} on {_day(match.as_of)} · {pillars}{partial}"


def _security(symbol: str) -> screen.ChartSecurityRow:
    """Read one: the symbol's matches with the screen's night, then the one meant."""
    found = playground.select(
        screen.CHART_SECURITY, {"symbol": symbol, "logic": LOGIC_DESCRIPTION}
    )
    return screen.choose_match(symbol, screen.parse_all(screen.ChartSecurityRow, found.rows))


def _closes(security_id: int, as_of: date) -> list[tuple[date, Decimal]]:
    """Reads two and three: bars and corporate actions, adjusted as the dashboard adjusts them.

    No `observed_at` bound, for the reason `screen.read` gives (D11): ingest
    re-stamps the last week of bars nightly.
    """
    bind = {
        "ids": [security_id],
        "as_of": as_of,
        "since": as_of - timedelta(days=screen.CLOSE_LOOKBACK_DAYS),
        "count": screen.CHART_CLOSES,
    }
    bars = screen.parse_all(
        screen.BarRow, playground.select(screen.CLOSES, bind, screen.CHART_CLOSES).rows
    )
    actions = screen.parse_all(screen.ActionRow, playground.select(screen.ACTIONS, bind).rows)
    return [
        (date.fromisoformat(day), Decimal(close))
        for day, close in screen.closes(bars, actions, screen.CHART_CLOSES)
    ]


@tool(
    "chart",
    "Draw a ticker's 60-day adjusted price. mark: " + "|".join(MARKS) + " marks that point.",
)
def chart(ticker: str, mark: str = "") -> str:
    """Register a chart and describe it in one line.

    The return value is what the model reads. It carries the figures worth
    stating in a sentence and nothing else, because the reader can see the rest.
    """
    wanted = mark.strip().lower()
    if wanted and wanted not in MARKS:
        return f"error: mark must be one of {'|'.join(MARKS)}"
    symbol = ticker.strip().removeprefix("$").upper()
    if not symbol or len(symbol) > screen.MAX_SYMBOL:
        return "error: give a ticker symbol, such as NVDA"
    if not playground.enabled():
        return UNAVAILABLE

    try:
        match = _security(symbol)
        if not match.is_active:
            return f"{match.symbol} is no longer in the universe."
        # With no scored night yet, the prices up to today (plan C2).
        closes = _closes(match.security_id, match.as_of or datetime.now(timezone.utc).date())
    except (playground.NotConfigured, playground.Unavailable):
        return UNAVAILABLE
    except screen.UnknownSymbol:
        return f"{symbol} is not a current symbol in the universe."
    except screen.AmbiguousSymbol as exc:
        return f"{symbol} is listed on {', '.join(exc.exchanges)}; say which exchange."

    caption = _caption(match)
    if len(closes) < 2:
        return f"{match.symbol} has no stored prices to draw yet; {caption}."
    days = [day for day, _ in closes]
    values = [close for _, close in closes]
    annotation, answer = annotate(wanted, values, days)

    charts = _PENDING.get()
    if charts is None:
        # Nothing is collecting, so the chart would be drawn and dropped. The
        # model still gets the figures rather than an error it has to explain.
        logger.info("chart(%s) called outside a collecting context", match.symbol)
    else:
        charts.append(
            Chart(
                ticker=match.symbol,
                title=f"{match.symbol} — adjusted close, {len(values)} trading days",
                subtitle=f"{match.name} · {caption}",
                series=tuple(values),
                dates=tuple(day.isoformat() for day in days),
                marks=(annotation,) if annotation else (),
            )
        )

    head = (
        f"{match.symbol} ({match.name}) adjusted close over {len(values)} trading days "
        f"to {_day(days[-1])}: latest {screen.price(values[-1])}; {caption}."
    )
    # Only claim a chart exists where one is actually rendered, so the model
    # never refers the reader to something that is not there.
    tail = (
        "The chart is shown to them; answer in a sentence, do not list the numbers."
        if charts is not None
        else ""
    )
    return " ".join(part for part in (head, answer, tail) if part)
```

- [ ] **Step 5: Delete the concept package**

```bash
git rm -r src/screener/concept
grep -rn "screener.concept\|from screener import concept" src tests
```

Expected: `grep` prints nothing.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_charts.py tests/test_bot.py tests/test_screen_security.py -n0 -q`
Expected: `tests/test_charts.py` passes. `tests/test_bot.py` may fail only on prompt tests that Task 4 rewrites; any other failure is real. If `test_a_chart_costs_at_most_three_reads` sees fewer than three calls, the tool is catching an exception. Print `said` to see it.

- [ ] **Step 7: Typecheck and commit**

Run: `pyright` → `0 errors`.

```bash
git add src/screener/screen src/screener/bot/tools/charts.py tests/test_charts.py
git commit -m "Draw Steven's chart from real adjusted closes and the served night's scores"
```

---

### Task 4: Steven's prompt stops calling his figures illustrative

**Files:**
- Modify: `src/screener/bot/agent.py` (`SYSTEM_PROMPT`)
- Modify: `tests/test_bot.py`

**Interfaces:**
- Consumes: `agent.SYSTEM_PROMPT`, `TOOLS["chart"].description` (Task 3).

- [ ] **Step 1: Write the failing test**

In `tests/test_bot.py`, after `test_the_prompt_stays_small_enough_to_send_on_every_message`, add:

```python
def test_the_prompt_neither_calls_chart_figures_illustrative_nor_offers_a_crossing():
    # The chart draws real adjusted closes now, and a crossing needs score
    # history and alerting, which do not exist (ui-swap spec D17).
    prompt = agent.SYSTEM_PROMPT.lower()
    assert "illustrative" not in prompt
    assert "crossing" not in prompt
```

In the budget test's comment, replace the sentence fragment `` `chart` draws invented concept numbers and `status` reports process facts. `` with `` `chart` drew invented concept numbers until the UI swap and `status` reports process facts. ``

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest "tests/test_bot.py::test_the_prompt_neither_calls_chart_figures_illustrative_nor_offers_a_crossing" -n0 -q`
Expected: FAIL on `"illustrative" not in prompt`.

- [ ] **Step 3: Rewrite the three lines of prompt**

In `src/screener/bot/agent.py`'s `SYSTEM_PROMPT`, replace rule 2 with:

```
2. Never invent a number. Figures come from tools only. No live prices: closes are stored end-of-day bars.
```

Replace the chart paragraph with:

```
For a ticker's price history, high, low, or biggest surge or drop: call `chart` with that mark. Its closes are real, adjusted for splits and dividends, and its scores come from the latest scored night. Where it draws, the point is marked and dated for them, so answer in one sentence rather than listing figures.
```

Replace the final "If asked" line with:

```
If asked: percentiles are sector-relative; the scored pillars are valuation, quality and momentum; alerts are not built yet, and will fire when a score crosses a threshold rather than while it sits above one; every score traces to its raw inputs.
```

- [ ] **Step 4: Run the bot tests**

Run: `.venv/bin/python -m pytest tests/test_bot.py tests/test_charts.py -n0 -q`
Expected: PASS. If `test_the_prompt_stays_small_enough_to_send_on_every_message` fails, **shorten the new wording until it passes. Do not raise the budget.** Keep all four facts: closes are real and adjusted, scores are from the latest night, three pillars are scored, and alerts are not built.

- [ ] **Step 5: Commit**

```bash
git add src/screener/bot/agent.py tests/test_bot.py
git commit -m "Tell Steven his chart's closes and scores are real"
```

---

### Task 5: Lint and typecheck the web app in CI

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify, with one comment line each: `web/app/audit/page.tsx:83`, `web/app/magpie/[id]/page.tsx:63`, `web/app/magpie/page.tsx:64`, `web/app/skybird/page.tsx:67,81`, `web/components/Ladder.tsx:80,86`, `web/components/MicButton.tsx:35`, `web/components/Palette.tsx:72`, `web/components/Sidebar.tsx:82`, `web/lib/steven.tsx:84,112`

**Interfaces:** none. After this task, `npm run lint` exits 0 in `web/` and the CI job enforces it for every later task.

- [ ] **Step 1: Confirm the baseline**

Run: `cd web && npm run lint 2>&1 | tail -3; cd ..`
Expected: `✖ 13 problems (12 errors, 1 warning)`. All 12 errors are `react-hooks/set-state-in-effect`, at the locations listed above. The warning is an unused `Thread` import in `Palette.tsx`, which does not fail lint and is left alone.

- [ ] **Step 2: Disable the rule at each existing site, with the reason**

On the line directly above each of the 12 reported lines, add the comment, indented to match:

```tsx
// eslint-disable-next-line react-hooks/set-state-in-effect -- reads browser state or starts a load on mount; predates lint in CI, and new code must pass the rule
```

Line numbers shift as comments go in, so work from the bottom of each file upward. Only a comment line may change in these files.

- [ ] **Step 3: Verify lint passes**

Run: `cd web && npm run lint && npx tsc --noEmit; cd ..`
Expected: `0 errors`, and `tsc` prints nothing.

- [ ] **Step 4: Add the CI job**

In `.github/workflows/ci.yml`, after the `test` job and before the `build` job's comment, add:

```yaml
  # The dashboard's lint and typecheck. `next build` in Next 16 does not lint, and
  # the image build that compiles the web app runs on pull requests only, so
  # without this a lint error fails nothing at all (ui-swap spec F8).
  web:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: web
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          # The major version `web/Dockerfile` builds on, so CI checks what ships.
          node-version: "22"
          cache: npm
          cache-dependency-path: web/package-lock.json
      - run: npm ci
      - run: npm run lint
      - run: npx tsc --noEmit
```

Change `ci-ok`'s `needs: [typecheck, test, build]` to `needs: [typecheck, test, web, build]`.

- [ ] **Step 5: Verify the workflow parses and commit**

Run: `.venv/bin/python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml'))" && echo ok`
Expected: `ok`. If PyYAML is not installed, run `.venv/bin/python -m pytest tests/test_compose.py -n0 -q`, or skip; GitHub validates the file on push.

```bash
git add .github/workflows/ci.yml web/app web/components web/lib
git commit -m "Lint and typecheck the dashboard in CI"
```

---

### Task 6: The one renderer gains a price mode

**Files:**
- Modify: `web/lib/threads.ts` (`ChartSpec`, the `SKILLS` comment)
- Modify: `web/lib/chart-svg.ts`
- Modify: `web/components/ChatChart.tsx`

**Interfaces:**
- Consumes: Task 3's payload `{kind: "price", ticker, title, subtitle, series, dates, marks}`.
- Produces:
  - `ChartSpec` with `kind?: "score" | "price"`, `median?: number`, `threshold?: number`.
  - From `web/lib/chart-svg.ts`: `priceLabel(v: number): string`, `priceDomain(series: number[]): [number, number]`, `priceTicks(domain: [number, number]): number[]`.
  - `Scales` gains `m: { t: number; r: number; b: number; l: number }`.

- [ ] **Step 1: Capture score mode's output before any change**

```bash
cd web
CHECK=$(mktemp -d); echo "$CHECK"
cp lib/chart-svg.ts "$CHECK/before.mts"
cat > "$CHECK/render.mts" <<'EOF'
import { writeFileSync } from "node:fs";
const [module, out, kind] = process.argv.slice(2);
const { chartSvg } = await import(module);
const dates = Array.from({ length: 60 }, (_, i) =>
  new Date(Date.UTC(2026, 6, 1 + i)).toISOString().slice(0, 10));
const score = {
  ticker: "NVDA", title: "NVDA — blended score, 60d",
  subtitle: "Semiconductors · 38 peers · alert threshold 75 · illustrative data",
  series: Array.from({ length: 60 }, (_, i) => 50 + 20 * Math.sin(i / 7)),
  dates, median: 57, threshold: 75,
  marks: [
    { kind: "point", index: 40, end: null, label: "crossed 75 up", tone: "amber" },
    { kind: "span", index: 10, end: 20, label: "+12.0 over 10d", tone: "blue" },
  ],
};
const price = {
  kind: "price", ticker: "APH", title: "APH — adjusted close, 60 trading days",
  subtitle: "Amphenol · score 71.2 on 13 Sep · V 44.5 Q 79.0 M 91.0",
  series: Array.from({ length: 60 }, (_, i) => 1210 + 60 * Math.sin(i / 9) + i),
  dates,
  marks: [{ kind: "point", index: 59, end: null, label: "now 1269", tone: "copper" }],
};
writeFileSync(out, chartSvg(kind === "price" ? price : score));
EOF
node "$CHECK/render.mts" "$CHECK/before.mts" "$CHECK/before-score.svg" score && ls -l "$CHECK/before-score.svg"
cd ..
```

Expected: a non-empty `before-score.svg`. Keep `$CHECK` for Step 5. The `.mts` copy is what makes Node run the unchanged file as an ES module; its only import is a type-only one, which Node strips.

- [ ] **Step 2: Make the score lines optional and add `kind`**

In `web/lib/threads.ts`, replace the `ChartSpec` type with:

```ts
export type ChartSpec = {
  /* Absent on every chart saved before price mode existed, and those were all
     scores, so absence means "score" (ui-swap spec D16). */
  kind?: "score" | "price";
  ticker: string;
  title: string;
  subtitle: string;
  series: number[];
  dates: string[];
  /** Score mode only. */
  median?: number;
  /** Score mode only. */
  threshold?: number;
  marks: Mark[];
};
```

Replace the comment above `SKILLS` with:

```ts
/* Suggestions, not commands. Each is a question Steven can genuinely answer
   today: one draws a real chart, the rest are things he knows about himself or
   the design. */
```

- [ ] **Step 3: Add price mode to `chart-svg.ts`**

In `web/lib/chart-svg.ts`:

Replace `const m = { t: 22, r: 38, b: 30, l: 26 };` with:

```ts
type Margin = { t: number; r: number; b: number; l: number };
const SCORE_MARGIN: Margin = { t: 22, r: 38, b: 30, l: 26 };
/* Wider on both sides in price mode: a tick such as "272.50" or a last close
   such as "1269" is twice the width of a score's "75". */
const PRICE_MARGIN: Margin = { t: 22, r: 44, b: 30, l: 38 };
```

After `shortDate`, add:

```ts
/** D5: a close has two decimals below 1,000 and none at or above. */
export function priceLabel(v: number): string {
  return Math.abs(v) >= 1000 ? v.toFixed(0) : v.toFixed(2);
}

/* Price mode's y-domain: the data's own range with 5% padding on each side, so a
   stock moving between 270 and 280 fills the plot instead of lying flat inside a
   score's 20–85. A flat series gets a band of 2% of its level so it still has
   a height. */
export function priceDomain(series: number[]): [number, number] {
  const lo = Math.min(...series);
  const hi = Math.max(...series);
  const span = hi - lo || Math.abs(hi) * 0.02 || 1;
  return [lo - span * 0.05, hi + span * 0.05];
}

/* Three rounded ticks. The step is the largest of 1, 2 or 5 times a power of
   ten that fits three times into the domain, which is what guarantees all three
   land inside it. */
export function priceTicks([lo, hi]: [number, number]): number[] {
  const rough = (hi - lo) / 3;
  const power = 10 ** Math.floor(Math.log10(rough));
  const step = [5, 2, 1].map((k) => k * power).find((s) => s <= rough) ?? power;
  const first = Math.ceil(lo / step) * step;
  return [first, first + step, first + 2 * step];
}
```

Replace the `Scales` type and `scales` function with:

```ts
export type Scales = {
  x: (i: number) => number;
  y: (v: number) => number;
  plotX: number;
  plotY: number;
  m: Margin;
};

/** Plot-local coordinates. Shared with the hover overlay so it lands on the line. */
export function scales(spec: ChartSpec): Scales {
  const s = spec.series;
  const price = spec.kind === "price";
  const m = price ? PRICE_MARGIN : SCORE_MARGIN;
  const [lo, hi] = price
    ? priceDomain(s)
    : [Math.min(20, ...s) - 4, Math.max(85, ...s) + 4];
  return {
    x: (i) => m.l + (i / (s.length - 1)) * (W - m.l - m.r),
    y: (v) => m.t + (1 - (v - lo) / (hi - lo)) * (H - m.t - m.b),
    plotX: PLOT_X,
    plotY: PLOT_Y,
    m,
  };
}
```

In `annotation`, add `const m = sc.m;` as the first line of the function body. The existing `m.t`, `m.b` and `W` references stay as they are.

In `chartSvg`, after `const sc = scales(spec);`, add:

```ts
  const m = sc.m;
  const price = spec.kind === "price";
```

Replace the `grid` block with:

```ts
  const ticks = price ? priceTicks(priceDomain(s)) : [25, 50, 75];
  const grid = ticks
    .map(
      (v) =>
        `<line x1="${m.l}" x2="${W - m.r}" y1="${sc.y(v)}" y2="${sc.y(v)}" stroke="${PAPER}" stroke-width="1"/>` +
        `<text x="${m.l - 5}" y="${sc.y(v) + 3}" text-anchor="end" font-size="8" fill="${INK_MUTED}">${price ? priceLabel(v) : v}</text>`
    )
    .join("");

  // A threshold and a median mean something for a score and nothing for a price (D16).
  const threshold =
    !price && spec.threshold !== undefined
      ? `<line x1="${m.l}" x2="${W - m.r}" y1="${sc.y(spec.threshold)}" y2="${sc.y(spec.threshold)}" stroke="${AMBER_4}" stroke-width="1.2"/>`
      : "";
  const median =
    !price && spec.median !== undefined
      ? `<line x1="${m.l}" x2="${W - m.r}" y1="${sc.y(spec.median)}" y2="${sc.y(spec.median)}" stroke="${BLUE}" stroke-width="1.6" stroke-linecap="round" opacity="0.8"/>`
      : "";
  const medianLabel =
    !price && spec.median !== undefined
      ? `<text x="${W - m.r + 5}" y="${sc.y(spec.median) + 3}" font-size="9" fill="${BLUE}">${spec.median.toFixed(0)}</text>`
      : "";
```

In the returned string, replace the two literal `<line ...threshold...>` and `<line ...median...>` segments with `threshold +` and `median +`. Replace the last-value text's `${last.toFixed(0)}` with `${price ? priceLabel(last) : last.toFixed(0)}`. Replace the median `<text>` segment with `medianLabel +`. Leave every other segment exactly as it is, so score mode's bytes do not move.

- [ ] **Step 4: Give `ChatChart` a price readout**

In `web/components/ChatChart.tsx`, add `priceLabel` to the `@/lib/chart-svg` import. Replace the `readout` expression with:

```tsx
  const readout =
    hover === null
      ? null
      : spec.kind === "price"
        ? `close ${priceLabel(s[hover])} on ${shortDate(spec.dates[hover])}`
        : `${shortDate(spec.dates[hover])} · score ${s[hover].toFixed(1)}` +
          (spec.median !== undefined ? ` · median ${spec.median.toFixed(0)}` : "") +
          (spec.threshold !== undefined && s[hover] >= spec.threshold
            ? " · above the alert threshold"
            : "");
```

In the hover `<line>`, replace `y1={22} y2={138}` with `y1={sc.m.t} y2={H - sc.m.b}`, and add `H` to the `@/lib/chart-svg` import. `H` is already exported.

- [ ] **Step 5: Verify score mode is byte-identical and price mode renders**

```bash
cd web
cp lib/chart-svg.ts "$CHECK/after.mts"
node "$CHECK/render.mts" "$CHECK/after.mts" "$CHECK/after-score.svg" score
cmp "$CHECK/before-score.svg" "$CHECK/after-score.svg" && echo "score mode unchanged"
node "$CHECK/render.mts" "$CHECK/after.mts" "$CHECK/after-price.svg" price
grep -o '>[0-9][0-9.]*</text>' "$CHECK/after-price.svg" | head
npm run lint && npx tsc --noEmit
cd ..
```

Expected:
- `score mode unchanged`.
- The price SVG has three tick labels. They are evenly spaced multiples of one round step (1, 2 or 5 times a power of ten) with no decimals, because the series is above 1,000. Its last-close label is a whole number too.
- There is no amber threshold line: `grep -c f99c00 "$CHECK/after-price.svg"` prints `0`.
- Lint and `tsc` are clean.

Open `after-price.svg` in a browser to look at it.

If the `cmp` fails, diff the two files (`diff <(tr '>' '\n' < before-score.svg) <(tr '>' '\n' < after-score.svg)`) and restore the segment that moved.

- [ ] **Step 6: Commit**

```bash
git add web/lib/threads.ts web/lib/chart-svg.ts web/components/ChatChart.tsx
git commit -m "Draw a price in the one chart renderer, leaving score charts byte-identical"
```

---

### Task 7: A typed client for the screen

**Files:**
- Create: `web/lib/screen.ts`

**Interfaces:**
- Consumes: D9/D10 JSON (`src/screener/screen/shape.py`); `ChartSpec` (Task 6); `shortDate` from `@/lib/chart-svg`.
- Produces, all exported from `@/lib/screen`:

  | kind | names |
  |---|---|
  | types | `PillarKey`, `Close`, `Sector`, `Run`, `ScreenRow`, `Tiles`, `Awaiting`, `ScreenPage`, `Sort`, `ScreenQuery`, `Status`, `Unit`, `StoredMetric`, `Metric`, `Pillar`, `Scored`, `Unscored`, `SecurityDetail` |
  | constants | `PILLAR_KEYS`, `PILLAR_NAMES`, `PAGE_SIZE`, `SORTS` |
  | class | `ScreenError` |
  | fetchers | `fetchScreen(q: ScreenQuery): Promise<ScreenPage \| Awaiting>`, `fetchSecurity(symbol: string, run: number): Promise<SecurityDetail \| Awaiting>` |
  | formatters | `screenErrorText(exc: unknown): string`, `count`, `pageRange(offset, shown, total)`, `longDate(iso)`, `clock(iso)`, `metricValue(value, unit)`, `percentile(value): number`, `difference(stored, reproduced): string`, `period(stored): string` |
  | chart and context | `priceSpec(symbol, name, closes): ChartSpec`, `summarise(row: ScreenRow \| undefined, filters: string[]): string` |

- [ ] **Step 1: Write the module**

Create `web/lib/screen.ts`:

```ts
import { shortDate } from "@/lib/chart-svg";
import type { ChartSpec } from "@/lib/threads";

/* The scored screen, as the status service serves it (ui-swap spec D9, D10).
 *
 * Every figure arrives as a decimal string. The service has already rounded
 * scores and percentiles half up, and sends raw values exactly, so that a stored
 * value and its reproduction are never two floats that print the same (D5).
 * This module turns those strings into what a person reads, and the page never
 * computes a score of its own. */

export type PillarKey = "V" | "Q" | "M";
export const PILLAR_KEYS: readonly PillarKey[] = ["V", "Q", "M"];
export const PILLAR_NAMES: Record<PillarKey, string> = {
  V: "Valuation", Q: "Quality", M: "Momentum",
};

export type Close = [date: string, close: string];
export type Sector = { code: string; name: string };

export type Run = {
  id: number;
  as_of: string;
  started_at: string;
  finished_at: string | null;
  git_sha: string;
  config_hash: string;
  weight_version: string;
  cutoff_offset_seconds: number;
  logic: string;
  emits_alerts: boolean;
};

export type ScreenRow = {
  symbol: string;
  name: string;
  sector: Sector;
  score: string;
  /** Null when there is no previous night under the same weights, or this security was not scored on it. */
  delta: string | null;
  pillars: Record<PillarKey, { score: string; coverage: string | null } | null>;
  agreement: number;
  min_coverage: string;
  partial: boolean;
  closes: Close[];
};

export type Tiles = {
  scored: number;
  active_now: number;
  partial: number;
  agreement_3: number;
  market_ranked_values: number;
};

export type Awaiting = { state: "awaiting_first_night" };

export type ScreenPage = {
  state: "ready";
  latest: boolean;
  run: Run;
  previous_as_of: string | null;
  tiles: Tiles;
  sectors: Sector[];
  total: number;
  rows: ScreenRow[];
};

export type Sort = "score" | "delta" | "V" | "Q" | "M";
export const SORTS: { value: Sort; label: string }[] = [
  { value: "score", label: "Score" },
  { value: "delta", label: "Δ 1d" },
  { value: "V", label: "Valuation" },
  { value: "Q", label: "Quality" },
  { value: "M", label: "Momentum" },
];

export const PAGE_SIZE = 50;

export type ScreenQuery = {
  run?: number;
  sector: string;
  agree: string;
  partial: string;
  sort: Sort;
  offset: number;
};

export type Status = "ok" | "mismatch" | "absent" | "unexpected" | "refreshed" | "unchecked";
export type Unit = "percent" | "multiple";

export type StoredMetric = {
  raw: string;
  percentile: string;
  peer_group: string;
  peer_count: number;
  market_ranked: boolean;
  period_basis: string | null;
  period_end: string | null;
};

export type Metric = {
  code: string;
  name: string;
  unit: Unit;
  higher_is_better: boolean;
  status: Status;
  stored: StoredMetric | null;
  reproduced: string | null;
  reason: string | null;
};

export type Pillar = {
  code: string;
  key: PillarKey;
  score: string | null;
  present: number;
  expected: number;
  metrics: Metric[];
};

export type Scored = {
  scored: true;
  run_id: number;
  latest: boolean;
  symbol: string;
  name: string;
  sector: Sector;
  industry: Sector | null;
  active: boolean;
  score: string;
  agreement: number;
  partial: boolean;
  pillars: Pillar[];
  reproduction: {
    visible_through: string;
    run_build: string;
    running_build: string;
    refreshed_inputs: string[];
  };
  closes: Close[];
};

export type Unscored = {
  scored: false;
  run_id: number;
  latest: boolean;
  symbol: string;
  name: string;
  sector: Sector;
  active: boolean;
  closes: Close[];
};

export type SecurityDetail = Scored | Unscored;

/** A refused request, with the service's error code when it sent one (spec §7). */
export class ScreenError extends Error {
  readonly status: number;
  readonly code: string | null;
  constructor(status: number, code: string | null) {
    super(`screen request failed: ${status}${code ? ` ${code}` : ""}`);
    this.status = status;
    this.code = code;
  }
}

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

async function get<T>(path: string, query: URLSearchParams): Promise<T> {
  const response = await fetch(`${BASE}${path}?${query}`, {
    // Same origin behind Caddy, but explicit: the session cookie is the whole
    // authorisation and a default that omitted it would 401 confusingly.
    credentials: "include",
    cache: "no-store",
  });
  if (!response.ok) {
    const body: { error?: unknown } | null = await response.json().catch(() => null);
    throw new ScreenError(response.status, typeof body?.error === "string" ? body.error : null);
  }
  return (await response.json()) as T;
}

export function fetchScreen(q: ScreenQuery): Promise<ScreenPage | Awaiting> {
  const query = new URLSearchParams();
  if (q.run !== undefined) query.set("run", String(q.run));
  if (q.sector) query.set("sector", q.sector);
  if (q.agree) query.set("agree", q.agree);
  if (q.partial) query.set("partial", q.partial);
  query.set("sort", q.sort);
  query.set("offset", String(q.offset));
  query.set("limit", String(PAGE_SIZE));
  return get("/api/screen", query);
}

export function fetchSecurity(symbol: string, run: number): Promise<SecurityDetail | Awaiting> {
  return get("/api/screen/security", new URLSearchParams({ symbol, run: String(run) }));
}

export function screenErrorText(exc: unknown): string {
  if (exc instanceof ScreenError) {
    if (exc.status === 401) return "Your session has ended. Reload the page to sign in again.";
    if (exc.code === "run_changed") return "The night you were browsing is no longer served.";
    if (exc.status === 503) return "The screen cannot be read: the database is unreachable.";
    return `The screen could not be read (${exc.message}).`;
  }
  return "The screen could not be read. Check the connection and try again.";
}

export function count(n: number): string {
  return n.toLocaleString("en-GB");
}

/** "1–50 of 1,499". */
export function pageRange(offset: number, shown: number, total: number): string {
  if (shown === 0) return `0 of ${count(total)}`;
  return `${count(offset + 1)}–${count(offset + shown)} of ${count(total)}`;
}

/** An `as_of` date in full. UTC, because a scoring night is a calendar date, not a moment. */
export function longDate(iso: string): string {
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-GB", {
    weekday: "long", day: "numeric", month: "long", year: "numeric", timeZone: "UTC",
  });
}

export function clock(iso: string): string {
  const time = new Date(iso).toLocaleTimeString("en-GB", {
    hour: "2-digit", minute: "2-digit", timeZone: "UTC",
  });
  return `${time} UTC`;
}

/** D5: a fraction as a percent to one decimal, or a multiple to two decimals with ×. */
export function metricValue(value: string, unit: Unit): string {
  const n = Number(value);
  return unit === "percent" ? `${(n * 100).toFixed(1)}%` : `${n.toFixed(2)}×`;
}

/** D5: percentiles are whole numbers on screen. */
export function percentile(value: string): number {
  return Math.round(Number(value));
}

function decimals(value: string): number {
  const dot = value.indexOf(".");
  return dot < 0 ? 0 : value.length - dot - 1;
}

/* Reproduced minus stored, computed on the decimal strings rather than as floats:
   a difference in the seventh digit is the whole point of showing one, and float
   subtraction would bury it in noise such as 0.00020000000000000573 (D5). */
export function difference(stored: string, reproduced: string): string {
  const places = Math.max(decimals(stored), decimals(reproduced));
  const scaled = (value: string) => {
    const negative = value.startsWith("-");
    const digits = value.replace("-", "").replace(".", "") + "0".repeat(places - decimals(value));
    return BigInt(digits) * BigInt(negative ? -1 : 1);
  };
  const d = scaled(reproduced) - scaled(stored);
  const zero = BigInt(0);
  const sign = d < zero ? "-" : d > zero ? "+" : "";
  const digits = (d < zero ? -d : d).toString().padStart(places + 1, "0");
  return places ? `${sign}${digits.slice(0, -places)}.${digits.slice(-places)}` : `${sign}${digits}`;
}

/** "TTM · Dec 2025", "annual · Sep 2025", or "price" for a momentum metric, which has no period. */
export function period(stored: StoredMetric): string {
  if (!stored.period_basis && !stored.period_end) return "price";
  const end = stored.period_end
    ? new Date(`${stored.period_end}T00:00:00Z`).toLocaleDateString("en-GB", {
        month: "short", year: "numeric", timeZone: "UTC",
      })
    : null;
  return [stored.period_basis, end].filter(Boolean).join(" · ");
}

/** The Overview's price chart, drawn by the one renderer in price mode (D15, D16). */
export function priceSpec(symbol: string, name: string, closes: Close[]): ChartSpec {
  return {
    kind: "price",
    ticker: symbol,
    title: `${symbol} — adjusted close, ${closes.length} trading days`,
    subtitle: `${name} · stored end-of-day bars, adjusted for splits and dividends · ${shortDate(closes[0][0])} to ${shortDate(closes[closes.length - 1][0])}`,
    series: closes.map(([, close]) => Number(close)),
    dates: closes.map(([day]) => day),
    marks: [],
  };
}

/* What the palette sends with a question about the Overview (D18). Most
   important first, because `describe()` cuts at 400 characters and should lose
   only the tail. */
export function summarise(row: ScreenRow | undefined, filters: string[]): string {
  if (!row) return "the scored screen, with no security selected";
  const delta =
    row.delta === null
      ? "new since the previous night"
      : `Δ ${Number(row.delta) > 0 ? "+" : ""}${row.delta} on the previous night`;
  return [
    `${row.symbol} (${row.name})`,
    `blended score ${row.score}${row.partial ? ", partial" : ""}`,
    delta,
    PILLAR_KEYS.map((key) => `${key} ${row.pillars[key]?.score ?? "—"}`).join(" "),
    `${row.agreement} of 3 pillars top-quartile`,
    row.sector.name,
    filters.length ? `filters: ${filters.join(", ")}` : null,
  ]
    .filter((part): part is string => part !== null)
    .join(", ");
}
```

- [ ] **Step 2: Check `difference` against the cases that matter**

```bash
cd web
CHECK=$(mktemp -d)
sed -e '/^import /d' lib/screen.ts > "$CHECK/screen.mts"
cat > "$CHECK/diff.mts" <<EOF
const { difference } = await import("$CHECK/screen.mts");
const cases = [["0.0741", "0.0739", "-0.0002"], ["0.5", "0.5000001", "+0.0000001"],
  ["-1.2", "0.3", "+1.5"], ["10", "10", "0"], ["3", "2.75", "-0.25"]];
for (const [a, b, want] of cases) {
  const got = difference(a, b);
  if (got !== want) { console.error("difference", a, b, "->", got, "want", want); process.exit(1); }
}
console.log("difference ok");
EOF
node "$CHECK/diff.mts"
npm run lint && npx tsc --noEmit
cd ..
```

Expected: `difference ok`, then lint and `tsc` clean. The `sed` drops the two imports, since `difference` needs neither. `shortDate` is unused by this check.

- [ ] **Step 3: Commit**

```bash
git add web/lib/screen.ts
git commit -m "Type the screen's two endpoints and format their decimal strings for display"
```

---

### Task 8: The "Why this score" panel

**Files:**
- Create: `web/components/WhyPanel.tsx`
- Modify: `web/app/globals.css` (append a `why this score` section)

**Interfaces:**
- Consumes: from `@/lib/screen`: `Metric`, `SecurityDetail`, `PILLAR_NAMES`, `difference`, `metricValue`, `percentile`, `period`.
- Produces: `default function WhyPanel({ detail, error, asOf }: { detail: SecurityDetail | null; error: string | null; asOf: string | null })`. `detail === null` with no error means loading. `asOf` is the night's short date, used for "not scored on".

- [ ] **Step 1: Write the component**

Create `web/components/WhyPanel.tsx`:

```tsx
"use client";

import type { Metric, SecurityDetail } from "@/lib/screen";
import { PILLAR_NAMES, difference, metricValue, percentile, period } from "@/lib/screen";

/* "Why this score": every metric behind each pillar, with its percentile and its
 * peers, beside whether it reproduces now (ui-swap spec D4, D14).
 *
 * The status service re-runs scoring's explaining forms for this one security
 * under the run's own view, so each row is a claim that has just been checked,
 * not a stored number repeated. What a status cannot check is the percentile,
 * which depends on every peer; the footer says so, so "ok" does not claim more
 * than it means. */

const CAUSES = [
  "a currency change or sector reclassification since the run",
  "the split rule's calendar-boundary gap",
  "an ingest that overlapped the run's start",
];

function note(metric: Metric): { text: string; warn: boolean } | null {
  const { stored, reproduced } = metric;
  switch (metric.status) {
    case "mismatch":
      return {
        warn: true,
        text:
          stored && reproduced !== null
            ? `does not reproduce: stored ${stored.raw}, now ${reproduced} (${difference(stored.raw, reproduced)})`
            : `does not reproduce: now absent, ${metric.reason ?? "no reason recorded"}`,
      };
    case "absent":
      return metric.reason ? { warn: false, text: metric.reason } : null;
    case "unexpected":
      return {
        warn: true,
        text:
          reproduced === null
            ? "not scored, but reproduces now"
            : `not scored, but reproduces now: ${metricValue(reproduced, metric.unit)}`,
      };
    case "refreshed":
      return { warn: false, text: "inputs changed since this run" };
    case "unchecked":
      return { warn: false, text: "could not re-check" };
    default:
      return null;
  }
}

function MetricRow({ metric }: { metric: Metric }) {
  const { stored } = metric;
  const said = note(metric);
  return (
    <>
      <span className="why-name">
        {metric.name}
        {metric.higher_is_better ? null : <span title="lower is better"> ↓</span>}
      </span>
      <span className="why-value">{stored ? metricValue(stored.raw, metric.unit) : "—"}</span>
      <span className="why-bar" title={stored ? `${percentile(stored.percentile)}th percentile` : undefined}>
        {stored ? <i style={{ width: `${percentile(stored.percentile)}%` }} /> : null}
      </span>
      <span className="why-meta">
        {stored
          ? `${percentile(stored.percentile)} · ${stored.market_ranked ? "vs market" : stored.peer_group} (${stored.peer_count}) · ${period(stored)}`
          : null}
      </span>
      {said ? <span className={`why-note${said.warn ? " warn" : ""}`}>{said.text}</span> : null}
    </>
  );
}

export default function WhyPanel({
  detail, error, asOf,
}: {
  detail: SecurityDetail | null;
  error: string | null;
  asOf: string | null;
}) {
  if (error) {
    return (
      <section className="card">
        <h2>Why this score</h2>
        <div className="sub">{error}</div>
      </section>
    );
  }
  if (!detail) {
    return (
      <section className="card">
        <h2>Why this score</h2>
        <div className="sub">Re-checking the inputs…</div>
      </section>
    );
  }
  if (!detail.scored) {
    return (
      <section className="card">
        <h2>Why this score</h2>
        <div className="sub">
          {detail.symbol} was not scored on {asOf ?? "this night"}
          {detail.active ? "." : ", and is no longer in the universe."}
        </div>
      </section>
    );
  }

  const { reproduction } = detail;
  const flagged = detail.pillars.some((pillar) =>
    pillar.metrics.some((metric) => metric.status === "mismatch" || metric.status === "unexpected")
  );
  const rebuilt = reproduction.run_build !== reproduction.running_build;
  const causes = rebuilt
    ? [`a build difference (run ${reproduction.run_build}, now ${reproduction.running_build})`, ...CAUSES]
    : CAUSES;

  return (
    <section className="card">
      <h2>Why this score</h2>
      <div className="sub">
        {detail.symbol} · {detail.score}
        {detail.partial ? " ◐ partial" : ""} · {detail.agreement} of 3 pillars top-quartile ·{" "}
        {detail.industry?.name ?? detail.sector.name}
      </div>
      {detail.pillars.map((pillar) => (
        <div key={pillar.code}>
          <div className="why-head">
            {PILLAR_NAMES[pillar.key]} {pillar.score ?? "—"}
            <small>
              {pillar.present} of {pillar.expected} metrics
            </small>
          </div>
          <div className="why-grid">
            {pillar.metrics.map((metric) => (
              <MetricRow key={metric.code} metric={metric} />
            ))}
          </div>
        </div>
      ))}
      <div className="why-foot">
        <p>
          Each value is re-computed from the inputs the run could see. Percentiles are not
          re-derived, because they depend on every peer, so “ok” means the raw value reproduces.
        </p>
        {reproduction.refreshed_inputs.length > 0 ? (
          <p>Price bars were rewritten since this run, so the metrics that read a close are not compared.</p>
        ) : null}
        {flagged ? <p>Known causes of a value that does not reproduce: {causes.join("; ")}.</p> : null}
      </div>
    </section>
  );
}
```

- [ ] **Step 2: Style it**

Append to `web/app/globals.css`, before the `/* ---- audit` section comment:

```css
/* ---- why this score -------------------------------------------------------- */
.why-head {
  display: flex; align-items: baseline; gap: 8px; padding: calc(var(--sp) * 3) calc(var(--sp) * 5) 0;
  font-size: 0.8rem; font-weight: 600; border-top: 1px solid color-mix(in srgb, var(--line) 55%, transparent);
}
.why-head small { font-weight: 400; color: var(--ink-muted); font-size: 0.72rem; }
.why-grid {
  display: grid; grid-template-columns: minmax(0, 1fr) auto 56px; column-gap: 10px; row-gap: 0;
  padding: 4px calc(var(--sp) * 5) calc(var(--sp) * 3); font-size: 0.76rem; align-items: center;
}
.why-name { padding-top: 5px; }
.why-value { font-family: var(--font-geist-mono), monospace; text-align: right; white-space: nowrap; padding-top: 5px; }
.why-bar { height: 6px; margin-top: 5px; background: var(--paper); border-radius: 4px; position: relative; overflow: hidden; }
.why-bar i { position: absolute; inset: 0 auto 0 0; background: var(--copper); border-radius: 4px; }
.why-meta { grid-column: 1 / -1; font-size: 0.68rem; color: var(--ink-muted); }
.why-note { grid-column: 1 / -1; font-size: 0.7rem; color: var(--ink-soft); }
.why-note.warn { color: var(--copper-deep); font-weight: 500; }
.why-foot {
  font-size: 0.7rem; color: var(--ink-muted); padding: calc(var(--sp) * 3) calc(var(--sp) * 5) calc(var(--sp) * 4);
  border-top: 1px solid color-mix(in srgb, var(--line) 55%, transparent);
}
.why-foot p + p { margin-top: 4px; }
```

- [ ] **Step 3: Verify**

Run: `cd web && npm run lint && npx tsc --noEmit; cd ..`
Expected: clean. The panel is not yet on a page, so it renders for the first time in Task 9.

- [ ] **Step 4: Commit**

```bash
git add web/components/WhyPanel.tsx web/app/globals.css
git commit -m "Show why a score is what it is, metric by metric, with each value re-checked"
```

---

### Task 9: The Overview reads the real screen, and the concept goes

**Files:**
- Rewrite: `web/app/page.tsx`
- Rewrite: `web/components/StatTiles.tsx`, `web/components/UniverseTable.tsx`
- Modify: `web/components/Sparkline.tsx`
- Modify: `web/lib/screen-context.tsx`
- Modify: `web/app/steven/page.tsx` (the notice), `web/app/audit/page.tsx:96` (comment), `web/lib/skybird.ts:3` (comment)
- Modify: `web/app/globals.css`
- Delete: `web/lib/data.ts`, `web/components/AlertFeed.tsx`, `web/components/ScoreChart.tsx`

**Interfaces:**
- Consumes: everything from Task 7; `WhyPanel` (Task 8); `ChatChart` in price mode (Task 6).
- Produces: `usePublishScreen(page: string, summary: string)`. The third parameter is gone, and `ScreenContext` has no `illustrative` field.

- [ ] **Step 1: Delete `illustrative` from the palette context**

In `web/lib/screen-context.tsx`, delete the paragraph of the header comment that begins `` `illustrative` travels with it``. Delete the `illustrative` field and its doc comment from `ScreenContext`. Replace `usePublishScreen` and `describe` with:

```tsx
/** Publish this page's state. Pass a stable string; it re-sends on change. */
export function usePublishScreen(page: string, summary: string) {
  const { setContext } = useScreenContext();
  useEffect(() => {
    setContext({ page, summary });
    // Cleared on unmount so a stale view never travels with a later question.
    return () => setContext(null);
  }, [page, summary, setContext]);
}

/** The single line sent to the model. Bounded, because it is paid for. */
export function describe(context: ScreenContext | null): string {
  if (!context) return "";
  return `${context.page}: ${context.summary}.`.slice(0, 400);
}
```

- [ ] **Step 2: Rewrite the tiles**

Replace `web/components/StatTiles.tsx` with:

```tsx
"use client";

import { animate, motion } from "framer-motion";
import { useEffect, useRef } from "react";
import { type Tiles, count } from "@/lib/screen";

function CountUp({ to, delay = 0 }: { to: number; delay?: number }) {
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    const controls = animate(0, to, {
      delay,
      duration: 0.9,
      ease: [0, 0, 0.2, 1],
      onUpdate: (v) => {
        if (ref.current) ref.current.textContent = count(Math.round(v));
      },
    });
    return () => controls.stop();
  }, [to, delay]);
  return <span ref={ref}>0</span>;
}

/* The night, not the page: these do not change with the filters (D9). */
export default function StatTiles({ tiles }: { tiles: Tiles }) {
  const items = [
    {
      k: "Scored", v: tiles.scored, suffix: ` / ${count(tiles.active_now)} active now`,
      s: "a snapshot on this night, against the universe as it stands today",
    },
    {
      k: "Partial scores", v: tiles.partial,
      s: "a weighted pillar below full coverage; marked ◐, not hidden",
    },
    {
      k: "All three pillars top-quartile", v: tiles.agreement_3,
      s: "valuation, quality and momentum in their top quarter at once",
    },
    {
      k: "Metric values ranked against the market", v: tiles.market_ranked_values,
      s: "a sector too thin to rank within, so the whole market stood in",
    },
  ];
  return (
    <div className="tiles">
      {items.map((t, i) => (
        <motion.div
          key={t.k}
          className="tile"
          initial={{ opacity: 0, y: 14 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.1 + i * 0.07, duration: 0.5, ease: [0, 0, 0.2, 1] }}
        >
          <div className="k">{t.k}</div>
          <div className="v">
            <CountUp to={t.v} delay={0.15 + i * 0.07} />
            {t.suffix ? <small>{t.suffix}</small> : null}
          </div>
          <div className="s">{t.s}</div>
        </motion.div>
      ))}
    </div>
  );
}
```

- [ ] **Step 3: Guard the sparkline and rewrite the table**

In `web/components/Sparkline.tsx`, add as the first line of the component body:

```tsx
  // A security with fewer than two stored bars has no line to draw.
  if (h.length < 2) return <svg width={84} height={26} aria-hidden="true" />;
```

Replace `web/components/UniverseTable.tsx` with:

```tsx
"use client";

import { motion } from "framer-motion";
import Sparkline from "@/components/Sparkline";
import { PAGE_SIZE, PILLAR_KEYS, PILLAR_NAMES, type ScreenRow, pageRange } from "@/lib/screen";

function DeltaCell({ delta }: { delta: string | null }) {
  // Null when there is no previous night under the same weights, or this
  // security was not scored on it (D9).
  if (delta === null) return <td className="num delta flat">new</td>;
  const d = Number(delta);
  const cls = d > 0 ? "up" : d < 0 ? "down" : "flat";
  const sym = d > 0 ? "▲" : d < 0 ? "▼" : "·";
  return <td className={`num delta ${cls}`}>{sym} {d > 0 ? "+" : ""}{delta}</td>;
}

export default function UniverseTable({
  rows, total, offset, selected, onSelect, onPage,
}: {
  /** Null while a page is being read: never the previous page's rows (D15). */
  rows: ScreenRow[] | null;
  total: number;
  offset: number;
  selected: string | null;
  onSelect: (symbol: string) => void;
  onPage: (offset: number) => void;
}) {
  return (
    <section className="card">
      <h2>Screen by blended score</h2>
      <div className="sub">
        Percentiles within sector, averaged within pillar. The blend is derivable — pillar
        scores and their raw inputs are the record.
      </div>
      <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Security</th><th>Sector</th><th className="num">Score</th><th className="num">Δ 1d</th>
            <th>Pillars · V Q M</th><th>Agree</th><th>30d</th>
          </tr>
        </thead>
        <tbody>
          {rows === null ? (
            <tr className="empty-row"><td colSpan={7}>Reading the screen…</td></tr>
          ) : rows.length === 0 ? (
            <tr className="empty-row"><td colSpan={7}>No securities match these filters.</td></tr>
          ) : (
            rows.map((r, ri) => (
              <motion.tr
                key={r.symbol}
                className={r.symbol === selected ? "sel" : undefined}
                onClick={() => onSelect(r.symbol)}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.12 + Math.min(ri, 20) * 0.035, duration: 0.4, ease: [0, 0, 0.2, 1] }}
              >
                <td><span className="tick">{r.symbol}<small>{r.name}</small></span></td>
                <td className="sector">{r.sector.name}</td>
                <td className="num score">
                  {r.score}
                  {r.partial ? (
                    <span className="partial-mark" title="Partial: a weighted pillar is below full coverage">◐</span>
                  ) : null}
                </td>
                <DeltaCell delta={r.delta} />
                <td>
                  <div className="pillars">
                    {PILLAR_KEYS.map((key) => {
                      const cell = r.pillars[key];
                      const v = cell ? Number(cell.score) : null;
                      return (
                        <div
                          key={key}
                          className={`pl${v !== null && v >= 75 ? " top" : ""}`}
                          title={cell ? `${PILLAR_NAMES[key]} ${cell.score}` : `${PILLAR_NAMES[key]}: not scored`}
                        >
                          <b>{key}</b>
                          {v === null ? (
                            <span className="pl-dash">—</span>
                          ) : (
                            <div className="bar">
                              <motion.div
                                className="fill"
                                initial={{ width: 0 }}
                                animate={{ width: `${v}%` }}
                                transition={{ delay: 0.25 + Math.min(ri, 20) * 0.035, duration: 0.6, ease: [0, 0, 0.2, 1] }}
                              />
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </td>
                <td>
                  <span className="agree" title={`${r.agreement} of 3 pillars top-quartile`}>
                    {"●".repeat(r.agreement)}
                    <span>{"●".repeat(3 - r.agreement)}</span>
                  </span>
                </td>
                <td><Sparkline h={r.closes.map(([, close]) => Number(close))} /></td>
              </motion.tr>
            ))
          )}
        </tbody>
      </table>
      </div>
      {rows !== null && total > PAGE_SIZE ? (
        <div className="pager">
          <button onClick={() => onPage(Math.max(0, offset - PAGE_SIZE))} disabled={offset === 0}>
            Previous
          </button>
          <span>{pageRange(offset, rows.length, total)}</span>
          <button onClick={() => onPage(offset + PAGE_SIZE)} disabled={offset + PAGE_SIZE >= total}>
            Next
          </button>
        </div>
      ) : null}
    </section>
  );
}
```

The animation delay is capped at row 20, so the fiftieth row does not arrive two seconds late.

- [ ] **Step 4: Rewrite the page**

Replace `web/app/page.tsx` with:

```tsx
"use client";

import { motion } from "framer-motion";
import { useCallback, useEffect, useRef, useState } from "react";
import ChatChart from "@/components/ChatChart";
import Sidebar from "@/components/Sidebar";
import StatTiles from "@/components/StatTiles";
import UniverseTable from "@/components/UniverseTable";
import WhyPanel from "@/components/WhyPanel";
import { shortDate } from "@/lib/chart-svg";
import {
  type Awaiting, type ScreenPage, type SecurityDetail, type Sort,
  SORTS, clock, fetchScreen, fetchSecurity, longDate, pageRange, priceSpec,
  screenErrorText, summarise,
} from "@/lib/screen";
import { usePublishScreen } from "@/lib/screen-context";

/* A reply, stamped with the request it answers. What is drawn is checked against
   the request wanted now, so a slow reply to an old filter is never drawn as the
   answer to a new one, and "loading" is simply having no reply for this key. */
type Answer<T> = { key: string; value: T | null; error: string | null };

export default function Page() {
  const [sector, setSector] = useState("");
  const [agree, setAgree] = useState("");
  const [partial, setPartial] = useState("");
  const [sort, setSort] = useState<Sort>("score");
  const [offset, setOffset] = useState(0);
  const [chosen, setChosen] = useState<string | null>(null);
  const [reloads, setReloads] = useState(0);
  /* The night being browsed (D8). A ref, not state: it is pinned in reply to the
     first page, and pinning must not request that same page again. */
  const pinned = useRef<number | undefined>(undefined);
  const chartRef = useRef<HTMLDivElement>(null);

  /* The last night served. Kept across requests so the header, filters and tiles
     do not blink out while the next page is read; they describe the night, which
     a filter does not change. Rows are never kept this way. */
  const [night, setNight] = useState<ScreenPage | null>(null);

  const screenKey = `${sector}|${agree}|${partial}|${sort}|${offset}|${reloads}`;
  const [screen, setScreen] = useState<Answer<ScreenPage | Awaiting> | null>(null);
  useEffect(() => {
    let live = true;
    fetchScreen({ run: pinned.current, sector, agree, partial, sort, offset }).then(
      (value) => {
        if (!live) return;
        if (value.state === "ready") {
          pinned.current ??= value.run.id;
          setNight(value);
        }
        setScreen({ key: screenKey, value, error: null });
      },
      (exc: unknown) => {
        if (live) setScreen({ key: screenKey, value: null, error: screenErrorText(exc) });
      },
    );
    return () => {
      live = false;
    };
  }, [sector, agree, partial, sort, offset, screenKey]);

  const answer = screen?.key === screenKey ? screen : null;
  const page = answer?.value?.state === "ready" ? answer.value : null;
  const awaiting = answer?.value?.state === "awaiting_first_night";
  const selected = chosen ?? page?.rows[0]?.symbol ?? null;
  const runId = night?.run.id;

  const detailKey = `${selected}|${runId}`;
  const [detail, setDetail] = useState<Answer<SecurityDetail | Awaiting> | null>(null);
  useEffect(() => {
    if (selected === null || runId === undefined) return;
    let live = true;
    fetchSecurity(selected, runId).then(
      (value) => {
        if (live) setDetail({ key: detailKey, value, error: null });
      },
      (exc: unknown) => {
        if (live) setDetail({ key: detailKey, value: null, error: screenErrorText(exc) });
      },
    );
    return () => {
      live = false;
    };
  }, [selected, runId, detailKey]);
  const shown = detail?.key === detailKey ? detail : null;
  const security = shown?.value && "scored" in shown.value ? shown.value : null;

  // On one-column layouts the chart sits above the table, so a tapped row would
  // otherwise change something offscreen. Bring it into view instead.
  const select = useCallback((symbol: string) => {
    setChosen(symbol);
    if (typeof window !== "undefined" && window.innerWidth <= 1100) {
      chartRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }, []);

  // A filter or a sort starts again at page one: page four of a narrower screen
  // is an empty table that reads as a broken filter.
  const refilter = <T,>(set: (value: T) => void) => (value: T) => {
    set(value);
    setOffset(0);
  };
  const refresh = () => {
    pinned.current = undefined;
    setNight(null);
    setChosen(null);
    setOffset(0);
    setReloads((n) => n + 1);
  };

  const sectorName = night?.sectors.find((s) => s.code === sector)?.name;
  const filters = [
    sectorName ? `sector ${sectorName}` : null,
    agree ? `pillar agreement at least ${agree}` : null,
    partial === "only" ? "partial scores only" : partial === "hide" ? "partial scores hidden" : null,
    sort !== "score" ? `sorted by ${SORTS.find((s) => s.value === sort)?.label}` : null,
  ].filter((f): f is string => f !== null);
  usePublishScreen(
    "Overview",
    awaiting
      ? "no night has been scored under v2 yet"
      : summarise(page?.rows.find((r) => r.symbol === selected), filters),
  );

  const run = night?.run;
  return (
    <div className="shell">
      <Sidebar active="Overview" />
      <div className="content">
      <div className="wrap">
        <motion.header
          className="hero"
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.55, ease: [0, 0, 0.2, 1] }}
        >
          <h1>{run ? `Screen for ${longDate(run.as_of)}` : "Screen"}</h1>
          {run ? (
            <p>
              run <b>#{run.id}</b>
              {run.finished_at ? <> finished {clock(run.finished_at)}</> : null} · weights{" "}
              <b>{run.weight_version}</b> · alerts <b>off</b>
            </p>
          ) : null}
        </motion.header>

        {page && !page.latest ? (
          <div className="notice-bar">
            <b>A newer night is available.</b>
            <span>This view stays on {longDate(page.run.as_of)} so its pages never mix two nights.</span>
            <button className="chip-toggle" onClick={refresh}>Refresh</button>
          </div>
        ) : null}

        {awaiting ? (
          <section className="card awaiting">
            <h2>No night scored yet</h2>
            <div className="sub">
              The first v2 night has not been scored yet. The screen appears here once it has.
            </div>
          </section>
        ) : (
          <>
            {night ? (
              <div className="filters">
                <label>Sector</label>
                <select value={sector} onChange={(e) => refilter(setSector)(e.target.value)}>
                  <option value="">All sectors</option>
                  {night.sectors.map((s) => (
                    <option key={s.code} value={s.code}>{s.name}</option>
                  ))}
                </select>
                <label>Pillar agreement ≥</label>
                <select value={agree} onChange={(e) => refilter(setAgree)(e.target.value)}>
                  <option value="">any</option>
                  <option value="1">1</option>
                  <option value="2">2</option>
                  <option value="3">3</option>
                </select>
                <label>Partial</label>
                <select value={partial} onChange={(e) => refilter(setPartial)(e.target.value)}>
                  <option value="">all</option>
                  <option value="only">only</option>
                  <option value="hide">hide</option>
                </select>
                <label>Sort</label>
                <select value={sort} onChange={(e) => refilter(setSort)(e.target.value as Sort)}>
                  {SORTS.map((s) => (
                    <option key={s.value} value={s.value}>{s.label}</option>
                  ))}
                </select>
                <span className="range">
                  {page ? pageRange(offset, page.rows.length, page.total) : "reading…"}
                </span>
              </div>
            ) : null}

            {night ? <StatTiles tiles={night.tiles} /> : null}

            <div className="grid">
              {answer?.error ? (
                <section className="card">
                  <h2>Screen unavailable</h2>
                  <div className="sub">{answer.error}</div>
                  <div className="card-actions">
                    <button className="chip-toggle" onClick={refresh}>Try again</button>
                  </div>
                </section>
              ) : (
                <UniverseTable
                  rows={page ? page.rows : null}
                  total={page?.total ?? 0}
                  offset={offset}
                  selected={selected}
                  onSelect={select}
                  onPage={setOffset}
                />
              )}
              <div className="col" ref={chartRef}>
                {security && security.closes.length >= 2 ? (
                  <ChatChart spec={priceSpec(security.symbol, security.name, security.closes)} />
                ) : null}
                {selected !== null ? (
                  <WhyPanel
                    detail={security}
                    error={shown?.error ?? null}
                    asOf={run ? shortDate(run.as_of) : null}
                  />
                ) : null}
              </div>
            </div>
          </>
        )}
      </div>

      <footer className="site">
        <div className="wrap">
          Every score traces to visible raw inputs
          {run ? (
            <>
              {" "}· provenance
              <span className="prov">git {run.git_sha.slice(0, 7)}</span>
              <span className="prov">config {run.config_hash.slice(0, 8)}</span>
              <span className="prov">weights {run.weight_version}</span>
            </>
          ) : null}
        </div>
      </footer>
      </div>
    </div>
  );
}
```

- [ ] **Step 5: Delete the concept files and the stale wording**

```bash
git rm web/lib/data.ts web/components/AlertFeed.tsx web/components/ScoreChart.tsx
grep -rn "lib/data\|AlertFeed\|ScoreChart\|illustrative" web/app web/components web/lib
```

Expected after the edits below: nothing printed.
- In `web/app/steven/page.tsx`, replace the notice paragraph's text with: `Ask about the screener, the deployment, or a ticker&rsquo;s price history. He draws the chart from stored, adjusted closes and marks what you asked about.`
- In `web/app/audit/page.tsx`, delete the comment line `// Real figures, unlike the Overview table, so not flagged illustrative.`
- In `web/lib/skybird.ts`, change the header comment's `Everything here is real, unlike the concept data behind the Overview: these` to `Everything here is real, as the Overview's scores now are: these`, keeping the rest of that sentence.

- [ ] **Step 6: Update the stylesheet**

In `web/app/globals.css`:
1. Rename the three `.concept-bar` rules to `.notice-bar`, and add `.notice-bar button { margin-left: auto; }`.
2. Grep each selector below across `web/app web/components web/lib` (for example `grep -rn '"feed\|className="alert\|pchip\|cooldown\|"flag\|chart-wrap\|legend\|"tt"\|chart-foot' web/app web/components web/lib`) and delete only those whose sole users were the deleted components: `.flag`; the whole `alert feed` section (`.feed`, `.alert`, `.alert .*`, `.pchip`, `.pchip.hi`, `.alert.muted*`, `.cooldown`); and from the `chart` section `.chart-wrap`, `.legend`, `.legend i`, `.tt`, `.chart-foot`. Keep any a remaining file still uses.
3. In the `max-width: 1100px` block, change the comment `chart and alerts, then the` to `chart and its panel, then the`.
4. Add, after the `tbody tr.sel td:first-child` rule:

```css
tbody tr.empty-row { cursor: default; }
tbody tr.empty-row:hover { background: none; }
.empty-row td { text-align: center; color: var(--ink-muted); padding: calc(var(--sp) * 8) 12px; }
.partial-mark { margin-left: 4px; color: var(--copper-deep); font-size: 0.8rem; }
.pl-dash { display: block; text-align: center; font-size: 0.66rem; line-height: 6px; color: var(--ink-muted); }
.card-actions { padding: 0 calc(var(--sp) * 5) calc(var(--sp) * 4); }
.card.awaiting { margin: calc(var(--sp) * 4) 0 calc(var(--sp) * 10); padding-bottom: calc(var(--sp) * 2); }
.card > .pager { padding: calc(var(--sp) * 3) calc(var(--sp) * 5); }
```

- [ ] **Step 7: Verify it compiles, lints and builds**

```bash
cd web && npm run lint && npx tsc --noEmit && npm run build; cd ..
```

Expected: lint `0 errors` and `tsc` clean. `next build` completes and lists `/` among its routes. If lint reports `react-hooks/set-state-in-effect` in `page.tsx`, a `setState` has moved out of a `.then` callback. Move it back rather than disabling the rule.

- [ ] **Step 8: Commit**

```bash
git add -A web
git commit -m "Serve the real screen on the Overview and delete the concept data"
```

---

### Task 10: Errata

**Files:**
- Modify: `CLAUDE.md`, `PLAN.md`, `docs/architecture.md`

**Interfaces:** none.

- [ ] **Step 1: `CLAUDE.md`**

- Delete the whole `screener.concept` bullet.
- In the `screener.bot` bullet, after the sentence ending `rather than chosen by the model.`, add: `` The `chart` tool draws a security's real adjusted closes, read as `playground_bot` through the `screener.screen` statements the dashboard's endpoints use, captioned with the latest scored night's scores. ``
- In the same bullet, change `A chart is one SVG string built by \`web/lib/chart-svg.ts\`` to `` A chart is one SVG string built by `web/lib/chart-svg.ts`, in score or price mode, ``.
- In the `screener.screen` bullet, change `and, from piece (c) of the UI swap, for Steven's chart` to `` and, through `playground.select`, for Steven's chart ``.
- At the end of that bullet, add: `A symbol that several securities hold resolves to the only active one, because \`universe load\` leaves a departed security's symbol open; two active is ambiguous. Only prices can be \`refreshed\`: a fact is appended, never rewritten.`

- [ ] **Step 2: `PLAN.md`**

- Rename `### Steven draws charts — built on concept data` to `### Steven draws charts — real adjusted prices`.
- Replace its first paragraph ("Built, on the explicit understanding…") with: `Built, and on real data since the UI swap. Ask how a ticker has moved, its high or low, or its biggest surge or drop, and the \`chart\` tool draws its 60 trading days of adjusted closes under the reply, captioned with the latest scored night, with that point marked and dated.`
- Keep the two decisions. In the second, change the mark list to `` `peak`, `low`, `surge`, `drop`, `latest` ``.
- Replace the paragraph beginning `` `screener.concept` holds the invented data`` with: `` `screener.concept` and `web/lib/data.ts` are deleted. The tool reads through `playground.select` as `playground_bot`, in three statements shared with the dashboard's endpoints, so the chart in Discord and the chart on the Overview draw the same closes. `crossing` returns with score history and alerting. ``
- In the "Still not built — but no longer blocked" list, delete the **Real data** bullet and the **A tool over the snapshot tables** bullet.
- Under `## Done`, add this entry at the top, after the heading:

  ```
  **UI swap** — merged in #50, #51 and this piece's pull request. The Overview reads the real v2 screen: filters, a paged table, a price chart and a "Why this score" panel that re-checks every metric under its run's own view. Steven's chart reads the same data. Spec: `docs/specs/2026-09-13-ui-swap.md`. Plans: `docs/plans/2026-09-13-ui-swap-a-scoring-explains.md`, `docs/plans/2026-09-14-ui-swap-b-read-path.md`, `docs/plans/2026-09-14-ui-swap-c-page-and-steven.md`.
  ```

- [ ] **Step 3: `docs/architecture.md`**

- Delete the line `concept["concept<br/>delete when ingest lands"]`.
- Replace `tools --> concept` with `tools --> screen`.
- Run `grep -n "concept" docs/architecture.md`, and fix any prose mention it prints so it no longer describes the concept as present.

- [ ] **Step 4: Verify and commit**

Run: `grep -rn "screener.concept\|concept data\|illustrative" CLAUDE.md PLAN.md docs/architecture.md`
Expected: nothing that describes the concept as current. A historical mention in `PLAN.md`'s Done section is fine.

```bash
git add CLAUDE.md PLAN.md docs/architecture.md
git commit -m "Bring CLAUDE.md, PLAN.md and the architecture diagram up to the real screen"
```

---

### Task 11: Verify in a real browser, then open the pull request

**Done by the controller with the user, not by a subagent:** it needs a copy of production data and a signed-in browser.

- [ ] **Step 1: The full suites**

```bash
.venv/bin/python -m pytest -q
pyright
cd web && npm run lint && npx tsc --noEmit && npm run build; cd ..
```

Expected: only the 5 known magpie DNS failures; `pyright` 0 errors; web clean.

- [ ] **Step 2: A local stack on production's data**

1. Bring up the app stack locally as `deploy/README.md` describes (app Postgres on 5435).
2. Restore a `pg_dump --schema=public` of production that includes at least one v2 night.
3. Run `python -m screener.boot migrate`.
4. Run the status service with GitHub sign-in unconfigured, so `/auth/local` issues a session, and the web app behind the same origin.
5. Sign in through `/auth/local`.

- [ ] **Step 3: Walk §8's list, taking screenshots for the pull request**

- The header, finished time, weights and footer provenance match the run's row in `scoring_run`.
- Each filter, each sort, and paging forward and back. The range reads "1–50 of N". Page two does not repeat page one.
- The newer-night banner. Browse page two, run `python -m screener.scoring run --as-of <tomorrow>` locally, change the sort, see the banner, press Refresh.
- The panel:
  - a partial security (◐ in the row, "partial" in the panel);
  - a bank (book yield and ROE listed);
  - a recent splitter (the chart continuous across the split);
  - one metric absent for each of two different reasons.
- A saved chat thread from before this change, whose score chart still renders with its threshold and median.
- Steven: in the palette, ask for one security's 60-day chart with a surge marked. The chart is a price, the caption carries the scores, and nothing says "illustrative".
- The palette's "what am I looking at" names the selected real row.

- [ ] **Step 4: Open the pull request**

Push `ui-swap-page`. Open the pull request with the screenshots, a summary of C1–C7, and the attribution footer.
