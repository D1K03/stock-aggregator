# Database schema design

Status: agreed, not implemented. Written 2026-09-04.

The snapshot shape and diffing logic the rest of the system hangs off. Standing decisions
summarised in `DESIGN.md`; this document holds the full table set and the reasoning that is
specific to the schema.

Target scale: 500 tickers growing to ~3,000, US-only initially with UK as a planned second
market. First fortnight runs against ~100 tickers to shake out bugs.

---

## 1. Constraints this schema has to satisfy

From `DESIGN.md`, restated as schema requirements:

1. **Every score traces to visible raw inputs.** From any percentile you must be able to reach
   the exact observation behind it, and from there the stored API response.
2. **A score can move because peers moved.** The peer distribution on a given day is itself
   data, not something recomputed on demand.
3. **The blended score is derivable** from pillar scores plus a weight set.
4. **Percentiles need peers.** Thin sectors fall back to a broader grouping, and which grouping
   was used has to be visible afterwards.
5. **Restatements must not corrupt history.** Fundamentals get revised; a revision must not
   rewrite what was known at the time.
6. **Scoring bugs must not poison the backtest.** Corrected scores land as a new version
   alongside the old, never over it.
7. **Cost ceiling ~£5–10/month.** Postgres holds structured data only; raw payloads live in
   Blob.

---

## 2. Decisions

**D1 — Narrow relational metric detail, not JSONB.** `metric_daily` is one row per
ticker/date/metric (~30M rows/yr at full universe) rather than a JSONB bundle per ticker/date
(~750k rows/yr). The deferred plans — weight backtesting and web-UI screening — are both
cross-sectional metric queries, which JSONB makes expensive. Row count was never the binding
constraint; storage was, and that was solved by moving payloads out of Postgres.

**D2 — Raw payloads live in Azure Blob, never in Postgres.** Azure Postgres Flexible Server
provisioned storage ratchets: it grows and cannot shrink, and deleted rows do not return space
without `VACUUM FULL`. A payload table peaking at 15 GB would permanently raise the floor even
after expiry. Blob Cool tier at roughly a penny per GB/month makes retention a lifecycle rule
rather than a job. AWS was considered and offers no advantage here — RDS has the same one-way
storage ratchet; only serverless-storage engines (Aurora, Neon) escape it, and once payloads
are in Blob, Postgres never approaches the limit anyway.

**D3 — "Raw metrics" and "raw payloads" are different things.** Parsed metric values are
canonical, immutable, and stay in Postgres forever (they are inside the 2–3 GB/yr figure).
Unparsed source JSON goes to Blob. The two are easy to conflate under "raw data" and the
retention rules differ completely.

**D4 — Content-hash deduplication, but always record the observation.** Most fundamentals
endpoints return full history on every call, so a daily pull rewrites the same statements ~250
times a year. Hash the response and skip the *blob write* when unchanged — but always insert
the `ingest_observation` row, or the record of what was known on a given date is lost. The hash
doubles as the restatement detector: a changed hash for a period already held means the
provider revised something.

**D5 — Bitemporal fundamentals.** Every fact carries `period_end` (what it describes) and
`observed_at` (when it was learned). A restatement is an insert, never an update. Scoring on
date D reads the latest observation with `observed_at <= D`, which makes the forward log
point-in-time correct by construction rather than by discipline.

**D6 — Raw prices only; no stored `adj_close`.** Providers retroactively rewrite adjusted
prices whenever a corporate action occurs, which would smuggle mutable history back in. Store
raw OHLCV immutably plus a `corporate_action` table, and compute adjustment at scoring time.
The alternative is a 2-for-1 split appearing as a −50% twelve-month return.

**D7 — Versioned scoring runs (recompute, versioned).** Pillar scores are derived and stored
per run; corrected scores land as a new run and old rows stay immutable. The decisive argument
is specific to the alerting design: a crossing is a property of an *adjacent pair* of scores,
so recomputing in place does not merely change today's score, it retroactively changes whether
a crossing ever happened. Alerts that fired become alerts that shouldn't have, with no record
of either — a corrupted history that looks fine.

