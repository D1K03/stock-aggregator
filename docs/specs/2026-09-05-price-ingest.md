# Price ingest

Status: agreed, not implemented. Written 2026-09-05.

Daily ingest of raw daily bars and corporate actions from Yahoo into `price_daily`,
`corporate_action` and the `ingest_observation` trail, plus the payload store the schema has
always assumed and never had. Standing decisions live in `DESIGN.md`; the schema is
`docs/specs/2026-09-04-database-schema-design.md`; the universe that supplies securities is
`docs/specs/2026-09-05-universe-and-identity.md`.

Scope: prices only. Fundamentals are cycle two and are named here only where a decision has to
anticipate them.

---

## 1. What this has to satisfy

1. **Momentum needs at least twelve months of trailing prices.** Migration 008 already
   pre-creates yearly partitions back to 2020 for exactly this reason, so the first run writes
   history, not just today.
2. **Raw prices only, never `adj_close`.** Schema D6 stores raw OHLCV plus a separate
   `corporate_action` table and computes adjustment at scoring time. A stored adjusted price is
   mutable history in disguise.
3. **Every score traces back to the stored response.** Schema D1 and `ingest_observation.blob_path`
   being `not null` make that a claim the database enforces, so the payload store cannot be
   something a cron job empties.
4. **A night is ~1,500 requests and must not need supervision.** It has to be re-runnable,
   self-healing after a failure, and honest about partial success.
5. **One HTTP client.** `pyproject.toml` declares `httpx` the only network client, and
   `psycopg` the only database driver. Adding a third runtime dependency needs an argument.

---

## 2. Decisions

D-numbers below belong to this spec. References to the schema spec's own decisions are
written `schema D<n>`.

**D1 — Prices only in this cycle.** `/v8/finance/chart` needs no crumb, and every metric in the
Momentum pillar that does not depend on analyst revisions — 3/6/12-month relative return,
distance from the 52-week high — is computable from bars alone. This exercises the whole spine
end to end (fetch → hash → blob → observation → fact → metric → percentile → pillar → snapshot →
UI) while avoiding the hardest part of the fact layer: `price_daily` has no `period_end`, no
`period_type` and no restatement chain. Fundamentals slot in afterwards without restructuring
anything.

**D2 — Backfill to 2020-01-01.** Deeper than the twelve months Momentum strictly needs, because
prices are the one thing in this system that can be legitimately backfilled: `DESIGN.md`'s
warning that history cannot be reconstructed is about *fundamentals* being restated. A
momentum-only score can therefore be recomputed over history and compared against forward
returns, which a fundamentals-based one never can.

The honest caveat, which any such backtest must state: `security_sector` is bitemporal but we
hold only today's classification, so historical percentiles would group by today's sectors. That
is a mild lookahead and it is not fixable from any free source.

**D3 — Two windows, not one.**

| | what it controls | value |
|---|---|---|
| **fetch window** | how much is requested from Yahoo | derived per security, floor 7 days |
| **settling window** | how far back an upsert is *permitted* | 7 calendar days, fixed |

They must not be the same number. After an outage the fetch window widens to close the gap, but
the permission to overwrite must not widen with it, or catching up silently rewrites bars that
had already settled.

Seven calendar days rather than five trading days so that no market calendar is needed; it
covers a normal trading week across a weekend.

**D4 — The fetch window is derived per security, from the data.** One query up front:

```sql
select security_id, max(trade_date) from price_daily group by security_id
```

Then `start = max(held + 1 day, today − settling window)`, and **no rows at all → 2020-01-01**.

> **Erratum (2026-09-05, added during the branch review).** That formula should read
> `min`, not `max`, and the code implements `min`. With `max`, a security held to
> yesterday would start at *today*: the settling window would never be re-requested,
> and D5's upsert — the one path permitted to absorb Yahoo's revisions to recent
> sessions — would have nothing to work on. The window has to reach back to the
> *earlier* of "where we stopped" and "the settling cutoff". The decision itself is
> unchanged; only the formula was written the wrong way round.

Deriving it from the last successful `ingest_run` instead was the original design and is wrong at
the edges: failures are per ticker but a run's status is per run, so a `partial` night leaves
some securities behind and a run-level window never returns for them. Three properties fall out
of the per-security form:

