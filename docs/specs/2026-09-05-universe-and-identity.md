# Universe and identity

Status: agreed, not implemented. Written 2026-09-05.

How the v1 ticker universe is chosen, refreshed and loaded into `security`,
`security_symbol`, `security_sector`, `sector_node` and `peer_group`. Standing decisions live in
`DESIGN.md`; the schema is `docs/specs/2026-09-04-database-schema-design.md`.

Scope: the S&P 1500, US-only. Everything downstream needs securities to exist, so this is the
first slice of the v1 spine.

---

## 1. What this has to satisfy

1. **The universe changes quarterly and history must survive it.** Names are added, dropped,
   renamed and acquired. A departure must not delete anything.
2. **Sector classification drives peer groups**, so a reclassification silently moves a ticker
   between peer groups and changes its score for reasons unrelated to the company.
3. **The fragile parts must not sit on the daily path.** `DESIGN.md` treats scrapers as the
   fragile layer, and there is no free API for index constituents.
4. **One HTTP client.** `pyproject.toml` declares httpx the only network client, and
   `screener.fetch` is the layer that wraps it.

---

## 2. Decisions

**D1 — Retire, never delete.** A ticker leaving the index gets `is_active = false` and
`last_seen` set. Its securities, facts and scores stay. Wiping and reloading the universe each
refresh would destroy the record of what was in it when, so any later backtest would see only
today's survivors — textbook survivorship bias, and invisible once it has happened.

**D2 — No universe-membership table.** The obvious objection to D1 is that `is_active` plus one
`first_seen`/`last_seen` pair cannot express a name that leaves and later rejoins. It does not
need to: `snapshot_daily` already records which securities were *scored* on each date, which is a
record of what happened rather than what a membership table claimed. Re-entry clears `last_seen`
and flips `is_active`; the gap stays visible in the snapshot log.

**D3 — Two commands, and only one of them writes to Postgres.**

```
refresh-universe   Wikipedia + Yahoo + SEC  →  data/universe.csv      [no database]
load-universe      data/universe.csv        →  Postgres               [no network]
```

`refresh-universe` never opens a database connection, so it needs no credentials and runs
anywhere. `load-universe` never opens a socket, so it is deterministic and testable offline.

The review surface is the point. A sector reclassification moves a ticker between peer groups,
and `DESIGN.md` already warns that a score can move because peers moved — a reclassification is
the extreme case, and it would otherwise be invisible. Routed through a committed CSV it becomes
a line in a pull request diff that has to be looked at before it can affect a score. Membership
changes and reclassifications land in the same reviewable place, and the universe as it stood at
any past commit is recoverable from git.

**D4 — Match on CIK, not symbol.** This is the decision the reconciler hangs on. Matching by
symbol makes a rename look like a departure plus an unrelated arrival: FB leaves, META appears,
and Meta's entire history is orphaned behind a dead symbol. CIK is stable across renames. SEC
publishes `company_tickers.json` — one request, every US filer, ticker to CIK — which is an API
rather than a scrape, and is what EDGAR ingest will need anyway.

**D5 — Call Yahoo directly; no yfinance.** Measured: through the library, eight concurrent
workers lost 43% of 1,506 requests and sequential calls took ~0.35s each. Direct, with a
persistent client and a cached crumb, 30 sequential requests completed in 3.4s with zero
failures. The rate limit was the library's, not Yahoo's.

The stronger reason is architectural. `DESIGN.md` stores unparsed source JSON in Blob and uses
`content_hash` to detect restatements. yfinance emits parsed DataFrames, so a payload stored from
it is the library's reshaping rather than the response: the hash would change whenever yfinance
changed its parser, which is indistinguishable from a provider restatement, and *"did the API lie
or did our parser?"* — the question the traceability chain exists to answer — becomes
unanswerable. A library that only emits parsed output erases the line D3 of the schema spec draws.

**D6 — Sector level only, and `security_sector` points at the industry.** Measured against the
S&P 1500: at 500 tickers, 95% of names sit in an industry group holding fewer than 20 peers; at
1,500 it is still 59%. Sector groups clear 20 members at every size. So v1 scores at sector.

The classification stored is nonetheless the **industry** — the most specific fact available —
with the sector reachable by walking `parent_id`. Storing the coarser fact while holding the
finer one discards information for no gain, and it makes adding an industry rung later a scoring
change with no data backfill.

---

## 3. Conventions this follows

- Entry point mirrors `screener.boot`: a `screener.universe` package with `__main__.py` and an
  argparse `main(argv)` taking a positional command, so `python -m screener.universe refresh`
  and `python -m screener.universe load` read the same as `python -m screener.boot migrate`.