**D8 — Comparability keys on an explicit logic version, not the git sha.** Deriving it from
git means an unrelated commit changes the key and silently suppresses a night of alerts.
`git_sha` and `config_hash` are stored for reproducibility; comparability uses a deliberately
bumped integer, enforced by a golden-file test that fails when fixture outputs change without a
bump.

**D9 — Missing metrics are dropped from the pillar average, not imputed.** Free-tier API
coverage is ragged and a percentile for an absent value is undefined. Imputing to the sector
median invents data; refusing to score loses tickers entirely. Dropping changes the pillar's
meaning silently, so `metric_count` and `coverage` are stored columns and alerting is gated on
a coverage floor.

**D10 — Temporal sector assignment.** Reclassification is rare but moves a ticker's entire peer
group, surfacing as an unexplained score jump. Same argument as D5.

---

## 3. Conventions

Applied throughout, per Postgres best practice:

- `bigint generated always as identity` primary keys; `smallint` for small reference tables.
- `text` (never `varchar(n)`), `timestamptz` (never `timestamp`), `numeric` (never float) for
  any value used in arithmetic.
- Every foreign key column is indexed explicitly — Postgres does not do it automatically.
- Composite indexes order equality columns before range columns.
- Enumerations are `text` with a `check` constraint rather than native enum types, so adding a
  value is a constraint change rather than a type migration.
- Migrations add constraints inside `do $$ ... $$` blocks, since Postgres has no
  `add constraint if not exists`.
- `btree_gist` is required (available on Azure Flexible Server) for exclusion constraints that
  combine scalar equality with a range overlap.

**Invariant: nothing holds a foreign key into a partitioned table.** Postgres 12+ permits an FK
referencing a partitioned table, but only against a unique constraint containing the partition
key — and none of `metric_daily`, `pillar_score_daily`, `snapshot_daily`, `event_flag_daily` or
`price_daily` has a surrogate id to point at. An attempt yields "there is no unique constraint
matching given keys" while a perfectly good primary key is visible, which is a confusing failure
to debug cold. If a future table genuinely needs the reference, it must use the full composite
key (e.g. `(as_of, scoring_run_id, security_id)`), not a single column.

Tables below are ordered for reading, not for execution: `peer_group_stat` and the derived
daily tables reference `scoring_run`, `metric` and `weight_version`, which are defined in
later sections. Creation order is reference data (`pillar`, `metric`, `sector_scheme`,
`data_source`) → `security` and its temporal tables → `weight_version` and `scoring_run` →
ingest and fact tables → derived daily tables → alerting.

---

## 4. Identity and reference

```sql
create table security (
  id             bigint generated always as identity primary key,
  name           text not null,
  mic            text not null,                          -- ISO 10383, 'XNAS' / 'XLON'
  currency       text not null check (length(currency) = 3),
  country        text not null check (length(country) = 2),
  cik            text,                                   -- SEC filer id; null outside the US
  figi           text,
  primary_symbol text not null,                          -- denormalised convenience only
  is_active      boolean not null default true,
  first_seen     date not null,
  last_seen      date,
  created_at     timestamptz not null default now()
);

create table security_symbol (
  id          bigint generated always as identity primary key,
  security_id bigint not null references security(id),
  symbol      text not null,
  mic         text not null,                             -- denormalised so the index below works
  valid_from  date not null,
  valid_to    date,                                      -- null = current
  source      text not null
);
create index security_symbol_security_id_idx on security_symbol (security_id);
create unique index security_symbol_current_uq
  on security_symbol (symbol, mic) where valid_to is null;
alter table security_symbol add constraint security_symbol_no_overlap
  exclude using gist (security_id with =,
                      daterange(valid_from, valid_to, '[)') with &&);
```

