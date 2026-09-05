-- Social posts and comments, for the Sentiment pillar.
--
-- A separate table rather than `ingest_observation`, which cannot hold this:
-- its `security_id` is `not null` and references `security(id)`, and a Reddit
-- post mentions zero, one or many tickers. Making that column nullable would
-- weaken the traceability chain for every source that does have exactly one
-- security, to accommodate one that never does.
--
-- Nothing here references a security yet. Connecting an item to the tickers it
-- mentions is its own piece of work, and doing it at ingest would mean the
-- universe had to be loaded before a single post could be stored.

insert into data_source (code, name)
values ('arctic_shift', 'Arctic Shift (Reddit mirror)')
on conflict (code) do nothing;

-- Deliberately NOT partitioned, for the same reason `ingest_observation` is
-- not: a partitioned table's unique constraint must include the partition key,
-- and the upsert this table exists to support keys on `(source_id,
-- external_id)` alone. At roughly 7.6M rows a year it stays comfortable for
-- years; revisit it alongside `ingest_observation` rather than separately.
create table social_item (
    id           bigint generated always as identity primary key,
    source_id    smallint not null references data_source(id),
    kind         text not null check (kind in ('post', 'comment')),
    -- Reddit's own id, `t3_abc123` for a post and `t1_abc123` for a comment.
    -- Text rather than an integer: it is base-36 and prefixed, and the prefix
    -- is what distinguishes the two namespaces.
    external_id  text not null,
    subreddit    text not null,
    -- The post a comment belongs to. Left as the provider's string rather than
    -- a foreign key to `id`: a comment can arrive before the post it replies
    -- to, and a self-referencing not-null constraint would make ingest order
    -- load-bearing for no gain.
    parent_id    text,
    -- Null for a deleted account, which Reddit reports as "[deleted]" rather
    -- than omitting the field.
    author       text,
    -- When it was written, which is the axis a sentiment window slices on.
    created_utc  timestamptz not null,
    -- When we saw it. Both are needed: a score read six hours after a post was
    -- written is a different observation from the same score read a week on.
    fetched_at   timestamptz not null,
    score        int,
    title        text,
    body         text not null,
    permalink    text,
    -- sha256 over the fields that carry meaning, so a re-fetch that changed
    -- only the vote count is not recorded as an edit. Hashing per item rather
    -- than per response is what makes dedup work here at all: a whole listing
    -- changes on every fetch, exactly as `quoteSummary` does, but a comment
    -- body almost never does.
    content_hash bytea not null,
    unique (source_id, external_id)
);
create index social_item_source_id_idx on social_item (source_id);
-- The read a sentiment window will make: one subreddit over a date range.
create index social_item_subreddit_created_idx
    on social_item (subreddit, created_utc desc);
-- And the read ingest itself makes, to find where it left off.
create index social_item_created_idx on social_item (created_utc desc);