- a security that failed three nights running gets a three-day-wider window automatically;
- `partial` becomes a reporting status rather than a correctness one;
- a security added by the quarterly universe refresh has no rows, so it backfills to 2020 on the
  next ordinary night with no special case.

**D5 — Bars inside the settling window are upserted; bars outside it are insert-if-absent.**
Yahoo revises the most recent session — volume in particular, as consolidated tape arrives — so
"raw bars are immutable" describes this schema and not the source. The seven-day settling
window is honest about that and makes re-running a failed night idempotent. Beyond it,
`price_daily` is append-only.

The upsert is **the only place in the entire ingest path that mutates an existing row.** It lives
in one named function, not in an `on conflict` clause folded into a bulk insert, and it logs
every changed value with security, date, field, old and new. That log is the only witness these
changes have: the sweep in D8 compares against what is already stored, so by the time it runs an
in-window change has already been absorbed and leaves no mismatch to find.

**D6 — `price_daily` keeps its snapshot key for now.** Its primary key is
`(security_id, trade_date)` with a single `observed_at` — a snapshot, not a bitemporal table.
If Yahoo turns out to correct settled bars routinely, it needs `observed_at` in the key and a
schema-D5-style point-in-time read like `fundamental_fact` has.

That is a decision to make with evidence rather than by guesswork, and the migration risk is
lower than it looks: the rows that would need rewriting are backfilled prices, which can simply
be re-derived, and the forward log that genuinely cannot be recovered will not have accumulated
anything yet. D8 produces the evidence.

**D7 — Corporate actions are insert-if-absent on `(security_id, effective_date, action_type)`,
and a differing value is logged, never written.** The `events` block returns every split and
dividend *inside the requested window*, and `corporate_action` has no unique constraint — only a
non-unique index on `(security_id, effective_date)`. Without this rule a dividend three days old
is re-inserted on each of the next several nights, and duplicated dividends corrupt precisely the
adjustment schema D6 computes at scoring time.

A unique constraint would also stop the duplicates, but it would permanently block a provider
revising a dividend amount — the same mistake as making `price_daily` bitemporal by guesswork.
Same discipline as D5 and D6: detect, log, decide later. No migration is required.

**D8 — A monthly sweep, detect-only.** `range=6y` against every security, comparing the response
to what is stored, writing **nothing** — no rows, no blobs, no `ingest_observation`. It is a
diagnostic, not an observation of record.

It logs each mismatch as security, date, **field**, old and new, and finishes with a count by
field. The field matters more than the count: "volume was revised" and "close was revised" have
completely different implications for a momentum score. If the answer is a handful a year, all
volume, D6 stands. If closes are corrected regularly, `price_daily` needs the bitemporal key and
you want to know that before any backtest means anything.

**D9 — Payloads go to an object store, not a directory on the VPS.** `blob_path` is `not null`
and schema D1 claims every score traces back to the stored response; a pruning job that can
delete the file behind a live `blob_path` makes that a promise the database cannot keep.

The apparent saving of keeping blobs local is also illusory. The VPS has no backups —
`docs/infrastructure.md` calls it the largest outstanding gap — and fixing that needs off-box
storage, a credential in Infisical and an upload step. Once that exists, blobs are a path prefix
in the same bucket. Keeping them local defers work that is already owed and adds a later
migration of `blob_path` values.

Cloudflare R2: 10 GB storage, 1M writes and 10M reads a month on the free tier, no egress fee.
Prices are ~1 GB/year and ~45k writes a month.

**D10 — Hand-rolled SigV4 rather than `boto3`.** `boto3` pulls botocore, jmespath, s3transfer,
urllib3 and python-dateutil, and botocore's endpoint data is large in a container image — five
packages and a third HTTP stack for two verbs. The narrow case here is what makes signing
tractable by hand: two verbs, one bucket, static credentials, no session tokens, no assume-role,
no presigning, no multipart. AWS publishes official SigV4 test vectors, so this is implemented
against a spec with a conformance suite rather than against a blog post. Same reasoning that kept
`screener.secrets` on stdlib `urllib`.

**D11 — A blob write failure aborts the run.** `ingest_observation` asserts that `blob_path`
exists, so the row must not be written when the object does not. An R2 error is almost certainly
systemic — credentials or an outage — so continuing would mean ~1,500 doomed securities. Same
reasoning as `CrumbUnavailable` being fatal in the universe client. A failed night is a re-run;
D4 makes the re-run close its own gap.

