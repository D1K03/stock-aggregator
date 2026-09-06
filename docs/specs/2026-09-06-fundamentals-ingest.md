# Fundamentals ingest

Status: agreed, not implemented. Written 2026-09-06.

Yahoo's fundamentals timeseries into `fundamental_fact` and the `ingest_observation` trail, plus
the point-in-time read that is the only thing able to prove those writes were correct. Standing
decisions live in `DESIGN.md`; the schema is `docs/specs/2026-09-04-database-schema-design.md`;
the securities come from `docs/specs/2026-09-05-universe-and-identity.md`; the run shape this
one mirrors is `docs/specs/2026-09-05-price-ingest.md`.

Scope: **the writes and the read, and nothing that scores.** No ratio is computed, no pillar
changes, `screener.scoring.CODES` stays the four momentum codes, and nothing on the dashboard
moves. This is the first exercise of the bitemporal fact layer — `period_end`, `period_type`,
`observed_at`, `restates_id`, the point-in-time read — none of which price ingest touched.
Keeping it to writes-plus-read is what lets a bug found here be a bug in the writes rather than
an argument about a metric definition.

---

## 1. What this has to satisfy

1. **Facts are bitemporal and append-only.** `period_end` says what a value describes,
   `observed_at` when we learned it, and a restatement is an insert. Nothing updates a fact.
2. **"Stored correctly" means the point-in-time read returns what was known on date D.** Rows
   existing is not the claim; a test that asserts the fields it just passed in is a tautology.
   The read ships in this cycle for that reason.
3. **Fundamentals cannot be backfilled.** They are restated rather than point-in-time, and Yahoo
   holds four annual and five quarterly periods. A line item not stored tonight has no recoverable
   value at tonight's `observed_at`, ever.
4. **Every fact traces back to a stored response.** `ingest_observation.blob_path` is `not null`,
   so the payload store cannot be something a cron job empties.
5. **A night must not need supervision.** Re-runnable, self-healing after failure, honest about
   partial success — the properties price ingest already has.
6. **No new runtime dependencies.** `httpx` and `psycopg`, as ever.

---

## 2. Findings that reshaped this spec

Measured 2026-09-06, before any of the decisions below. Recorded because two of them contradict
documents already in this repository, and a reader who trusts those would design the wrong thing.

**F1 — `quoteSummary`'s statement modules are empty.** `balanceSheetHistory`,
`balanceSheetHistoryQuarterly`, `cashflowStatementHistory` and
`cashflowStatementHistoryQuarterly` return `endDate` and `maxAge` and nothing else;
`cashflowStatement` adds `netIncome`. Only `incomeStatementHistory` is populated. So the endpoint
`DESIGN.md` describes as returning "all three financial statements" now yields one, and a spec
built on it would have no equity, no debt, no cash flow, no share count — no Valuation pillar at
all, and a Quality pillar of margins only. **`DESIGN.md` needs an erratum**: its description was
true when measured and is not now.

**F2 — `/ws/fundamentals-timeseries/v1/finance/timeseries/` carries all of it.** One request for
AAPL returned 19 populated series in 16 KB: revenue, gross profit, operating income, net income,
EBITDA, total assets, stockholders equity, total debt, cash, free cash flow, operating cash flow,
capital expenditure, current assets, current liabilities, and **basic and diluted average
shares** — which is what makes any price-relative ratio possible. Annual and quarterly both, each
value carrying an `asOfDate` that is exactly `period_end`.

**F3 — the payload is byte-stable.** Three consecutive fetches produced one hash. A result
carries only `meta` (symbol and type) and `timestamp` (the period ends), so nothing in it moves
between reports. This is what makes schema D4's content-hash dedup fire here — the problem
`DESIGN.md` proposes per-module hashing to solve is a `quoteSummary` problem, and this endpoint
does not have it.

**F4 — no crumb, no cookie.** A cold `httpx.Client` with no jar and no crumb parameter gets 200.
`PLAN.md`'s "the Yahoo path holds one session for the run" was written about `quoteSummary` and
does not apply. This client is shaped like `ChartClient`, not `YahooClient`.

**F5 — a fiscal Q4 shares its `period_end` with the fiscal year.** AAPL reports both
`annualTotalRevenue` and `quarterlyTotalRevenue` at `2025-09-30`, 416,161M against 109,417M. This
is structural, not an AAPL quirk: the fourth quarter of a fiscal year ends when the year does,
for every company, every year. Its consequence is D6.

---

## 3. Decisions

D-numbers below belong to this spec. References to other specs' decisions are written
`schema D<n>` and `price D<n>`.

