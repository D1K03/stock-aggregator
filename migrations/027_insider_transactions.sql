-- SEC Form 4: what the people inside a company did with its own stock.
--
-- The Insider/Institutional pillar has been seeded since 019 with nothing
-- behind it, and `docs/specs/2026-09-06-fundamentals-ingest.md` names why:
-- "Form 4 / 13F, which has no adapter". This is the Form 4 half of that.
--
-- **The source is the EDGAR daily index and the complete submission it names.**
-- Not `.../{accession}/form4.xml`, which is what one filer agent in seven
-- happens to call its XML -- the filer's agent names the file, and `form4.xml`,
-- `ownership.xml`, `wk-form4_1789156901.xml` and `tm2624595-6_4seq1.xml` were
-- four of seven sampled on 2026-09-11. Fetching the `.txt` the index already
-- names is one request, and nothing has to be guessed.
--
-- In `public` rather than a schema of its own, which is the opposite of the
-- call `magpie` and `skybird` made. Both earned a schema on the grounds that
-- nothing in them references `security`. This does: it is a fact about a
-- security we already hold, it carries a not-null `security_id`, and the pillar
-- that reads it will join `price_daily` and `snapshot_daily`. Putting it
-- elsewhere would make that join cross a schema for no reason.
--
-- Ingest only, exactly as `screener.reddit` stores comments and
-- `screener.magpie` stores articles without either becoming a number. Wiring
-- this into a score needs a weight-version bump and a backfill with alerting
-- disabled, per DESIGN.md -- not a table.

insert into data_source (code, name)
values ('sec_edgar_form4', 'SEC EDGAR (Form 4 insider transactions)')
on conflict (code) do nothing;