---

## 3. Conventions this follows

- Each piece is its own package with a small public surface through `__init__.py`; nothing
  outside imports a submodule directly.
- `screener.ingest` owns its configuration; credentials do not live in `screener.config`.
- Migrations are not needed. Every table this writes to already exists.
- The Yahoo client is **not** promoted to a shared package in this cycle. `YahooClient` in
  `screener.universe.sources.yahoo` carries crumb and cookie state that `/v8/finance/chart` does
  not use, so promoting it now would share the one part price ingest does not need. The genuinely
  shared machinery is `LanePool`, already shared properly through `screener.fetch`. The ~20-line
  "request on a lane, park on 429, retry" loop is duplicated once, deliberately. Cycle two adds a
  second crumbed caller and is the moment that promotion earns itself.

---

## 4. Layout

```
screener.blobs           put(path, data) / get(path); local and s3 implementations
screener.ingest
  __main__.py            `prices` and `sweep`
  window.py              per-security window derivation; the settling-window rule
  chart.py               the Yahoo chart client, over a LanePool
  parse.py               chart JSON → bars + corporate actions
  prices.py              the run: the loop, the transaction, the one mutating function
  sweep.py               detect-only comparison and its count-by-field summary
  run.py                 ingest_run lifecycle
```

### `screener.blobs`

```python
put(path: str, data: bytes) -> None
get(path: str) -> bytes
```

The implementation is chosen by configuration: `local` (a directory — what tests use) and `s3`
(R2). Path shape is fixed by the schema spec:
`{source}/{endpoint}/{date}/{security_id}.json.gz`.

Three notes for the SigV4 implementation, each a known way to lose an afternoon:

- **Three different hashes are in play and they are not interchangeable.** `content_hash` is
  schema D4's sha256 of the **uncompressed** response. The blob is the **gzipped** bytes.
  `x-amz-content-sha256` is a third thing, and this implementation sends `UNSIGNED-PAYLOAD` for
  it — permitted by R2 over HTTPS and it avoids hashing the object a second time. Passing the
  schema D4 hash there would be a signature error that presents as a credentials problem.
  Comment it so nobody later "optimises" it.
- **Do not write general percent-encoding.** Canonical URI encoding is where hand-rolled
  implementations break. Every path this produces is `{source}/{endpoint}/{date}/{id}.json.gz`
  — safe characters only, no spaces, no unicode. `put()` asserts that rather than implementing
  encoding rules it cannot fully test.
- **Clock skew returns 403, not a clear error.** One line of comment, because on a VPS with
  drifting NTP this is the failure that reads as bad credentials.

R2 wants `auto` as the region in the credential scope. Its API surface is not all of S3's —
irrelevant for PUT and GET, but listing has slightly different paging semantics if that is ever
wanted.

---

## 5. `python -m screener.ingest prices`

One command that works out what it needs. Backfill is not a mode; it is what the ordinary path
does when a security has no rows. Cron and a human run the same code, and the awkward case — an
outage — exercises the same path as a normal night rather than a branch that only runs when
something has already gone wrong.

Per security, in symbol order:

```
GET /v8/finance/chart/{symbol}?period1=…&period2=…&interval=1d&events=div,split
  ↓  non-200 → count as failed, continue to the next security
sha256(response)          →  content_hash        (schema D4, uncompressed)
gzip(response) → blobs.put(…)                    raises → the run aborts (D11)
  ↓
BEGIN
  insert ingest_observation                      first: it is the FK target of everything below
  insert bars outside the settling window        bulk, on conflict do nothing (not a mutation)
  upsert bars inside the settling window         the one mutating function, logging changes
  insert corporate actions                       insert-if-absent, logging differences (D7)
COMMIT
```

`period1`/`period2` are epoch seconds rather than `range=`, because the window is computed per
security and `range` only offers fixed buckets.

**The observation row and both fact writes are one transaction.** A run that inserts bars, dies,
and leaves the split un-inserted produces an unadjusted series across a 2-for-1 split — a −50%
twelve-month return that looks like real data rather than like a failure. This is the single
most important invariant in the command and it has its own test.