**D1 — The source is the fundamentals timeseries endpoint, not `quoteSummary`.** Per F1 and F2.
Sixteen series are requested annual *and* quarterly in one call per security — 32 `type=` values —
with a `period1` far enough back to take everything Yahoo holds. `screener.universe` continues to
use `quoteSummary` for profiles and is untouched by this.

The series, and the `metric` row each one seeds. `code` is ours and is what any later ratio names;
the Yahoo type is `annual`/`quarterly` prefixed onto the stem. `cadence` is `'quarterly'` for all
sixteen — the schema's vocabulary for "reported per period" rather than a claim that only
quarterlies are stored. `unit` is `'currency'` except where noted, and every row carries
`is_input = true` and `higher_is_better = true`, the latter unread because nothing ranks an input.

| `metric.code` | Yahoo type stem | unit | nominal pillar |
|---|---|---|---|
| `revenue` | `TotalRevenue` | currency | valuation |
| `gross_profit` | `GrossProfit` | currency | quality |
| `operating_income` | `OperatingIncome` | currency | quality |
| `net_income` | `NetIncome` | currency | valuation |
| `ebitda` | `EBITDA` | currency | valuation |
| `total_assets` | `TotalAssets` | currency | quality |
| `stockholders_equity` | `StockholdersEquity` | currency | valuation |
| `total_debt` | `TotalDebt` | currency | quality |
| `cash` | `CashAndCashEquivalents` | currency | quality |
| `free_cash_flow` | `FreeCashFlow` | currency | valuation |
| `operating_cash_flow` | `OperatingCashFlow` | currency | quality |
| `capital_expenditure` | `CapitalExpenditure` | currency | quality |
| `current_assets` | `CurrentAssets` | currency | quality |
| `current_liabilities` | `CurrentLiabilities` | currency | quality |
| `shares_basic` | `BasicAverageShares` | shares | valuation |
| `shares_diluted` | `DilutedAverageShares` | shares | valuation |

**The "nominal pillar" column is the fiction D8 describes**, recorded here so the seed does not
have to invent it twice. `shares_diluted` genuinely feeds both pillars; `revenue` feeds Valuation
through P/S and Quality through every margin. The column is filled because the schema requires it,
not because it routes anything.

`value` is `numeric`, so a share count and a currency amount coexist without scaling. Yahoo
reports `CapitalExpenditure` as a negative number; it is stored exactly as reported, per D3 —
sign conventions are the consuming ratio's problem, not the fact's.

**D2 — The client holds no session.** Per F4: no crumb, no cookie jar, nothing carried between
requests, so `screener.ingest.timeseries.TimeseriesClient` is `ChartClient`'s shape rather than
`YahooClient`'s. It goes over the same `LanePool` for exit-address spreading and inherits the
same per-security failure boundary. Reasoning worth keeping: the crumb machinery exists because
a crumb is only valid alongside the cookie issued with it, and an endpoint needing neither should
not pay for the machinery that serves one that does.

**D3 — Raw line items only. No ratio is ever stored.** What goes into `fundamental_fact` is what
Yahoo reports for a period: revenue, net income, equity, shares. P/E, ROE and every other ratio
are computed at scoring time from these plus `price_daily`.

Two reasons, and the second is the load-bearing one. A ratio mixing price with fundamentals has
no honest `period_end` — it is an as-of-today value in a period-shaped table. And a restatement
of one line item must move every ratio built on it; storing the ratio instead would leave the
restatement invisible to everything downstream of it.

**D4 — Facts are appended on a changed value, not on a changed hash.** For each
`(security_id, metric_id, period_end, period_type)`, a row is inserted only when the value differs
from the latest already held. A security whose fundamentals did not move writes no
`fundamental_fact` row and no blob.

Value comparison rather than hash comparison because the two answer different questions: the hash
decides whether to store the payload, the value decides whether we learned something. They
coincide here only because F3 makes the payload stable, and a design that leaned on that
coincidence would break the day a volatile field is added upstream.

**Its `ingest_observation` is written every night regardless.** The record that we checked on a
date is what makes `observed_at` mean anything: without it, "we did not know this on Tuesday" and
"we did not look on Tuesday" are the same absence.

**D5 — A changed value sets `restates_id` to the row it supersedes.** The column exists for
exactly this and populating it makes a restatement chain walkable without a self-join on
timestamps. It is set at insert time from the row the value comparison already had to read, so
it costs nothing.

**D6 — `period_type` is part of the point-in-time key, and the schema's stated read is wrong
without it.** Per F5, `(security_id, metric_id, period_end)` is not unique across period types, so

```sql
distinct on (security_id, metric_id, period_end) ... order by observed_at desc
```

