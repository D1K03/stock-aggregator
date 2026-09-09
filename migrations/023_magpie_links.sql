-- The sites a document points at.
--
-- Read from the page already in the blob store rather than fetched, so having
-- these costs no request: `magpie.document.blob_path` names the gzipped page,
-- and the links are a parse of bytes that were kept the first time.
--
-- The links are the article's own, not the site's. Every anchor on a Wikipedia
-- page includes "Donate", "Privacy Policy" and the interwiki language list;
-- the ones taken here come from the same extracted body the text does, with
-- the navigation and the footer already removed, so what lands is the citations.

create table if not exists magpie.link (
    id          bigint generated always as identity primary key,

    -- Cascade, unlike `magpie.attempt`, which deliberately survives its
    -- document. An attempt is a record that money was spent and a site was
    -- asked; a link is a reading of a page, and once the page is gone the
    -- reading is not evidence of anything.
    document_id bigint not null references magpie.document (id) on delete cascade,

    -- Canonicalised on the way in, so one source cited from an article twice
    -- with different tracking parameters is one row with a count of two.
    url         text not null,
    host        text not null,

    -- The link's own words. The cheapest honest answer to "what is this": the
    -- article already described it, and a description written by whoever cited
    -- it beats anything a fetch would return.
    anchor      text,

    occurrences int not null default 1 check (occurrences > 0),

    -- The document this link produced, once somebody followed it.
    --
    -- **This column is the frontier.** `where scraped_id is null` is exactly the
    -- queue a crawler would drain, which is why it exists before anything
    -- drains it: the crawl cycle then arrives as a new process rather than a
    -- migration over live rows, the same argument `magpie.attempt`'s state
    -- column already makes.
    --
    -- Set null rather than cascade: deleting the document a link produced
    -- should leave the link, unfollowed, not delete the reference to it.
    scraped_id  bigint references magpie.document (id) on delete set null,

    found_at    timestamptz not null default now(),

    -- One row per source per document. Re-reading a page updates counts rather
    -- than doubling them.
    unique (document_id, url)
);

-- The page's own read: every link of a document, grouped by site.
create index if not exists link_document_idx on magpie.link (document_id, host);
-- Which documents cite a given site, across everything gathered.
create index if not exists link_host_idx on magpie.link (host);
-- The frontier. Partial, because once a crawl exists most links will have been
-- followed and the unfollowed ones are the small slice worth indexing.
create index if not exists link_frontier_idx on magpie.link (document_id)
    where scraped_id is null;

-- Null means the stored page has never been read for links.
--
-- The flag rather than "no rows": an article that genuinely cites nothing and
-- one nobody has opened are different states, and without this they look the
-- same and the blob gets re-read on every page view.
alter table magpie.document
    add column if not exists links_read_at timestamptz;

-- Readable wherever the documents are, on 022's terms: a link is a fact about
-- an article that was already published, and the same three roles that may read
-- the article may read what it points at.
grant select on magpie.link to playground, playground_bot, playground_mcp;