### Throttling

Sequential, and no artificial delay by default: 1,506 requests at a measured 0.055s each is about
83 seconds, 3,012 sequential requests produced no 429 at all, and `LanePool` already spreads
across four exits and parks one that rate-limits. A `--delay` flag is the safeguard `PLAN.md`
asks for and the value used is recorded on the run.

This is the one number taken from a measurement made on a development machine rather than the
VPS. If the VPS probe ever comes back badly, `--delay` is the knob and nothing else changes.

### `ingest_run`

Opened `running` with `source_id`, `endpoint='chart'`, `started_at` and `securities_requested`;
closed with `finished_at`, `securities_ok` and `ok` / `partial` / `failed`. Because D4 derives
windows from `price_daily` rather than from this table, `partial` is informational and the next
run heals it.

---

## 6. `python -m screener.ingest sweep`

D8, detect-only. Writes nothing, anywhere. Fetches `range=6y` per security, compares each bar
against `price_daily`, logs `security, date, field, old, new` per mismatch and a count by field
at the end. Intended to be run monthly by hand while the question it answers is still open.

---

## 7. Failure behaviour

| failure | behaviour |
|---|---|
| non-200 for one security | counted, logged, run continues; D4 closes the gap next night |
| transport error for one security | as above |
| `blobs.put` fails | **run aborts** (D11) — systemic, not per-object |
| database error mid-security | that security's transaction rolls back whole; run continues |
| every security fails | run closes `failed`, `securities_ok = 0` |
| run killed mid-flight | `ingest_run` left `running`; next run's windows unaffected, since D4 reads `price_daily` |

A `running` row left behind by a kill is deliberately not cleaned up automatically: it is the
only evidence a run died, and D4 means it costs nothing.

---

## 8. Testing

The suite runs against a real Postgres 16, and network is mocked with `httpx.MockTransport` as
`screener.fetch` and the universe sources already do. `screener.blobs` uses the `local`
implementation throughout.

Load-bearing claims, demonstrated rather than asserted:

- **The observation and both fact writes are atomic.** Inject a failure between the bar insert
  and the corporate-action insert; assert no bars survive. This is the −50%-momentum test.
- **A bar inside the settling window is upserted and the change is logged.**
- **A bar outside the settling window is not modified**, even when the response differs.
- **The window derivation is per security**: a security with no rows backfills to 2020; one held
  to yesterday fetches the settling window; one three days stale fetches three days plus the
  window.
- **A new security added to the universe backfills** on an ordinary run, with no flag.
- **A corporate action already held is not duplicated**, and a differing amount is logged, not
  written.
- **A failed `blobs.put` aborts the run** and writes no `ingest_observation`.
- **One security's non-200 does not end the run**, and the next run's window for it is wider.
- **Re-running the same night is idempotent** — bar count and values unchanged.
- **SigV4 signing matches the published AWS test vectors.** No network.
- **The sweep writes nothing**: run it against a database with deliberately wrong bars and assert
  row counts, `ingest_observation` and blob calls are all unchanged.

---

## 9. Out of scope

- Fundamentals, `fundamental_fact`, and the crumbed `quoteSummary` path. Cycle two.
- Scoring: `metric_daily`, `pillar_score_daily`, `snapshot_daily`. Its own cycle, next.
- Replacing `screener.concept` in the UI and the bot chart tool. Depends on scoring.
- The nightly `pg_dump` to R2. This cycle creates the bucket and the credential it needs, and
  `docs/infrastructure.md` calls its absence the largest outstanding gap, but backups are their
  own change.
- Scheduling. Nothing in the repo runs this on a timer yet; cron and the deploy story are
  separate.

---

## 10. Open parameters

- **Settling window: 7 calendar days.** A guess, generous on purpose. Yahoo appears to revise
  the most recent session, so the real figure is probably one or two days. The upsert log in D5
  is what would justify shortening it.
- **Sweep cadence: monthly.** Arbitrary until it has produced a result twice.
- **`--delay`: 0 by default.** Rests on a measurement taken off the VPS, which remains the one
  outstanding piece of evidence in the whole fetch story.
- **Whether `price_daily` becomes bitemporal.** D6, decided by D8's output.
- **Whether `corporate_action` gains a unique constraint.** D7, decided the same way.
