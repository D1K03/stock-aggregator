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

**F2 — `/ws/fundamentals-timeseries/v1/finance/timeseries/` carries all of it.** A first request
for AAPL returned every series asked for: revenue, gross profit, operating income, net income,
total assets, stockholders equity, total debt, cash, operating cash flow, capital expenditure,
current assets, current liabilities, and **basic and diluted average shares** — the last of which
is what makes any price-relative ratio possible. Annual and quarterly both, each value carrying an
`asOfDate` that is exactly `period_end`. (That request returned 19 *series objects* for 16 distinct
line items, because three were asked for quarterly as well as annually. Series objects are not
line items, and this spec counts line items.)

**F6 — every statement line the ratios will want is available, and the payload names its own
currency.** A second, wider request found all 24 further candidates populated, among them
`TaxProvision`, `InterestExpense`, `PretaxIncome`, `EBIT`, `DepreciationAndAmortization`,
`CostOfRevenue`, `LongTermDebt`, `CurrentDebt`, `NetPPE` and `OrdinarySharesNumber`. Three things
fell out of it:

- **Each value carries `currencyCode`** (`USD` throughout for AAPL). Currency is therefore read
  from the payload rather than assumed, which D12 turns into a decision.
- **`OrdinarySharesNumber` (14,773,260,000) differs from `BasicAverageShares` (14,948,500,000).**
  The first is shares outstanding at the period end, the second an average across the period. Both
  are stored, under names that say which, because a ratio wanting one and getting the other is
  wrong by a percent or two — the least detectable size of wrong.
- **`EBIT` + `DepreciationAndAmortization` reproduces `EBITDA` exactly** (133,050M + 11,698M =
  144,748M). So EBITDA is a derivation Yahoo happens to publish, and D3 excludes it.

**F7 — coverage is patchy per metric and per year, and this is the finding with the longest
reach.** AAPL's `InterestExpense` ends at 2023-09-30 with nothing for 2024 or 2025 while every
neighbouring series runs to 2025-09-30. Measured across a 60-security random sample of the loaded
universe, all 60 answering:

| stem | any period | a period at or after 2024-06-30 |
|---|---|---|
| `TotalRevenue`, `NetIncome`, `PretaxIncome`, `OperatingCashFlow`, `OrdinarySharesNumber` | 60 | 60 |
| `StockholdersEquity`, `TotalDebt`, `NetPPE` | 59–60 | 59 |
| `TaxProvision`, `DepreciationAndAmortization` | 57–58 | 57–58 |
| `CapitalExpenditure` | 57 | 56 |
| `InterestExpense` | 55 | **54** |
| `EBIT`, `CashCashEquivalentsAndShortTermInvestments` | 54 | 54 |
| `CurrentDebt` | 47 | **43** |

**AAPL is not representative**: interest expense is recently present for 90% of the sample, not a
third. But 90% still means roughly 150 of 1,504 securities with no interest coverage ratio, and
`CurrentDebt` at 72% is thin enough that any ratio resting on it starts compromised. That
constrains which Quality ratios are viable, and it is knowable now rather than after they are
designed.

**Two different absences, and only one is already handled.** Schema D9 drops a missing metric from
its pillar's average and records `metric_count` and `coverage` — that answers *a metric absent for
a security*. F7 is *a period absent for a metric*, which D9 does not see: the point-in-time read
returns the latest fact held, so a ratio taking "the latest value per metric" will pair 2025
revenue with 2023 interest expense and produce a number that looks entirely reasonable. The same
silent-wrong shape as D6's collapsed period, arriving by a different route.

**So the ratios cycle needs a staleness rule, not only a coverage rule**: a maximum age of a fact
relative to `as_of`, beyond which it is not eligible and its ratio is absent rather than stale.
Named here, with the measurement in front of us, so that cycle inherits the question already
sharpened — the rule's *value* is its business, since it trades coverage against freshness, but
the need for one is settled. D11's withdrawal case is the same rule seen from the other side: a
figure the provider has stopped standing behind and one it stopped reporting in 2023 are
indistinguishable in the fact table, and a staleness bound is what makes both harmless.

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