- HTTP goes through `screener.fetch.fetch()`, which returns a `FetchResult` carrying `text`,
  `status_code` and the `strategy` that served it. Its `transport` parameter is how these are
  tested without network.
- Database access uses `screener.config.settings().database_url` and psycopg 3, as
  `boot.startup.prepare_database` does.
- `text` never `varchar(n)`, `timestamptz` never `timestamp`, `numeric` never float.

---

## 4. `refresh-universe`

Three sources, one output file.

| Source | Endpoint | Auth | Gives |
|---|---|---|---|
| Wikipedia | three `List_of_S&P_N_companies` pages | none | symbol, name, GICS sector |
| SEC | `www.sec.gov/files/company_tickers.json` | declared User-Agent | ticker → CIK |
| Yahoo | `/v10/finance/quoteSummary/{sym}?modules=assetProfile,price` | crumb | sector, industry, exchange, currency |

**Two Yahoo modules, not one.** `assetProfile` carries sector and industry but **not** exchange
or currency — those are in the `price` module, which returns `exchange` (`NMS`, `NYQ`) and
`currency` alongside. Both come back in a single request.

**SEC requires a declared User-Agent.** The file returns 403 to a request without one; SEC asks
for an identifying string with contact details. With one it is 200 and 778 KB, covering 10,412
filers. This is a hard block, not a courtesy.

**The Yahoo crumb.** `quoteSummary` returns 401 `Invalid Crumb` unauthenticated. The handshake is
a cookie from `https://fc.yahoo.com`, then a token from
`https://query1.finance.yahoo.com/v1/test/getcrumb`, appended as `&crumb=`. Fetched once per run
and reused; a 401 mid-run refreshes it and retries that symbol once. Prices need no crumb at all,
which matters for ingest but not here.

### The CSV contract

Sorted by `symbol`, one row per security, so the diff shows only real changes.

| column | source | notes |
|---|---|---|
| `symbol` | Wikipedia | normalised, `.` → `-` |
| `name` | Wikipedia | |
| `index_name` | Wikipedia | `sp500` / `sp400` / `sp600` |
| `mic` | Yahoo `exchange` | mapped: `NMS` → `XNAS`, `NYQ` → `XNYS` |
| `currency` | Yahoo | always `USD` here; the column exists because UK is planned |
| `cik` | SEC | zero-padded to 10 digits |
| `yf_sector` | Yahoo | the scoring taxonomy |
| `yf_industry` | Yahoo | stored, unused in v1 scoring |
| `gics_sector` | Wikipedia | cross-check only, never used for peer groups |

`gics_sector` is carried because the two taxonomies agree for only 92% of the S&P 1500 — Yahoo's
"Technology" absorbs payment processors GICS files under Financials. Keeping both in the file
makes a divergence visible without ever letting GICS reach a peer group.

**Unresolved names go to a second file.** 5 of 1,506 symbols failed to resolve in
reconnaissance, and share-class tickers such as `CWEN.A` → `CWEN-A` return 404. Writing
half-populated rows into the main CSV would either pollute every diff or load securities that can
never be scored, so they go to `data/universe-unresolved.csv` with the reason. The main file
stays complete by construction and `load-universe` needs no "is this row usable" branch.

### Throttling

Measured at 0.11s per request with no failures across 30 sequential calls, so a modest fixed
delay is a safeguard rather than a necessity. Sequential with a small delay, exponential backoff
on 429 or 401, and the run reports any symbol it gave up on. No concurrency: the one measurement
we have of concurrent access is that it failed badly, even if the cause was the library.

---

## 5. `load-universe`

Reads the CSV, reconciles against the database, applies everything **in one transaction**. A
half-applied universe would leave the next scoring run computing percentiles against a partly
built peer group — silent corruption rather than a visible failure.

**Resolution order:** CIK, then current symbol via `security_symbol`, then treat as new. Two CSV
rows resolving to one security is a hard error, never a merge.

| Case | Detection | Effect |
|---|---|---|
| New | not resolvable | insert `security` with `first_seen` = as-of; open `security_symbol` and `security_sector` rows |
| Departed | active in database, absent from CSV | `is_active = false`, `last_seen` = as-of |
| Reclassified | CSV industry ≠ current `security_sector` | close current row at `valid_to` = as-of, insert new at `valid_from` = as-of |
| Renamed | CIK matches, symbol differs | close current `security_symbol`, insert new, update `primary_symbol` |
| Re-entry | in CSV, in database, inactive | `is_active = true`, `last_seen = null` |

Closing at `valid_to` = as-of and opening at `valid_from` = as-of do not overlap under the
half-open `'[)'` bounds the exclusion constraints use — the behaviour proved by
`test_adjacent_symbol_periods_are_allowed`.

