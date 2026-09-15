# Insider transactions, from SEC Form 4

*Status: implemented. Written 2026-09-15.*

Every Form 4 filed by a company in the universe, stored one row per transaction:
who dealt, in what, how many shares, at what price, and whether they are a
director, an officer or a ten percent holder. Twice a day, newest day first.

Ingest only. Nothing here scores anything, and the Insider/Institutional pillar
is a separate piece of work.

## The route, and why it is the government's own site

**EDGAR is the primary source, and everything else is downstream of it.** Form 4
data exists because SEC compels the filing, so a mirror parses the same XML this
does and can only add lag, parse bugs and a dependency. The alternatives were
checked rather than dismissed:

| Route | Why not |
|---|---|
| Finnhub `/stock/insider-transactions` | Free key, but the ToS is "strictly for personal use", forbids redistributing "derived results", and requires all data deleted when a subscription ends. A pillar score posted to Discord is a derived result shared with a third party, and an append-only fact layer cannot live under a delete-on-cancel clause |
| sec-api.io, Form4API | Subscription products |
| Apify Form 4 actors | Advertise $0.10 per alert. At ~490 transactions a day that is $50/day against a £5-10/month target. `CLAUDE.md` already rejects Apify for exactly this |
| secform4.com | `Crawl-delay: 10` for every agent, so one sweep of 1,504 companies is 4h10m against EDGAR's ~30s. HTML rather than a schema, and it is one person's Joomla install absorbing bandwidth for data SEC gives away. `/insider-trading-search` and `/site/` are disallowed outright, the latter covering their own terms-of-use page |

EDGAR needs no key, no session and no proxy. `sec.gov/robots.txt` does not
disallow `/Archives/`; `data.sec.gov` has no robots.txt at all. SEC publishes a
rate of 10 requests a second and requires a User-Agent naming a contact address,
answering 403 without one. That address is the only friction and there is
nothing else to configure.

Two of this project's free sources have already failed this year: StockTwits
went behind a Cloudflare managed challenge, and Arctic Shift is a volunteer
service `2026-09-05-reddit-ingest.md` calls a single point of failure. SEC is
legally obliged to publish this and has no paid tier to be pushed into.

## Three facts about the format, each measured

**1. A filing's XML filename is the filer agent's choice.** Seven Form 4
directories sampled on 2026-09-11 gave four patterns: `form4.xml`,
`ownership.xml`, `wk-form4_1789156901.xml`, `tm2624595-6_4seq1.xml`. Guessing
`form4.xml` 404s for six filings in seven, verified against accession
`0001104659-26-107065`. The daily index already names the complete submission
`.txt`, which carries the XML verbatim inside an `<XML>` block, so that is what
is fetched: one request, no filename guessing, no directory listing.

It is also not `primaryDocument` from `data.sec.gov/submissions/`, which is the
XSL-rendered HTML view rather than the data.

**2. EDGAR writes one index line per filer, not per filing.** A Form 4 names at
least two parties. On 2026-09-11, 921 Form 4 and 4/A rows were **435 filings**:

| rows per accession | accessions |
|---|---|
| 2 | 421 |
| 3 | 4 |
| 5 | 3 |
| 6 | 5 |
| 11 | 2 |

Without dedupe by accession, one filing is fetched eleven times.

**3. A missing daily index is 403, not 404.** So is a blocked User-Agent, and
`screener.fetch` collapses both into one `FetchError` string with no status left
to read, making a weekend indistinguishable from being throttled. The quarter's
`index.json` names exactly which `form.*.idx` exist: on QTR3 it listed 52, with
Saturday the 12th, Sunday the 13th, Labor Day and the current day all absent. A
day nobody asks for cannot be a refusal anybody has to interpret.

## Measured volume

Two real days, 2026-09-11 and 2026-09-14:

| | |
|---|---|
| Index rows for a whole market day | 3,784 |
| Of those, Form 4 and 4/A | 921 |
| Distinct filings | 435 |
| Filings touching our universe | 174 and 305 |
| Transactions stored | 975 across the two days |
| On disk, table and indexes | 689 bytes a row |
| Projected | ~490 rows a day, ~123,000 a year, **~85 MB a year** |
| Wall clock, one day | ~100s, almost all of it the deliberate 0.15s pause |

Thirty times smaller than the Reddit corpus, which is ~2.7 GB a year.

Transaction codes over those two days: `A` 354 (grants), `S` 337 (sales), `M`
132 (option exercises), `F` 50 (shares withheld for tax), `P` 48 (open-market
purchases), `G` 41 (gifts), then `C`, `J` and `D` in single figures. **`P` is
the code worth reading**: an insider spending their own money is a different
event from a vesting schedule firing, and a pillar that treats `A` and `P` alike
would mostly measure compensation policy.

## Decisions

**D1 — One row per transaction, never per (transaction x owner).** A Form 4 can
be filed jointly, and this is not rare: **108 of 975 transactions carry more
than one reporting owner**, 11% of the rows. The extreme case on 2026-09-11 was
accession `0000902664-26-003792`, where Workers United and nine of its regional
joint boards filed against Amalgamated Financial and carried exactly one
transaction of 79,649 shares between them.

The XML does not attribute that transaction to any one owner, because as a
matter of fact they made it together. A row per owner would turn one trade into
ten, and anything summing shares would read ten times the volume with every
figure still looking plausible. So owners ride on the transaction as `text[]`
and the four relationship flags are OR'd across them: in the common case that is
one owner and the flags are theirs, and in the other 11% it says "one of these
is a ten percent owner", which is what the filing says and no more.