The series, and the `metric` row each one seeds. `code` is ours and is what any later ratio
names; the Yahoo type is `annual`/`quarterly` prefixed onto the stem. Both prefixes are requested
for every stem, so the list below is 28 metrics and 56 `type=` values.

**What is on the list and what is not** follows one rule, which is D3 applied consistently: a line
Yahoo *reports* is stored; a figure Yahoo *computes* from lines we already store is not. So
`EBITDA`, `NetDebt`, `WorkingCapital`, `InvestedCapital`, `TangibleBookValue`,
`TotalCapitalization` and `FreeCashFlow` are all deliberately absent — each is exactly reproducible
from stored inputs (F6 checked EBITDA against EBIT + D&A, and free cash flow against operating
cash flow minus capital expenditure, to the reported figure). `ReconciledDepreciation`,
`DepreciationAmortizationDepletion`, `InterestExpenseNonOperating` and `ShareIssued` are absent as
exact duplicates of a stem already listed.

| `metric.code` | Yahoo type stem | unit | nominal pillar |
|---|---|---|---|
| `revenue` | `TotalRevenue` | currency | valuation |
| `cost_of_revenue` | `CostOfRevenue` | currency | quality |
| `gross_profit` | `GrossProfit` | currency | quality |
| `research_and_development` | `ResearchAndDevelopment` | currency | quality |
| `selling_general_admin` | `SellingGeneralAndAdministration` | currency | quality |
| `operating_income` | `OperatingIncome` | currency | quality |
| `ebit` | `EBIT` | currency | valuation |
| `interest_expense` | `InterestExpense` | currency | quality |
| `pretax_income` | `PretaxIncome` | currency | quality |
| `tax_provision` | `TaxProvision` | currency | quality |
| `net_income` | `NetIncome` | currency | valuation |
| `depreciation_amortisation` | `DepreciationAndAmortization` | currency | valuation |
| `operating_cash_flow` | `OperatingCashFlow` | currency | quality |
| `capital_expenditure` | `CapitalExpenditure` | currency | quality |
| `total_assets` | `TotalAssets` | currency | quality |
| `current_assets` | `CurrentAssets` | currency | quality |
| `current_liabilities` | `CurrentLiabilities` | currency | quality |
| `total_liabilities` | `TotalLiabilitiesNetMinorityInterest` | currency | quality |
| `stockholders_equity` | `StockholdersEquity` | currency | valuation |
| `cash_and_equivalents` | `CashAndCashEquivalents` | currency | quality |
| `cash_and_short_term_investments` | `CashCashEquivalentsAndShortTermInvestments` | currency | quality |
| `current_debt` | `CurrentDebt` | currency | quality |
| `long_term_debt` | `LongTermDebt` | currency | quality |
| `total_debt` | `TotalDebt` | currency | quality |
| `net_ppe` | `NetPPE` | currency | quality |
| `shares_basic_avg` | `BasicAverageShares` | shares | valuation |
| `shares_diluted_avg` | `DilutedAverageShares` | shares | valuation |
| `shares_outstanding` | `OrdinarySharesNumber` | shares | valuation |

Four names earn their length. `cash_and_equivalents` and `cash_and_short_term_investments` are
both stored and differ materially for a cash-rich company — AAPL reports 35,934M and 54,697M for
the same period end — so a bare `cash` would leave the next reader guessing which was taken. The
`_avg` on `shares_basic_avg` and `shares_diluted_avg` says these are period averages, against
`shares_outstanding` which is the count at the period end; F6 measured them a percent apart, which
is exactly the size of error nobody notices. **Codes are permanent once seeded**, so they say what
they are rather than what is shortest.

