-- Magpie: scraped documents, and every attempt to get one.
--
-- Its own schema, on the three grounds `auth`, `audit` and `skybird` each give:
-- a document is not a fact and not a score; no scoring query joins to it, and
-- nothing here references `security` -- deliberately, see below; and an article
-- outlives the fetch that produced it on a different clock from a daily bar.
-- Keeping it out of `public` also means the test suite's `drop schema public
-- cascade` still says exactly what it means.
create schema if not exists magpie;

-- One row per canonical URL: the latest reading of a page, not a history of
-- readings. The history is `attempt`, and the bytes of each one are in the blob
-- store.
create table if not exists magpie.document (
    id            bigint generated always as identity primary key,

    -- The identity, after tracking parameters and the fragment are stripped.
    -- Keying on the raw URL would make one article three documents when it
    -- arrives from an email, a tweet and a search result -- which is
    -- `screener.universe`'s reason for matching on CIK rather than symbol.
    url           text not null unique,

    -- What the publisher calls this page, from `<link rel="canonical">`. Kept
    -- beside `url` rather than replacing it so "what did we ask for" and "what
    -- does the publisher call it" stay separate questions. Not taken from the
    -- response: `fetch` returns the requested address, not the redirected one.
    canonical_url text,

    host          text not null,
    title         text not null,
    author        text,
    published     date,
    language      text,
    text          text not null,
    word_count    int not null check (word_count >= 0),

    -- sha256 over the headline and the body, and nothing else.
    --
    -- Deliberately not the response. `screener.reddit` recorded this from
    -- Yahoo: hashing a whole payload meant the hash changed every night and the
    -- dedup it existed for never fired once. An article page carries ad slots,
    -- nonces, view counters and a render timestamp, every one of which moves on
    -- each fetch. The headline and the body do not.
    content_hash  bytea not null,

    -- The gzipped page as fetched. Evidence rather than cache, on
    -- `screener.blobs`' terms: nothing prunes. When an extraction looks wrong,
    -- the only way to tell whether the site changed or the parser did is to
    -- have the bytes that were read.
    blob_path     text not null,

    strategy      text not null,
    status_code   int,
    requested_by  text,
    first_seen_at timestamptz not null default now(),
    fetched_at    timestamptz not null default now()

    -- No `security_id`, and that is the point. Tying a document to a ticker is
    -- entity resolution, which is its own problem; a nullable column here would
    -- invite a half-done join and quietly start being read as a fact.
);

create index if not exists document_host_idx on magpie.document (host, fetched_at desc);
create index if not exists document_fetched_idx on magpie.document (fetched_at desc);
create index if not exists document_hash_idx on magpie.document (content_hash);

-- One row per try.
--
-- Separate from `document` for the reason `ingest_run` is separate from
-- `social_item`, and `stream_session` from `transcript_segment`: a robots
-- refusal, a paywall, a page that was not HTML and a fetch where every strategy
-- failed all have to be recordable, and not one of them produces a document.
-- Without this table a dead link and a link nobody has tried look identical,
-- and the same dead link gets paid for again every time somebody asks.
--
-- It is also the control plane the outward crawl will poll, which is why the
-- state column and its partial index exist before anything polls them. That is
-- `skybird.stream_session`'s shape, and building it now means the crawler is a
-- new process rather than a second migration over live data.
create table if not exists magpie.attempt (
    id            bigint generated always as identity primary key,
    url           text not null,          -- as it was given to us
    canonical_url text not null,          -- magpie.urls.canonical(), the identity
    host          text not null,
    requested_by  text,
    requested_at  timestamptz not null default now(),
    finished_at   timestamptz,

    -- A closed set this code owns, so a check constraint rather than a table --
    -- the opposite of `skybird.platform`, where the whole point was that the
    -- list grows. 'stored' changed the document, 'unchanged' found it identical.
    state         text not null default 'requested'
                  check (state in ('requested', 'running', 'stored', 'unchanged',
                                   'refused', 'failed')),

    -- Why, when it is not 'stored' or 'unchanged'. Each of these is a place the
    -- ladder deliberately stops rather than escalating.
    reason        text check (reason in ('robots', 'paywall', 'not_a_page',
                                         'too_short', 'all_strategies_failed',
                                         'unlocker_capped')),

    strategy      text,                   -- the rung that answered
    attempts      text[],                 -- every rung tried, in order
    status_code   int,
    payload_bytes int,

    -- The only per-request price anywhere in this project.
    --
    -- Recorded here as well as on the audit row, and the difference matters:
    -- the audit trail is the ledger and `/audit` is where the money is read,
    -- but this is the *meter*, and the meter must not depend on the audit
    -- schema being readable by the process doing the metering.
    cost_usd      numeric(10, 6) not null default 0,

    -- Declared inline rather than by a later `alter table`, so re-applying
    -- this file against a database that kept the schema does not fail on a
    -- constraint that is already there.
    document_id   bigint references magpie.document (id) on delete set null,
    error         text
);

create index if not exists attempt_canonical_idx on magpie.attempt (canonical_url, requested_at desc);
create index if not exists attempt_host_idx on magpie.attempt (host, requested_at desc);

-- The daily meter's only query. Partial, because billed fetches are a small
-- slice of a table that will mostly be free ones.
create index if not exists attempt_unlocker_idx on magpie.attempt (requested_at desc)
    where strategy = 'unlocker';

-- The crawler's claim, pre-built. The live states are a small slice of a table
-- that will mostly be history -- `skybird.stream_session_live_idx`, exactly.
create index if not exists attempt_live_idx on magpie.attempt (state)
    where state in ('requested', 'running');

-- Readable by all three roles, article text included -- and that is a departure
-- from skybird, so it is worth saying why rather than leaving it to look like
-- an oversight.
--
-- 016 and 017 deny Steven a transcript because of what that data *is*: speech
-- captured by us, from people who did not publish it to us, and 018 says out
-- loud that granting it to the connector means transcripts leave the box. The
-- denial defends a property of the material, not a distrust of the reader.
--
-- A scraped article inverts every one of those. It was already published, it is
-- already readable by whoever pasted the link, and it was fetched precisely so
-- that it could be read. There is no asymmetry here to defend, so denying it
-- would be copying the shape of 017 without its reason.
--
-- The same sentence 018 wrote about transcripts applies here and is worth
-- repeating: granting `playground_mcp` means article text leaves the box to
-- claude.ai. It is public text that claude.ai could fetch for itself, which is
-- cheaper to say plainly than to defend a distinction nobody believes.
--
-- Table by table, never `all tables in schema`, on 013's terms: exposure costs
-- a line here and a line in tests/test_playground.py.
grant usage on schema magpie to playground, playground_bot, playground_mcp;
grant select on magpie.document, magpie.attempt
    to playground, playground_bot, playground_mcp;