Symbols are attributes with history, never keys: FB became META, and retired symbols get
reissued to unrelated companies.

```sql
create table sector_scheme (
  id   smallint generated always as identity primary key,
  code text not null unique,                             -- 'yfinance', later 'gics'
  name text not null
);

create table sector_node (
  id        bigint generated always as identity primary key,
  scheme_id smallint not null references sector_scheme(id),
  parent_id bigint references sector_node(id),
  level     smallint not null check (level in (1, 2)),   -- 1 = sector, 2 = industry
  code      text not null,
  name      text not null,
  unique (scheme_id, code)
);
create index sector_node_scheme_id_idx on sector_node (scheme_id);
create index sector_node_parent_id_idx on sector_node (parent_id);

create table security_sector (
  id             bigint generated always as identity primary key,
  security_id    bigint not null references security(id),
  sector_node_id bigint not null references sector_node(id),
  valid_from     date not null,
  valid_to       date,
  source         text not null
);
create index security_sector_security_id_idx on security_sector (security_id);
create index security_sector_node_idx on security_sector (sector_node_id);
create unique index security_sector_current_uq
  on security_sector (security_id) where valid_to is null;
alter table security_sector add constraint security_sector_no_overlap
  exclude using gist (security_id with =,
                      daterange(valid_from, valid_to, '[)') with &&);
```

The taxonomy is a tree rather than a string because the peer-count fallback needs levels to
walk up. The partial unique indexes enforce "one current row"; the exclusion constraints enforce
"no two validity periods overlap", which a partial index cannot express.

```sql
create table peer_group (
  id             bigint generated always as identity primary key,
  scheme_id      smallint not null references sector_scheme(id),
  sector_node_id bigint references sector_node(id),      -- null = market-wide fallback
  level          smallint not null check (level in (0, 1, 2)),
  code           text not null,
  unique (scheme_id, code)
);
create index peer_group_sector_node_idx on peer_group (sector_node_id);
```

Resolution at scoring time walks industry (2) → sector (1) → market (0) until
`member_count >= min_peers`. The level actually used is recorded per metric per day, so a thin
peer group is visible rather than silent.

```sql
create table peer_group_stat (
  as_of          date not null,
  scoring_run_id bigint not null references scoring_run(id),
  peer_group_id  bigint not null references peer_group(id),
  metric_id      smallint not null references metric(id),
  member_count   int not null,
  deciles        numeric[] not null
                   check (array_length(deciles, 1) = 11),  -- p0, p10 … p90, p100
  primary key (as_of, scoring_run_id, peer_group_id, metric_id)
);
```

~1.5M rows/yr. This is what answers *"did the company move, or did the sector re-rate around
it?"* without recomputing anything.

Deciles rather than quartiles: three boundaries cannot resolve a move from p82 to p78 into
"the ticker fell" versus "the p75 boundary rose". Eleven numerics on a 1.5M-row table is cheap
now and awkward to backfill later. `deciles[1]` and `deciles[11]` are the min and max, so
separate columns for them would be redundant.

**This table is a cache, not a source of truth.** Every peer's `raw_value` and `percentile` for
the group and date is already in `metric_daily`, so the exact distribution is always
recoverable — the summary exists to avoid scanning a 30M-row table for the common question. It
has the same status as `blended_score`: recompute it and the same numbers must come back.

---

## 5. Ingest and the bitemporal fact layer