**`shares_outstanding` is the one row in this table that describes an instant rather than a
period.** Its `period_end` is honest — the count is as at that date — so the schema holds and
storing it needs no special case. But the next cycle has to reach for the right one: a
period-average count belongs in anything earned *over* the period (EPS, and the P/E built on it),
while the point-in-time count belongs in market capitalisation, which is a fact about a moment.
The other 27 metrics are period quantities and need no such care.

`total_debt` is kept despite `current_debt` and `long_term_debt` also being stored, because a
provider's total-debt definition often includes lease obligations in neither component — it is a
reported line here rather than a derivation, which is why the rule above says "computes from lines
we already store" rather than "computes".

**The rule is about provenance, not redundancy**, and that matters because this list is
deliberately redundant in places. `pretax_income`, `tax_provision`, `net_income` and `ebit` are
related by the usual identities, and a reader applying the rule too eagerly would drop two of them.
They stay because each is a line Yahoo reports rather than a figure Yahoo computes — and the
redundancy earns its place, since reported numbers that ought to reconcile are a cross-check that a
restatement landed coherently. Redundancy is a reason to drop something only when the duplicate is
*exactly* reproducible from stored inputs, as EBITDA and free cash flow were measured to be.

`cadence` is `'quarterly'` on all 28 and `higher_is_better` is `true`. Neither is read: period
granularity lives in `period_type` per fact, and nothing ranks an input. They are `not null`
columns being filled.

**The "nominal pillar" column is the fiction D8 describes**, recorded here so the seed does not
have to invent it twice. `shares_diluted_avg` genuinely feeds both pillars, and `revenue` feeds
Valuation through P/S and Quality through every margin. The column is filled because the schema
requires it, not because it routes anything.

`value` is `numeric`, so a share count and a currency amount coexist without scaling. Yahoo
reports `CapitalExpenditure` as a negative number; it is stored exactly as reported, per D3 — sign
conventions are the consuming ratio's problem, not the fact's.

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

**`observed_at` is `ingest_observation.fetched_at`, identical for every fact parsed from one
payload.** Not insert time, not parse time, not `now()` evaluated per row. This is the column the
whole cycle exists to prove correct, and a per-row clock would break it in the least visible way
available: two facts from one response would carry different timestamps, `order by observed_at
desc` would become non-deterministic between them, and D5's `restates_id` could point at a row
stamped *later* than the row superseding it. A single value read once per security and passed
down makes that unrepresentable rather than merely unlikely.

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

`fundamental_fact`'s unique constraint already includes `period_type`; its `pit` index does not.
The migration therefore adds

```sql
create index fundamental_fact_pit_idx2 on fundamental_fact
    (security_id, metric_id, period_end, period_type, observed_at desc);
```

**Column order matches the read's `order by` exactly, and has to.** An index ordered
`(security_id, metric_id, period_type, period_end, observed_at desc)` — period_type before
period_end — cannot satisfy this `distinct on` without a sort node, so it would be paid for on
every insert and then sorted around on every read.

The existing `fundamental_fact_pit_idx` is **dropped in the same migration.** It was
`(security_id, metric_id, period_end, observed_at desc)`, and the only query it serves better than
the new index is "the latest observation of one metric for one period end, across both period
types" — which is the query D6 exists to say nobody should run. Keeping it would cost an index
write per fact per night to serve a mistake.

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

**D11 — A null is skipped, and a withdrawn value is left standing.** Yahoo pads its arrays with
nulls, and a null becoming `0` in `value` would be a fabricated fundamental — `value` is `not null`
precisely so a missing number cannot be mistaken for a real one. That much is a parse rule.

The case it does not cover is a **withdrawal**: Yahoo reported a figure for a period on Monday and
returns null for that same period on Tuesday. Skipping means Monday's value stays the latest, so
the point-in-time read keeps serving a number the provider has stopped standing behind.

