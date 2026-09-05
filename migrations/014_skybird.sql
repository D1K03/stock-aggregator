-- Live stream capture: what was said on a stream, and when.
--
-- Its own schema, for the reason `auth` and `audit` have one: nothing here is a
-- fact or a score, no scoring query joins to it, and a transcript outlives the
-- stream it came from. Keeping it out of `public` also means the test suite's
-- `drop schema public cascade` still says exactly what it means.
create schema if not exists skybird;

-- Where a stream came from.
--
-- A table rather than `text` + `check`, which departs from the schema doc's
-- rule on enumerations, deliberately. The states below are a closed set this
-- code owns and follow the rule. Platforms are the opposite: the whole point is
-- that the list grows, and a growing list behind a check constraint is a
-- migration every time somebody adds an adapter.
create table skybird.platform (
    code         text primary key,
    display_name text not null,
    added_at     timestamptz not null default now()
);

insert into skybird.platform (code, display_name)
    values ('youtube', 'YouTube'), ('twitch', 'Twitch');

-- One row per capture, and also the control plane.
--
-- The dashboard writes 'requested' and the supervisor in the skybird container
-- polls for it, so no internal HTTP surface has to be invented or
-- authenticated, and the state is durable: a session left 'running' by a
-- container that died is visible on the next boot and gets reconciled, rather
-- than disappearing with the process that held it.
create table skybird.stream_session (
    id             bigint generated always as identity primary key,
    platform       text not null references skybird.platform(code),
    -- The video id, or the channel name for a platform that streams from a
    -- channel rather than a numbered broadcast. Whatever the adapter can match
    -- again tomorrow.
    external_id    text not null,
    channel        text,
    title          text,
    source_url     text not null,
    -- Built in Python from the platform adapter and SKYBIRD_EMBED_PARENTS, and
    -- handed to the browser ready to use. Twitch refuses to play unless
    -- `parent` names the host serving the page, and that is configuration --
    -- which the web container deliberately holds none of.
    embed_url      text,
    state          text not null default 'requested'
                   check (state in ('requested', 'starting', 'running',
                                    'stopping', 'stopped', 'failed')),
    stop_reason    text,
    requested_by   text not null,
    requested_at   timestamptz not null default now(),
    started_at     timestamptz,
    stopped_at     timestamptz,
    chunk_seconds  smallint not null check (chunk_seconds > 0),
    -- Counted rather than inferred. A stream that is quietly failing every
    -- chunk looks identical to one nobody is talking on, unless the interface
    -- can show the difference.
    chunks_ok      integer not null default 0 check (chunks_ok >= 0),
    chunks_failed  integer not null default 0 check (chunks_failed >= 0),
    chunks_dropped integer not null default 0 check (chunks_dropped >= 0),
    last_error     text
);
create index stream_session_platform_idx on skybird.stream_session (platform);
create index stream_session_requested_idx on skybird.stream_session (requested_at desc);
-- The supervisor polls every couple of seconds and only ever wants the live
-- states, which are a small slice of a table that mostly holds history.
create index stream_session_live_idx on skybird.stream_session (state)
    where state in ('requested', 'starting', 'running', 'stopping');
-- One live capture per stream. Pasting the same URL twice is the ordinary
-- mistake, and two ffmpegs on one manifest is fetched twice, transcribed twice
-- and stored twice.
create unique index stream_session_live_uq
    on skybird.stream_session (platform, external_id)
    where state in ('requested', 'starting', 'running', 'stopping');

-- The transcript. One row per utterance the model separated, not one per audio
-- chunk, so a mention keeps the second it was said at rather than the fifteen
-- second bucket it landed in.
--
-- Deliberately NOT partitioned, unlike the derived daily tables. Retention here
-- is "delete the session", which cascades, rather than a date-range drop; and
-- the only read that matters is one session in sequence order, which
-- partitioning by time would scatter across partitions instead of keeping
-- adjacent.
create table skybird.transcript_segment (
    session_id       bigint not null references skybird.stream_session(id) on delete cascade,
    -- Monotonic within the session and assigned by the supervisor, because it
    -- is what the dashboard polls on: "everything after 412".
    seq              integer not null,
    chunk_seq        integer not null,
    -- Both, and they answer different questions. The offset is from the start
    -- of the session and survives a reconnect; the wall clock is the honest
    -- anchor for a live event, and is what a later query -- what was being said
    -- while the stock moved -- actually joins on.
    captured_at      timestamptz not null,
    offset_seconds   numeric not null check (offset_seconds >= 0),
    duration_seconds numeric not null check (duration_seconds >= 0),
    text             text not null,
    -- No surrogate id: the natural key is the read pattern, and this is the
    -- table that grows.
    primary key (session_id, seq)
);
create index transcript_segment_time_idx on skybird.transcript_segment (session_id, captured_at);