```sql
create table data_source (
  id   smallint generated always as identity primary key,
  code text not null unique,                             -- 'yfinance', 'finnhub', 'finra', ...
  name text not null
);

create table ingest_run (
  id                   bigint generated always as identity primary key,
  source_id            smallint not null references data_source(id),
  endpoint             text not null,
  started_at           timestamptz not null,
  finished_at          timestamptz,
  status               text not null
                         check (status in ('running','ok','partial','failed')),
  securities_requested int,
  securities_ok        int,
  error                text
);
create index ingest_run_source_id_idx on ingest_run (source_id);

create table ingest_observation (
  id             bigint generated always as identity primary key,
  ingest_run_id  bigint not null references ingest_run(id),
  security_id    bigint not null references security(id),
  fetched_at     timestamptz not null,
  content_hash   bytea not null,                         -- sha256 of canonicalised JSON
  blob_path      text not null,                          -- {source}/{endpoint}/{date}/{security_id}.json.gz
  is_new_payload boolean not null,                       -- false = hash matched, blob not rewritten
  payload_bytes  int
);
create index ingest_observation_run_idx on ingest_observation (ingest_run_id);
create index ingest_observation_security_idx
  on ingest_observation (security_id, fetched_at desc);
```

**Deliberately not partitioned**, despite being time-series and ~3M rows/yr. A partitioned
table's unique constraint must include the partition key, which would make its primary key
`(id, fetched_at)` — and then `fundamental_fact` could not hold a simple foreign key to it. The
traceability chain is worth more than partitioning a table that stays small for years. Revisit
if it approaches 100M rows.

The blob path includes source and endpoint because one security has a yfinance fundamentals
payload, a Finnhub news payload and a FINRA short-interest record on the same date.

`content_hash` is taken over **canonicalised JSON** — keys sorted, insignificant whitespace
removed — not raw bytes. A provider reordering its keys would otherwise present as a
restatement, which is precisely the signal the hash exists to carry. Canonical hashing serves
the blob-write skip equally well: if the canonical form is unchanged, the stored payload is
already equivalent.

```sql
create table pillar (
  id   smallint generated always as identity primary key,
  code text not null unique,
  name text not null
);

create table metric (
  id               smallint generated always as identity primary key,
  code             text not null unique,
  name             text not null,
  pillar_id        smallint not null references pillar(id),
  unit             text not null,
  higher_is_better boolean not null,
  cadence          text not null
                     check (cadence in ('daily','quarterly','event')),
  is_active        boolean not null default true
);
create index metric_pillar_id_idx on metric (pillar_id);
```

`higher_is_better` lives in the database because percentile direction is a property of the
metric, not of the code that ranks it — a low P/E is good, a low ROIC is not.

```sql
create table fundamental_fact (
  id                    bigint generated always as identity primary key,
  security_id           bigint not null references security(id),
  metric_id             smallint not null references metric(id),
  period_end            date not null,                   -- what it describes
  period_type           text not null check (period_type in ('Q','A','TTM')),
  value                 numeric not null,
  currency              text check (length(currency) = 3),
  observed_at           timestamptz not null,            -- when it was learned
  ingest_observation_id bigint not null references ingest_observation(id),
  restates_id           bigint references fundamental_fact(id),
  unique (security_id, metric_id, period_end, period_type, observed_at)
);
create index fundamental_fact_pit_idx
  on fundamental_fact (security_id, metric_id, period_end, observed_at desc);
create index fundamental_fact_obs_idx on fundamental_fact (ingest_observation_id);
```

Append-only; never updated. The point-in-time read for scoring date `as_of` is:

```sql
select distinct on (security_id, metric_id, period_end) *
from fundamental_fact
where security_id = $1
  and observed_at <= ($2::date + $3::interval)   -- as_of + run's cutoff offset
order by security_id, metric_id, period_end, observed_at desc;
```

`fundamental_fact_pit_idx` is exactly that access path. ~430k rows/yr including restatements.

**The cutoff must be an explicit interval, not a bare date.** Comparing `observed_at <= $2`
against a `date` casts it to midnight at the *start* of that day, silently excluding everything
learned during it — including the nightly fetch the score is supposed to be based on. See
"Observation cutoff" in section 7 for the rule.