**A departure does not close the temporal rows.** Only `is_active` and `last_seen` change.
Setting `valid_to` on the symbol and sector rows would assert we know they stopped being true,
and we do not: the company still exists, still has that symbol, still has that sector. It left
the universe, not existence. The distinction matters the day it rejoins.

### Guards

**Departure ceiling.** If the CSV would retire more than 10% of the active universe, refuse and
exit non-zero unless `--force` is given. A truncated scrape returning 50 rows would otherwise
retire 1,450 securities in one committed transaction. This is the cheapest protection against the
failure most likely to actually happen.

**`--dry-run`** prints the plan — counts plus affected symbols — and touches nothing. With the
CSV diff that is two independent looks at a quarterly change before it lands.

**`--as-of`** defaults to today, so re-running an older CSV can be stamped with the date it
describes rather than the date it happened to run.

### Taxonomy and peer groups

`sector_scheme` gets one row, `yfinance`. `sector_node` gets both levels: 11 sectors at level 1,
~140 industries at level 2 with `parent_id` set to their sector. Codes are slugified
(`Financial Services` → `financial-services`); names are kept verbatim.

`peer_group` gets only what v1 scores: one market group at level 0 and 11 sector groups at level
1. Industry peer groups would be rows nothing references; creating them later is a trivial insert
from `sector_node`.

A security whose industry is missing but whose sector is present points at the sector node.

**Resolving a security to its peer group** is therefore a two-step walk, and worth stating
because classification and peer grouping sit at different levels: take the security's current
`security_sector` row, climb `parent_id` until reaching a level-1 node, then select the
`peer_group` whose `sector_node_id` is that node. A security already pointing at a level-1 node
skips the climb. When industry peer groups are added later the walk gains a first attempt at
level 2 and the rest is unchanged.

---

## 6. Failure behaviour

| Failure | Behaviour |
|---|---|
| Wikipedia layout change | `refresh-universe` exits non-zero, CSV untouched. Quarterly and manual; the daily job is unaffected. |
| Yahoo 401 mid-run | Refresh the crumb, retry that symbol once, then treat as unresolved. |
| Yahoo 429 | Exponential backoff, then unresolved. |
| SEC file unavailable | Warn and continue with null CIK. Matching degrades to symbol, which is the status quo. |
| CSV missing or malformed | `load-universe` fails before opening a transaction. |
| Ambiguous identity | Hard error; whole transaction rolls back. |
| Departures over ceiling | Refuse, exit non-zero. |
| Failure mid-load | Transaction rolls back; the universe is never half-applied. |

---

## 7. Testing

`load-universe` carries the weight, and it is testable without network because it reads a file.
The interesting cases are all *second run against changed input*: load fixture A, assert, load
fixture B, assert the transition.

- **Idempotence** — the same CSV twice reports zero changes on the second run and writes nothing.
  This catches most reconciler bugs on its own.
- **Each transition in isolation** — new, departed, reclassified, renamed, re-entry.
- **Rename preserves history** — same `security.id` before and after, two `security_symbol` rows,
  the old one closed. This is what D4 exists for, so it is the test that proves D4 works.
- **Reclassification adjacency** — `valid_to` on the closed row equals `valid_from` on the new
  one and the exclusion constraint accepts it.
- **Departure ceiling** — a truncated CSV refuses and leaves the database unchanged.
- **Transactional rollback** — a failure injected mid-load leaves nothing behind.
- **Ambiguous identity** — two CSV rows resolving to one security is rejected.

`refresh-universe` gets thinner tests against saved fixture payloads: the Wikipedia table parser,
the SEC ticker-to-CIK mapping, the `assetProfile` extraction, the exchange-to-MIC mapping, and
the split between the main and unresolved files. Network is supplied by `httpx.MockTransport`
through `fetch()`'s `transport` parameter, so no test reaches Yahoo. One test asserts the crumb
is fetched once and reused rather than per symbol.

---

## 8. Out of scope

Prices, fundamentals, and every other fact — that is ingest, and needs its own spec. This slice
populates identity and nothing else: no `fundamental_fact`, no `price_daily`, no scoring, no
`scoring_run`.

Choosing the universe is also not automated. `refresh-universe` reflects whatever the S&P 1500 is
on the day it runs; deciding to track something else is a change to this spec.

## 9. Open parameters

- **Refresh cadence** — quarterly matches index rebalancing, but nothing enforces it. The
  departure ceiling makes a stale run safe rather than destructive.
- **Departure ceiling percentage** — 10% is a guess sized to be far above real quarterly churn
  (typically well under 2% of the S&P 1500) and far below a truncated-file catastrophe. Tune once
  two or three real refreshes have been observed.
