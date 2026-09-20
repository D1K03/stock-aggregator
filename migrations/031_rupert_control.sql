-- Whether Rupert is allowed to run tonight.
--
-- **The database is the control plane**, which is the shape `screener.skybird`
-- already uses and for the same three reasons: there is no internal HTTP surface
-- between the dashboard and the resolver to authenticate, the state outlives the
-- process holding it, and a pause survives a deploy.
--
-- This is deliberately *not* `RUPERT_DAILY_MAX_CALLS`. That one is the deployment
-- switch — it lives in Infisical, it means "this container has no business
-- running at all", and turning it off needs the container recreated rather than
-- restarted. This one is the operator's switch: a button on a page, reversible in
-- a second, for "not tonight". Conflating them would mean pausing for an hour
-- required a redeploy, and unpausing required remembering what the number was.
--
-- One row, enforced by the primary key rather than by convention. `only_row` is
-- a constant column with a check on it: the table cannot hold a second row, so
-- no reader has to decide which one is current.
create table if not exists rupert.control (
    only_row     boolean primary key default true check (only_row),
    paused       boolean not null default false,
    -- Who pressed it and when. A pause nobody can attribute is one nobody will
    -- undo, because the first question is always "was that deliberate".
    changed_by   text,
    changed_at   timestamptz not null default now()
);

insert into rupert.control (only_row, paused) values (true, false)
on conflict (only_row) do nothing;

grant select on rupert.control to playground, playground_bot, playground_mcp;