```sql
create table price_daily (
  security_id bigint not null references security(id),
  trade_date  date not null,
  open        numeric not null,
  high        numeric not null,
  low         numeric not null,
  close       numeric not null,
  volume      bigint not null,
  observed_at timestamptz not null,
  ingest_observation_id bigint not null references ingest_observation(id),
  primary key (security_id, trade_date)
) partition by range (trade_date);
create index price_daily_obs_idx on price_daily (ingest_observation_id);

create table corporate_action (
  id                    bigint generated always as identity primary key,
  security_id           bigint not null references security(id),
  effective_date        date not null,
  action_type           text not null
                          check (action_type in ('split','dividend','spinoff')),
  ratio                 numeric,
  amount                numeric,
  currency              text check (length(currency) = 3),
  observed_at           timestamptz not null,
  ingest_observation_id bigint not null references ingest_observation(id)
);
create index corporate_action_security_idx on corporate_action (security_id, effective_date);
create index corporate_action_obs_idx on corporate_action (ingest_observation_id);
```

Yearly partitions on `price_daily` (~756k rows/yr). Momentum computes split and dividend
adjustment at scoring time from raw prices plus actions — see D6.

`price_daily` carries `ingest_observation_id` like every other fact table. Prices are the one
input D6 argues cannot be trusted to stay put, so being the only table without a route back to
the stored payload would be exactly backwards: a provider silently backfilling a wrong close is
the case where the payload matters most.

---

## 6. Derived daily layer

```sql
create table metric_daily (
  as_of               date not null,
  scoring_run_id      bigint not null references scoring_run(id),
  security_id         bigint not null references security(id),
  metric_id           smallint not null references metric(id),
  raw_value           numeric not null,                  -- the value that fed the score
  percentile          numeric not null check (percentile between 0 and 100),
  peer_group_id       bigint not null references peer_group(id),
  peer_count          int not null,
  fallback_level      smallint not null check (fallback_level in (0, 1, 2)),
  fundamental_fact_id bigint references fundamental_fact(id),  -- null for price-derived metrics
  period_end          date,
  primary key (as_of, scoring_run_id, security_id, metric_id)
) partition by range (as_of);

create index metric_daily_history_idx on metric_daily (security_id, metric_id, as_of desc);
create index metric_daily_run_idx on metric_daily (scoring_run_id);
create index metric_daily_fact_idx on metric_daily (fundamental_fact_id);
```

**No surrogate primary key.** Nothing references individual rows — `alert_event` carries its
own frozen copy — so the natural composite key serves as the primary key, saving an 8-byte
column and an entire index on the largest table in the system.

`fundamental_fact_id` makes the traceability constraint literal: percentile → observation →
`observed_at` → `ingest_observation` → the stored response it came from.

**Partitioned from day one, but yearly — not monthly.** Partitioning at all is non-negotiable:
retrofitting it onto an existing table is a full rewrite. The *granularity* is a different
question, and monthly is wrong at the start — 500 tickers is ~5M rows/yr, so monthly would mean
a hundred near-empty partitions long before the table justifies one.

Granularity is never "switched". Converting an existing yearly partition into twelve monthly
ones does move data (detach, create, `insert…select`, drop), but that is avoidable entirely: a
partitioned parent holds mixed granularity happily, so once the universe passes ~1,000 tickers,
start creating monthly partitions from the next year boundary and leave earlier years coarse
forever. Yearly and monthly boundaries align, so nothing has to be rebuilt.

Partitions are pre-created ahead of time by the daily job — roughly fifteen lines of SQL rather
than a `pg_partman` dependency, and it fails loudly at a time someone is watching. There is
deliberately **no default partition**: attaching a new partition alongside one requires a full
scan.

The cross-sectional screening index `(as_of, metric_id, percentile)` is **deferred until the
web UI exists**. A third index on this table is a real insert-time and storage cost for a query
nobody runs yet, and adding it later is a `create index`, not a data migration. Note that
`create index concurrently` is not supported directly on a partitioned parent — build
per-partition, then attach.