**D2 — The key is `(source, accession, table_kind, transaction_seq)`.**
Accession is globally unique at SEC and immutable: an amendment is a new
accession carrying document type `4/A`, never a rewrite. `table_kind` separates
the derivative and non-derivative sections, which have independent orderings.

`transaction_seq` is 1-based document order and is the part that matters. The
obvious alternative, keying on the contents, looks equivalent and is not: two
grants on the same day at the same price under different plans are two real
transactions, and a content key merges them into one row that looks correct.
Document order is safe because the archived submission is the same bytes on
every fetch, which is a stronger guarantee than `social_item` has for a comment
that can genuinely be edited.

**D3 — `content_hash` covers what SEC filed and nothing this code decided.**
`security_id` and `fetched_at` are excluded. Hashing `security_id` would make a
`universe load` that re-pointed a CIK read as SEC restating a months-old trade.
Decimals are hashed as `str(Decimal(...))` built from the filed text, never
through a float. Verified: a re-walk of an archived day stores 0 and edits 0.

**D4 — The frontier is `ingest_run`, not a new table and not `max(filed_date)`.**
One row per day walked, `endpoint = 'form4/YYYY-MM-DD'`. A count over stored
rows cannot serve, because **a day on which nobody we hold filed produces zero
rows** and "walked it, found nothing" would be indistinguishable from "never
walked it" — so that day would be re-walked on every pass for ever.
`ingest_run` already has every column this needs and is already granted to all
three read-only roles, so "which days has EDGAR done" is answerable on
`/playground` with no extra grant. `magpie.attempt` is the precedent for a log
of tries being the control plane.

A day is settled when it has an `ok` row, or after four attempts — a day EDGAR
will not serve must stop consuming the whole per-pass budget for ever.
Widening `EDGAR_BACKFILL_DAYS` **is** the backfill and there is no subcommand,
unlike `screener.reddit`: the frontier is a set of days rather than a span
somebody has to queue.

**D5 — The universe filter runs against the index, and is loose on purpose.**
The issuer is always one of a filing's index lines, so 435 filings a day can be
cut to the ~174 we hold without opening any of them. But the index does not
label which line is the issuer, so the filter matches on *any* filer — meaning a
company we hold filing as a ten percent owner of one we do not comes back with
an issuer outside the universe. Measured: Corebridge Financial filing against
Carlyle Tactical Private Credit Fund, one of 437 transactions on 2026-09-11.
Those are dropped at `store.save`, because `security_id` is `not null` and a
transaction we cannot attribute is not evidence for anything. One wasted fetch
in 437 is the price of deciding from the index.

**D6 — A refusal ends the pass; it does not narrow and retry.** The opposite of
Arctic Shift's 422, and a different type for that reason. There a refusal means
the query was too big and halving the window is the fix. Here SEC's limiter has
blocked the address for about ten minutes and every further request extends it,
so the only useful response is to stop and let the next pass start clean.

**D7 — No check constraint on `transaction_code`.** The opposite of the call
`magpie.attempt.reason` makes, where the set is closed because this code owns
it. Here SEC owns it, and the list has grown before. The two days measured
already produced nine distinct codes including `C`, `J` and `D`. A constraint
would mean the first filing using a new letter fails its insert, loses the row,
and reads as a parser bug rather than as SEC adding a code.

**D8 — In `public`, not a schema of its own.** The opposite of `magpie` and
`skybird`, which both earned a schema on the grounds that nothing in them
references `security`. This does: it carries a not-null `security_id` and the
pillar that reads it will join `price_daily` and `snapshot_daily`.

**D9 — `EDGAR_CONTACT_EMAIL` is deliberately not `SEC_CONTACT_EMAIL`.** That one
already exists and drives the quarterly, hand-run universe refresh. Sharing it
would mean setting a contact address for a command run four times a year
silently starts a daily crawler against the same service. `__main__` logs a hint
when one is set and the other is not. Unset is how the container is switched
off; an address containing `github.com` is refused by SEC and is treated as a
typo rather than an off switch.

## What this does not settle

**Which share class a filing belongs to.** A dual-class company is one filer
with two listed securities, so a Form 4 names one CIK and we hold two rows. On
the committed universe this fires twice, for `UA`/`UAA` and `NWS`/`NWSA`. The
filing does say which class in `securityTitle`, but mapping that text onto a
share class is a guess with its own failure mode; `store.universe_ciks` picks
the first active row and logs it rather than inventing a resolution.

**Amendments are not linked to what they amend.** A `4/A` is stored as an
ordinary row with its own accession. Form 4's XML does not carry the original
accession, only a matching `periodOfReport`, so anything wanting "the current
view of what happened" has to reconcile them itself.

**Nothing is written to the blob store.** Like `screener.reddit`, this keeps
extracted fields rather than the payload, so `ingest_observation` is not
involved and the traceability chain for this source is the accession number,
which resolves to a permanent public URL.

**Turning this into a metric is a `scoring_logic_version` bump.** A new metric
entering an existing pillar moves that pillar for every ticker on the night it
lands, and the diff step reads that as a universe-wide set of crossings. The
onboarding procedure in `DESIGN.md` applies: bump the weight version, backfill
with alerting disabled, then resume live.

**Form 5 and Form 3 are not ingested.** Form 3 is an initial statement of
holdings and Form 4 supersedes it in practice; Form 5 is an annual catch-up for
transactions exempt from Form 4 reporting. Both are cheap to add — one entry in
`FORM_TYPES` and a widened check constraint — and neither is evidence of a
decision to trade in the way a Form 4 is.

**13F is still unbuilt**, and is the other half of the Insider/Institutional
pillar `DESIGN.md` describes. `data_source.code` is `sec_edgar_form4` rather
than `sec_edgar` so that it gets a row of its own.