That is the intended behaviour and it is a decision, not a parse rule. An absence is not a
restatement, and the alternative — writing a tombstone row — cannot be expressed anyway, because
`value` is `not null`. What it costs is real and worth naming: a withdrawn figure is
indistinguishable from a current one in the fact table, and the only trace is that later
observations stop refreshing it. F7 makes this concrete rather than theoretical — AAPL's
`interest_expense` simply stops after 2023 — so the ratios cycle should treat the age of a fact as
information, not assume the latest value is currently reported.

**D12 — `currency` is read from the payload, never assumed.** Per F6 each value carries a
`currencyCode`, so `fundamental_fact.currency` is populated from it rather than defaulted to USD
from the US-only universe. The column is nullable and a null would be indistinguishable from
"unknown" for ever, on data that cannot be backfilled; taking the provider's own answer costs one
field in the parser and survives the day a non-USD listing enters the universe.

If a value arrives with no `currencyCode`, the fact is stored with `currency` null rather than
guessed. A null then means "the provider did not say", which is true, rather than "we assumed
dollars", which might not be.

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

A new migration adds `metric.is_input`, replaces the point-in-time index per D6, and seeds the
28 input metrics.

---

## 6. `python -m screener.ingest fundamentals`

```
open ingest_run   endpoint='timeseries', status='running'
   │
   └─ per security:
      ├─ fetch 28 stems, annual and quarterly          (one request, no crumb)
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
- **Every fact from one night carries its observation's `fetched_at`.** The direct test for D4's
  `observed_at` rule: read back a night's facts and assert each one's `observed_at` equals the
  `ingest_observation.fetched_at` it hangs from, with one distinct value across the security.
- **A value that reverts produces three rows and a two-link chain.** A on Monday, B on Tuesday, A
  again on Wednesday: Wednesday must insert rather than match, because the comparison is against
  the latest observation and not against every value ever seen. Proves D4 compares the right thing.
- A withdrawn value leaves the last reported figure as the point-in-time answer, per D11.
- `currency` is populated from the payload, and a value without one stores null rather than USD.
- One security's failure does not end the night, and the run reports `partial`.
- Seeded input metrics carry `is_input = true`, and the four momentum metrics do not.

---

## 9. Out of scope

- **Every ratio.** P/E, P/B, ROE, margins and the rest are the next cycle's, per D3.
- **Every pillar change.** `CODES` stays the four momentum codes, no `pillar_score_daily` row
  moves, and no snapshot changes.
- **The dashboard.** `screener.concept` stays, `illustrative` stays `true`, and nothing on screen
  moves. The swap is its own work and needs three pillars to be worth doing.

**What this cycle is worth, stated plainly, since none of it is visible:** the next cycle cannot
be wrong quietly. Every ratio it computes rests on `observed_at` meaning what it says and on the
point-in-time read returning what was known — and both are proved here, against a real database,
before anything depends on them.

That is a legitimate thing to merge with nothing on screen, and it carries one obligation: **this
cycle and the ratios cycle should be planned as a pair.** Facts accumulating with no consumer is
precisely the state in which an `observed_at` bug survives a hundred nights, and every one of
those nights writes history that cannot be corrected — the point-in-time record of what we knew is
not re-derivable once wrong. The gap between "facts land" and "something reads them" should be
weeks.
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
- **Which of the three share counts each future ratio should take.** All three are stored because
  the "cannot be backfilled" argument applies to every line item, and F6 measured the averages and
  the outstanding count a percent apart. The convention — diluted average for per-share earnings
  figures, outstanding for market capitalisation — belongs to the cycle that computes them.
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
  is wrong wherever both period types are held, which is everywhere. `PLAN.md` repeats it in its
  carried-forward note on source precedence and needs the same correction — the note's argument
  about restatements from one source is unaffected, only the query it quotes.
- **Migration 005** ships `fundamental_fact_pit_idx` on
  `(security_id, metric_id, period_end, observed_at desc)`, which encodes the same missing
  `period_type`. D6 replaces it; that is a dropped index rather than a doc fix, and it is the only
  thing this cycle changes about a settled table.