— the read named in `PLAN.md` and in the schema spec — collapses a fiscal year into its own
fourth quarter and returns whichever was inserted last. For AAPL's FY2025 that is a four-fold
error, silent, every year, for every company. The correct read is

```sql
select distinct on (security_id, metric_id, period_end, period_type)
       security_id, metric_id, period_end, period_type, value, observed_at
  from fundamental_fact
 where security_id = any(%(ids)s)
   and observed_at <= %(cutoff)s
 order by security_id, metric_id, period_end, period_type, observed_at desc
```

`fundamental_fact`'s unique constraint already includes `period_type`; its `pit` index does not,
so a migration adds `(security_id, metric_id, period_type, period_end, observed_at desc)`. The
old index is left alone: it still serves a query that filters to one period type.

`cutoff` is `screener.scoring.visibility_cutoff(as_of, cutoff_offset)`, imported rather than
reimplemented, so prices and fundamentals answer to one definition of what a scoring date may see.

**D7 — Both `'Q'` and `'A'` are stored; `'TTM'` is never written.** Yahoo reports quarterly and
annual, and both are stored as reported. TTM is a sum of four quarters — a derivation, and D3
says derivations are computed rather than stored. `period_type` keeps its `'TTM'` value unused in
this cycle, which is honest rather than wasteful.

**D8 — Line items are `metric` rows flagged as inputs, by a column added for the purpose.**

```sql
alter table metric add column is_input boolean not null default false;
```

`is_input = true` means stored as evidence and never scored. `is_active` is *not* reused for
this, though it would appear to work today: it means live versus retired, and the first metric
retired would make "which of these false rows are inputs and which are dead?" unanswerable from
the data. The collision is certain rather than hypothetical, and the column is cheap now.

**`pillar_id` on an input row is a fiction.** The column is `not null` so it must hold something,
and each input is seeded against the pillar that primarily consumes it — but `shares_diluted`
feeds Valuation and Quality both, and any query grouping inputs by pillar will get a
plausible-looking wrong answer. The migration says so where someone reading it would look.

**D9 — Its own `ingest_run`, its own command.** `ingest_run.endpoint` is a single text column, so
one row cannot describe two endpoints. `python -m screener.ingest fundamentals` is a sibling to
`prices`, with a separate run row. This is also what keeps the halves independent: they fail
separately, and a partial fundamentals night must not touch the per-security window derivation
that makes a partial *price* night self-healing.

**D10 — No window derivation, and backfill is not a mode.** Unlike prices there is nothing to
derive: Yahoo returns four annual and five quarterly periods regardless of what is asked for, so
every run requests the same wide range and D4 decides what is new. A security seen for the first
time has no held values, so its whole history inserts — which is backfill, arriving as the
ordinary path rather than a branch.

**D11 — A null inside a series array is skipped, never stored.** Yahoo pads its arrays with
nulls. A null becoming `0` in `value` would be a fabricated fundamental, and `value` is `not
null` precisely so that a missing number cannot be mistaken for a real one.

---

## 4. Conventions this follows

- Each package has a small public surface through `__init__.py`; nothing outside imports a
  submodule directly.
- Pure computation is separated from database work, as `screener.ingest` already separates
  `parse` from `load`: `facts.py` opens no connection, `load.py` opens no socket.
- `Decimal` throughout, never `float`.
- All SQL identifiers lowercase; `timestamptz` never `timestamp`.
- Comments explain *why*, not *what*.

---

## 5. Layout

`screener.ingest` is extended rather than joined by a sibling package — its own docstring
already says "Prices in this cycle; fundamentals in the next", and the two halves share
`active_securities`, the blob store, the observation trail and the run lifecycle. A separate
package would duplicate those or depend on them sideways.

```
screener.ingest
  timeseries.py   the client: one request per security, no session       [new]
  facts.py        payload -> typed line items                     [new, pure]
  load.py         + insert_facts, latest_values, read_facts       [extended]
  run.py          + run_fundamentals                              [extended]
  cli.py          + the `fundamentals` command                    [extended]
```

A new migration adds `metric.is_input`, the corrected point-in-time index, and the 16 seeded
input metrics.

---

## 6. `python -m screener.ingest fundamentals`

```
open ingest_run   endpoint='timeseries', status='running'
   │
   └─ per security:
      ├─ fetch the 16 series, annual and quarterly     (one request, no crumb)
      ├─ hash; unchanged -> no blob write, reuse the stored path
      ├─ parse -> line items, nulls skipped                        [pure]
      ├─ read the latest held value per (metric, period_end, period_type)
      ├─ insert only what differs, restates_id set to what it supersedes
      └─ write the observation, changed or not
   │
   └─ close run (ok | partial | failed)
```

