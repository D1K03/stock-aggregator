-- What the platform did, and what it cost.
--
-- Its own schema, for the reason `auth` has one: nothing here is a fact or a
-- score, no scoring query joins to it, and an audit trail outlives the data it
-- describes. Keeping it out of `public` also means the test suite's
-- `drop schema public cascade` still says exactly what it means.
create schema if not exists audit;

-- One row per thing the platform did on someone's behalf.
--
-- `kind` is the coarse filter the interface offers and `operation` is the
-- specific thing within it, so an agent reply is ('agent', 'steven.reply') and
-- a slash command is ('command', 'ping'). Two columns rather than one dotted
-- string because the interface filters on the first and displays the second,
-- and splitting a string in a where clause is how an index stops being used.
create table audit.event (
    id           bigint generated always as identity primary key,
    occurred_at  timestamptz not null default now(),
    kind         text not null check (kind in ('agent', 'command', 'tool', 'system')),
    operation    text not null,
    -- Who asked. A Discord user id, a GitHub login, or 'system' for anything
    -- nobody triggered. Stored as text because the two identity spaces do not
    -- share a type and joining them to one table would invent a relationship
    -- that does not exist.
    actor        text not null default 'system',
    actor_kind   text not null default 'system'
                 check (actor_kind in ('discord', 'github', 'system')),
    outcome      text not null default 'ok'
                 check (outcome in ('ok', 'refused', 'error')),
    -- Spend, recorded per event rather than summed later. OpenRouter reports
    -- the real charge on each response, so this is what was actually billed
    -- and not a reconstruction from a price table that drifts.
    model        text,
    prompt_tokens     integer not null default 0,
    completion_tokens integer not null default 0,
    cost_usd     numeric(12, 8) not null default 0,
    duration_ms  integer,
    -- Anything the row above cannot hold: the tool arguments, the refusal
    -- reason, the first line of an error. Deliberately unstructured, because
    -- an audit trail that needed a migration per new field would stop being
    -- written to.
    detail       jsonb not null default '{}'::jsonb
);

-- The interface pages newest-first and filters by kind, so the index carries
-- both in that order.
create index event_occurred_idx on audit.event (occurred_at desc);
create index event_kind_occurred_idx on audit.event (kind, occurred_at desc);
-- The spend totals at the top of the page sum over a time window.
create index event_cost_idx on audit.event (occurred_at) where cost_usd > 0;