-- One row per transaction. Not per filing, and -- the one that matters --
-- **not per (transaction x reporting owner).**
--
-- A Form 4 can be filed jointly. On 2026-09-11 accession 0000902664-26-003792
-- listed ten reporting owners against Amalgamated Financial -- Workers United
-- and nine of its regional joint boards -- and carried exactly ONE transaction
-- between them. The XML does not attribute that transaction to any one of them,
-- because as a matter of fact they made it together. A row per owner would turn
-- one trade into ten, and anything summing shares would read ten times the
-- volume with every figure still looking entirely plausible.
--
-- So the owners ride on the transaction as arrays and the four relationship
-- flags are OR'd across them. In the 421-of-435 case that is one owner and the
-- flags are his; in the other fourteen it says "one of these is a ten percent
-- owner", which is what the filing says and no more.
create table insider_transaction (
    id             bigint generated always as identity primary key,
    source_id      smallint not null references data_source(id),

    -- Not nullable, which decides what happens to a filing we cannot attribute.
    -- EDGAR writes one index line per *filer* and the issuer is always one of
    -- them, so `screener.edgar` filters the index against the universe's CIKs
    -- before opening anything. That filter is deliberately loose: it matches on
    -- any filer, and the filers are the issuer plus every reporting owner, so a
    -- company we hold filing as a ten percent owner of one we do not brings back
    -- a filing whose issuer is outside the universe. On 2026-09-11 that was
    -- Corebridge Financial, which we hold, filing against Carlyle Tactical
    -- Private Credit Fund, which we do not: one of 437 transactions that day.
    -- Those are dropped rather than stored unlinked, because a transaction we
    -- cannot attribute to a security we score is not evidence for anything.
    --
    -- Written once, at insert, and deliberately absent from the `do update set`
    -- in `store.save`: this link is our resolution of a CIK, not something SEC
    -- restated. Hashing it instead would make a universe reload read as SEC
    -- rewriting a trade that has not changed since the day it was filed.
    security_id    bigint not null references security(id),

    -- `0001610717-26-000414`. Globally unique at SEC and immutable: an
    -- amendment is a new accession carrying document type '4/A', never a
    -- rewrite of this one. That immutability is what makes the key below safe.
    accession_number text not null,
    document_type    text not null check (document_type in ('4', '4/A')),

    -- A Form 4 has two transaction sections with independent orderings.
    -- Derivative rows are options and RSUs and carry a strike, an expiry and an
    -- underlying; non-derivative rows are the shares themselves.
    table_kind       text not null
                       check (table_kind in ('non_derivative', 'derivative')),

    -- Position within that section, 1-based, in document order.
    --
    -- **This column is the reason the key works.** Keying on the contents
    -- instead -- date, code, shares, price -- looks equivalent and is not: two
    -- grants on the same day at the same price under different plans are two
    -- real transactions, and a content key silently merges them into one row
    -- that looks correct. Document order is stable because the document is
    -- immutable: the archived submission is the same bytes on every fetch,
    -- which is a stronger guarantee than `screener.reddit` has for a comment.
    transaction_seq  smallint not null check (transaction_seq > 0),

    -- Zero-padded to ten digits on the way in. The index publishes the CIK
    -- unpadded and the XML publishes it padded, so normalising once here is
    -- what lets the join against `security.cik` be a string comparison.
    issuer_cik       text not null,
    issuer_name      text not null,
    issuer_symbol    text,

    -- The date the trade is reported *for*, and the date the paperwork arrived.
    -- Both are needed and they are routinely days apart -- a trade on the 9th
    -- filed on the 11th is the ordinary case -- so a pillar windowing on the
    -- wrong one would read a two-day-old trade as today's news.
    period_of_report date not null,
    filed_date       date not null,

    -- One entry each per reporting owner, in document order, so the two arrays
    -- line up positionally. Arrays rather than a child table, for the joint
    -- filing reason above.
    owner_ciks       text[] not null,
    owner_names      text[] not null,
    -- Only the non-empty ones. SEC writes an empty element for a filer who is
    -- not an officer, and storing '' as though it were a job title would put it
    -- in a group-by.
    officer_titles   text[] not null default '{}',

    -- OR'd across every reporting owner on the filing. These are what separate
    -- a routine RSU vest from a chief executive buying on the open market,
    -- which is most of why this data is worth having at all.
    is_director          boolean not null,
    is_officer           boolean not null,
    is_ten_percent_owner boolean not null,
    is_other             boolean not null,

    security_title   text,

    -- Not null, and the parser drops a transaction missing either rather than
    -- storing one. A transaction with no date cannot be put in a window and one
    -- with no code cannot be told from a gift; neither is evidence of anything,
    -- which is the call `screener.reddit` already makes for a comment with no
    -- body.
    transaction_date date not null,

    -- **No check constraint, deliberately, and this is the opposite of the call
    -- `magpie.attempt.reason` makes.** There the set is closed because this
    -- code owns it. Here SEC owns it -- P, S, A, M, F, G, D, C, X and a dozen
    -- more, and the list has grown before. A constraint would mean the first
    -- filing using a new letter fails its insert, loses the row, and reads as a
    -- parser bug rather than as SEC adding a code.
    transaction_code text not null,

    -- 'A' acquired, 'D' disposed. Null passes, which is deliberate: it was
    -- present on every transaction sampled, and a single filing omitting it
    -- should land as an incomplete row rather than fail the whole day.
    acquired_disposed text check (acquired_disposed in ('A', 'D')),

    -- numeric, never float. A price that changed in its seventh digit because
    -- it went through a float is exactly the quiet wrongness this project
    -- spends its comments avoiding, and these are hashed as their own decimal
    -- text for the same reason.
    shares             numeric,
    price_per_share    numeric,
    shares_owned_after numeric,

    -- 'D' held directly, 'I' indirectly -- through a trust, a fund, or a family
    -- member. A pillar that treats the two alike would read a family trust's
    -- rebalancing as an officer's conviction.
    direct_or_indirect text check (direct_or_indirect in ('D', 'I')),

    -- Derivative rows only; null on every non-derivative one.
    conversion_or_exercise_price numeric,
    expiration_date              date,
    underlying_title             text,
    underlying_shares            numeric,

    -- When we read it. The filing itself never changes -- the archive is
    -- immutable -- so unlike `social_item.fetched_at` this is provenance rather
    -- than a clock on a value that moves.
    fetched_at   timestamptz not null,

    -- sha256 over every field above that was read out of SEC's XML, and over
    -- nothing computed here: `security_id` and `fetched_at` are excluded. So a
    -- re-walk of an archived day writes nothing at all. Per row rather than per
    -- response, for the reason `screener.reddit` hashes per item.
    content_hash bytea not null,

    unique (source_id, accession_number, table_kind, transaction_seq)
);

-- The read the Insider pillar will make: one security over a date range.
create index insider_transaction_security_date_idx
    on insider_transaction (security_id, transaction_date desc);
-- The read the dashboard will make: what landed recently, across everything.
create index insider_transaction_filed_idx
    on insider_transaction (filed_date desc);
-- "Show me that filing", from a row or from an accession somebody pasted. The
-- unique index above leads with `source_id` and cannot serve this.
create index insider_transaction_accession_idx
    on insider_transaction (accession_number);

-- Readable by all three roles, names and job titles included -- and the
-- argument is 022's rather than 016's, only stronger.
--
-- 016 and 017 deny Steven a transcript because of what that data *is*: speech
-- captured by us, from people who did not publish it to us. A Form 4 inverts
-- every part of that. It was published by law, by the filer's own company, to a
-- government agency, onto a public archive with no authentication in front of
-- it. The name and the officer's title are not incidental to the filing -- they
-- are the disclosure, and the reason the form exists.
--
-- The sentence 018 wrote about transcripts and 022 repeated about articles
-- applies here too and is cheaper said plainly than argued around: granting
-- `playground_mcp` means these names leave the box to claude.ai. They are
-- public filings that claude.ai could fetch for itself.
--
-- Table by table, never `all tables in schema`, on 013's terms: exposure costs
-- a line here, a line in tests/test_playground.py and a line in tests/test_mcp.py.
grant select on insider_transaction to playground, playground_bot, playground_mcp;