```sql
create table pillar_score_daily (
  as_of          date not null,
  scoring_run_id bigint not null references scoring_run(id),
  security_id    bigint not null references security(id),
  pillar_id      smallint not null references pillar(id),
  score          numeric not null,
  metric_count   smallint not null,                      -- metrics that actually contributed
  coverage       numeric not null check (coverage between 0 and 1),
  primary key (as_of, scoring_run_id, security_id, pillar_id)
) partition by range (as_of);
```

Narrow rather than five wide columns, because weights live in a table: the blend is
`sum(score * weight)` joined against `pillar_weight`. With pillars as columns the query would
have to name them, which fights the decision to keep weights out of code. ~3.8M rows/yr — five
*scored* pillars; event risk is a flag layer and never appears here.

```sql
create table snapshot_daily (
  as_of                date not null,
  scoring_run_id       bigint not null references scoring_run(id),
  security_id          bigint not null references security(id),
  blended_score        numeric not null,
  pillar_agreement     smallint not null,                -- pillars top-quartile simultaneously
  min_coverage         numeric not null,                 -- worst pillar coverage
  worst_fallback_level smallint not null,                -- thinnest peer group used
  primary key (as_of, scoring_run_id, security_id)
) partition by range (as_of);
```

`blended_score` is a **materialised derivation, not ground truth**. It is stored because the
nightly crossing diff would otherwise join and aggregate 3.8M rows, and it stays honest because
the run it belongs to carries the weight version: recompute it and the same number must come
back.

There is deliberately no `weight_version_id` column here. `scoring_run` already carries it, and
every snapshot row already joins to its run — duplicating it would create a second copy that
must agree with the first and eventually won't.

```sql
create table event_flag_daily (
  as_of          date not null,
  scoring_run_id bigint not null references scoring_run(id),
  security_id    bigint not null references security(id),
  flag_code      text not null,
  severity       smallint not null,
  detail         jsonb,
  primary key (as_of, scoring_run_id, security_id, flag_code)
) partition by range (as_of);
```

A table rather than a JSONB column on the snapshot, so *"everything with earnings inside seven
days"* is an index scan. Event risk is a flag layer, never a score.

---

## 7. Runs, weights, and comparability

```sql
create table weight_version (
  id         bigint generated always as identity primary key,
  code       text not null unique,                       -- 'v3-momentum-heavy'
  note       text,
  created_at timestamptz not null default now()
);

create table pillar_weight (
  weight_version_id bigint not null references weight_version(id),
  pillar_id         smallint not null references pillar(id),
  weight            numeric not null check (weight >= 0),
  primary key (weight_version_id, pillar_id)
);
create index pillar_weight_pillar_idx on pillar_weight (pillar_id);
```

Weights are stored raw and **normalised at read time** by dividing by the version's sum.
"Weights sum to 1" cannot be expressed cleanly as a table constraint and would eventually be
violated; normalising on use makes it impossible to lie.

```sql
create table scoring_logic_version (
  id            smallint generated always as identity primary key,
  description   text not null,                           -- 'added revision breadth to Momentum'
  introduced_at timestamptz not null default now()
);

create table scoring_run (
  id                bigint generated always as identity primary key,
  as_of_range       daterange not null,
  cutoff_offset     interval not null,                     -- see "Observation cutoff" below
  logic_version_id  smallint not null references scoring_logic_version(id),
  weight_version_id bigint not null references weight_version(id),
  status            text not null check (status in ('live','backfill','experiment')),
  emits_alerts      boolean not null,
  git_sha           text not null,
  config_hash       bytea not null,
  started_at        timestamptz not null,
  finished_at       timestamptz,
  outcome           text not null check (outcome in ('running','ok','failed')),
  supersedes_run_id bigint references scoring_run(id),
  note              text,
  exclude using gist (as_of_range with &&) where (status = 'live')
);
create index scoring_run_logic_idx on scoring_run (logic_version_id);
create index scoring_run_weight_idx on scoring_run (weight_version_id);
```