One transaction per security, as prices does: each security is independent evidence, and a night
that dies halfway leaves what it had rather than nothing.

---

## 7. Failure behaviour

| failure | behaviour |
|---|---|
| a transport or database error for one security | that security fails, the night continues, counted and logged |
| a 404 for a symbol | same — a delisted or renamed symbol is one security's problem |
| an empty or unparseable body | that security fails; nothing is inserted from a payload we could not read |
| the blob store cannot be written | systemic: the run ends, because an observation must never name an object that was not written |
| the run dies partway | the securities already committed stand; the next run re-reads and inserts nothing for them, because D4 compares values |

---

## 8. Testing

Postgres 16 for the database halves; `facts.py` needs nothing. Load-bearing claims, demonstrated
rather than asserted:

- **The restatement round-trip.** Insert a Q2 fact, insert a revision of the same Q2 with a later
  `observed_at`, then read as-of a date between the two and get the original value, and as-of
  today and get the revision. This is the test that proves the design rather than the code: if it
  passes the bitemporal layer works, and if it fails the bug is in the cycle built to find it.
- **A quarterly and an annual sharing a `period_end` both survive the read, distinctly** — the
  regression test for D6, using F5's real shape: a fiscal year and its own fourth quarter, both
  ending 2025-09-30, differing four-fold.
- **Two identical nights produce one set of facts and two observations.** Append-on-change and
  the evidence trail are separate mechanisms, and this is where they are proved separate.
- An unchanged payload writes no blob, and its observation names the object that *was* written.
- `restates_id` points at the row it supersedes, and a three-deep chain walks.
- `cutoff_offset` excludes a fact observed after the cutoff even when its `period_end` qualifies.
- A null inside a series array is skipped rather than stored as zero.
- One security's failure does not end the night, and the run reports `partial`.
- Seeded input metrics carry `is_input = true`, and the four momentum metrics do not.

---

## 9. Out of scope

- **Every ratio.** P/E, P/B, ROE, margins and the rest are the next cycle's, per D3.
- **Every pillar change.** `CODES` stays the four momentum codes, no `pillar_score_daily` row
  moves, and no snapshot changes.
- **The dashboard.** `screener.concept` stays, `illustrative` stays `true`, and nothing on screen
  moves. The swap is its own work and needs three pillars to be worth doing.
- **TTM.** Derived at scoring time when something needs it, per D7.
- **Source precedence.** `fundamental_fact` still has no `source_id` and `metric` gains none.
  There is one source, so there is no ambiguity to resolve and no evidence to resolve it with:
  which source wins for a metric is a judgement about two sources' quality. `PLAN.md`'s
  carried-forward note stands, and a second source is still the gate.
- **`quoteSummary` and the crumb path.** `screener.universe` still uses both; this cycle neither
  changes nor removes them.
- **Sentiment and Insider.** Both need sources that do not exist — FinBERT over `screener.reddit`,
  which is ingest-only and links no item to a security, and Form 4 / 13F, which has no adapter.
- **Scheduling.** Nothing runs this on a timer, exactly as nothing yet runs prices or scoring.

---

## 10. Open parameters

- **How far back `period1` reaches.** Yahoo returns four annual and five quarterly periods
  whatever is asked, so the value is a formality until it is not. Worth re-measuring if the shape
  of the response ever changes.
- **Whether `basic` and `diluted` average shares are both worth storing.** Both are stored
  because D3's "cannot be backfilled" argument applies to every line item, but only one will be
  used by the first ratio that needs a share count.
- **Whether an unchanged night should still write an observation for a security that failed.**
  It does not today: a failure writes nothing, so the trail records the check that succeeded and
  is silent about the one that did not. The `ingest_run` counts carry that instead.

---

## 11. Errata this cycle owes other documents

- **`DESIGN.md`** describes one `quoteSummary` call as returning "profile, all three financial
  statements, key statistics, financial data, earnings trend and recommendation trend together —
  19 KB raw". Per F1 the statement modules are now empty. The sentence was true when measured and
  is not now, and anything reasoning from it — including the 43/29/28 stability split and the
  per-module hashing proposal — is about an endpoint this cycle does not use.
- **`PLAN.md`** says ingest "inherits `screener.fetch` for every source but Yahoo: the crumb is
  only valid alongside the cookie issued with it, so the Yahoo path holds one session for the
  run." Per F4 that is true of `quoteSummary` and false of this endpoint.
- **The schema spec** documents the point-in-time read as
  `distinct on (security_id, metric_id, period_end) ... order by observed_at desc`. Per D6 that
  is wrong wherever both period types are held, which is everywhere.
