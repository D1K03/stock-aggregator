-- The span a walk did not finish, so the next pass can come back for it.
--
-- `screener.reddit` resumed from two scalars: `max(created_utc)` and
-- `min(created_utc)` over what was stored. Two scalars describe an interval,
-- and an interrupted walk does not leave an interval -- it leaves a hole in the
-- middle of one. The walk runs backwards from now, so a span that dies halfway
-- has banked everything newer than the point it died at; `max` then jumps to
-- the present and nothing ever looks between the two again.
--
-- Measured on 2026-09-14 before this existed: `stocks/comment` finished
-- 'partial' on 31 of 50 runs, and 36 of the previous 168 hours held no comments
-- at all while the mirror still held real ones for every hour checked. The
-- older-end span already in `_walk` cannot close those: it is `(target,
-- oldest)`, which repairs an unfinished backfill and by construction never
-- looks inside what has already been walked.
--
-- One row per un-walked span, deleted when a later pass covers it. A table
-- rather than a column on `ingest_run` because the question asked of it is
-- "what is still outstanding" rather than "how did that run go", and because a
-- gap outlives the run that discovered it.
create table social_gap (
    id           bigint generated always as identity primary key,
    source_id    smallint not null references data_source(id),
    subreddit    text not null,
    kind         text not null check (kind in ('post', 'comment')),
    -- Half open, `[span_after, span_before)`, matching the arguments
    -- `source.items` takes so that a row is a call.
    span_after   timestamptz not null,
    span_before  timestamptz not null,
    -- Why it is here: 'interrupted' for a walk that died, 'requested' for a
    -- span an operator queued by hand. The repair path and the ordinary one are
    -- the same drain, and this is what tells them apart afterwards.
    reason       text not null default 'interrupted'
                   check (reason in ('interrupted', 'requested')),
    discovered_at timestamptz not null default now(),
    -- How many passes have tried and failed to drain it. A gap over a span the
    -- mirror will not answer at any width would otherwise be retried forever,
    -- at the front of every pass, ahead of fresh comments.
    attempts     int not null default 0,
    check (span_before > span_after),
    -- Re-recording the same remainder should update the row rather than grow a
    -- second one: a span that fails at the same place twice is one gap.
    unique (source_id, subreddit, kind, span_after, span_before)
);

create index social_gap_stream_idx
    on social_gap (source_id, subreddit, kind, span_before desc);

-- The read-only roles see it for the same reason they see `ingest_run`: a hole
-- in the sentiment data is something the dashboard and the connector should be
-- able to ask about, and it is the only place that records one.
grant select on social_gap to playground;
grant select on social_gap to playground_bot;
grant select on social_gap to playground_mcp;

-- A word for "most of it worked", which the trail did not have.
--
-- `audit.event` offered 'ok', 'refused' and 'error', and a reddit pass that
-- lost three hours of one subreddit is none of the three: it is not an error,
-- because the other three streams are stored and the process is healthy, and
-- calling it 'ok' is how the hole stayed invisible. `ingest_run` has had
-- 'partial' since 005; this gives the trail the same vocabulary.
--
-- Worth writing down rather than reaching for 'error': `record` never raises,
-- so an outcome outside this list is not a failed insert that somebody notices
-- but a row that silently never arrives.
alter table audit.event drop constraint if exists event_outcome_check;

alter table audit.event
    add constraint event_outcome_check
    check (outcome in ('ok', 'partial', 'refused', 'error'));