The exclusion constraint is the authoritative-run pointer done properly: **the database refuses
to hold two overlapping live runs**, so `max(run_id)` can never accidentally become the answer
and an experiment cannot leak into a production read. As written it needs no `btree_gist`, since
only a range participates — but adding UK as a second market means wanting one live run *per
market*, which adds a scalar equality column and therefore the extension.

### Observation cutoff

`cutoff_offset` is an interval added to each scoring date to bound which observations that date
may see:

```
facts visible when scoring date D  =  observed_at <= D + cutoff_offset
```

A live run scoring D at 02:00 the next morning needs an offset past that fetch — around
`'1 day 6 hours'`. The value is stamped on the run and covered by `config_hash`.

**It has to be an offset rather than a timestamp**, because a single cutoff cannot survive a
multi-day backfill: one timestamp applied across a range would let a 2024 scoring date see 2026
knowledge, which is precisely the lookahead bias D5 exists to prevent. As an offset, live and
backfill evaluate the identical expression and produce the identical fact set — which is what
makes a backtest of the scoring logic mean anything.

**What `observed_at` actually records** is when *we* learned a fact, not when it became public.
A 10-Q filed at 16:30 on D is typically first seen by the nightly pull hours later, so the log
lags publication by up to a day. That biases any backtest **pessimistic** — the system is never
credited with data it did not yet hold — which is the safe direction, and honest in a way that
inventing publication timestamps from filing metadata would not be.

### Comparability

Two scores are comparable when produced under the same `(logic_version_id, weight_version_id)`.
Run identity is the wrong key — each nightly execution is its own run, so run-scoping every
comparison would suppress everything.

**The crossing rule:** compare today's snapshot against the most recent prior snapshot sharing
the same `(logic_version_id, weight_version_id)`. Where none exists, emit nothing.

**Deploy sequence when either version changes:** bump → backfill the previous day with
`status = 'backfill'` and `emits_alerts = false` → resume live. Skipping the backfill costs one
silent night. Removing the guard entirely would fire the whole universe into the channel at
once, because a changed metric definition moves every score simultaneously.

Comparability is checked by joining `snapshot_daily` (756k rows/yr) to `scoring_run`, not on
`metric_daily`, so the join is cheap.

### Pruning experiments

An experiment's `metric_daily` rows are prunable once its pillar scores are recorded — the
experiment keeps its conclusions permanently and its working set temporarily. A plain `delete`
is fine here: the freed space returns to the table's free space map and the next day's inserts
reuse it. The Azure storage ratchet only bites when a table shrinks permanently, and this one
grows daily.

---

## 8. Alerting

```sql
create table alert_rule (
  id             bigint generated always as identity primary key,
  code           text not null unique,
  name           text not null,
  enabled        boolean not null default true,
  condition_type text not null
                   check (condition_type in ('score_crossing','pillar_flip',
                                             'revision_cluster','insider_buying',
                                             'valuation_band')),
  params         jsonb not null,                         -- threshold, direction, lookback
  cooldown_days  smallint not null,
  min_coverage   numeric not null                        -- the D9 data-quality gate
);

create table alert_event (
  id                     bigint generated always as identity primary key,
  alert_rule_id          bigint not null references alert_rule(id),
  security_id            bigint not null references security(id),
  as_of                  date not null,
  scoring_run_id         bigint not null references scoring_run(id),
  fired_at               timestamptz not null,
  blended_score          numeric not null,
  previous_blended_score numeric,                         -- null for non-crossing rule types
  pillar_scores          jsonb not null,                 -- {"valuation": 44, "quality": 91, ...}
  raw_inputs             jsonb not null,
  driver                 text not null,
  event_flags            jsonb,
  delivery_status        text not null
                           check (delivery_status in ('pending','sent','failed')),
  delivery_attempts      smallint not null default 0,
  delivered_at           timestamptz,
  delivery_error         text,
  unique (alert_rule_id, security_id, as_of)
);
create index alert_event_security_idx on alert_event (security_id, as_of desc);
create index alert_event_rule_idx on alert_event (alert_rule_id);
create index alert_event_run_idx on alert_event (scoring_run_id);
create index alert_event_pending_idx
  on alert_event (fired_at) where delivery_status <> 'sent';
```

The frozen payload is the **one place JSONB is unambiguously right**: an immutable document,
never queried across rows, answering *"why did this alert?"* in a single row read instead of a
join against the correct run version. A few thousand rows a year.

The unique constraint makes the daily job **idempotent**. Given the v1 shortcut of running
locally, re-running after a partial failure is routine, and without it a re-run double-posts.

It also means a backfill run covering an `as_of` that already has alerts would collide on
insert. That is the desired outcome — a backfill has `emits_alerts = false` and has no business
writing alerts — but the constraint is a backstop, not the mechanism: **the alerting step must
be skipped entirely when `emits_alerts` is false**, rather than attempted and left to fail on a
unique violation. Postgres cannot express this as a cross-table check, so it is an invariant the
code owns and a test should cover.
Order of operations is insert as `pending` → POST to Discord → mark `sent`, so a crash
mid-flight can at worst duplicate one message, never silently lose one. The partial index on
undelivered rows is the retry queue.

```sql
create table alert_state (
  alert_rule_id   bigint not null references alert_rule(id),
  security_id     bigint not null references security(id),
  last_fired_at   timestamptz not null,
  last_fired_as_of date not null,
  cooldown_until  date not null,
  last_direction  smallint not null check (last_direction in (-1, 1)),
  primary key (alert_rule_id, security_id)
);
create index alert_state_security_idx on alert_state (security_id);
```

`last_direction` exists because a cooldown that suppresses the *opposite* crossing is wrong: a
score crossing up, then genuinely collapsing back three days later, is exactly the event worth
hearing about. Cooldown suppresses repetition, not reversal.

**Deliberately absent: a `crossing` table.** Crossings — including those suppressed by
cooldown — are derivable from consecutive comparable rows in `snapshot_daily`. Persisting them
would duplicate state that can drift, and the backtest question worth asking later ("how often
does a score-80 crossing precede a good three-month return?") is a query over `snapshot_daily`
that correctly includes crossings which never fired.

---

## 9. Sizing at full universe (3,000 tickers, ~40 metrics)

| Table | Rows/yr | Note |
|---|---|---|
| `metric_daily` | ~30M | monthly partitions, per live run |
| `pillar_score_daily` | ~3.8M | yearly partitions |
| `ingest_observation` | ~3M | unpartitioned |
| `peer_group_stat` | ~1.5M | unpartitioned |
| `price_daily` | ~756k | yearly partitions |
| `snapshot_daily` | ~756k | yearly partitions |
| `fundamental_fact` | ~430k | includes restatements |
| `alert_event` | a few thousand | |

About 36M rows and 3–4 GB a year including indexes, which sits inside a cheap Azure Postgres
tier for years. Raw payloads are not in this figure — they are gzipped JSON in Blob at roughly
a penny per GB/month, with retention set by a storage-account lifecycle rule rather than a job.

---

## 10. Open items for implementation

These are parameters, not unresolved design:

- **Minimum peer count** for the fallback walk. Start at 20 and tune against the real universe;
  it is a config value stamped into `config_hash`, not a schema property.
- **Coverage floor** for alert suppression, per rule in `alert_rule.min_coverage`.
- **Universe composition** — 3,000 tickers must be distributed so sectors have workable peer
  counts. The first-fortnight ~100-ticker run will not exercise this, so thin-sector fallback
  needs a deliberate test rather than waiting for production to reveal it.
- **Partition pre-creation window** — two months ahead for `metric_daily`, one year for the
  yearly-partitioned tables.
