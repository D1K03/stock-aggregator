-- A second way into this database, with almost nothing in it.
--
-- The application connects as `screener`, which on this deployment is the
-- cluster superuser. A SQL box on that connection is `pg_read_file` and
-- `COPY FROM PROGRAM`: arbitrary file read and remote code execution, reachable
-- by anyone holding a session cookie. The enforcement for the playground is
-- therefore this role and the list below, and not a check in Python. An
-- application allowlist is a pattern match over a language designed to be
-- written many ways; a role that was never granted select on `auth.session`
-- cannot read it however the query is spelled, including through a view, a CTE,
-- a function, or a cast nobody thought of.

-- The first plpgsql block in this directory, and it is here for one reason:
-- `create role` has no `if not exists` in Postgres 16, and a role is a *cluster*
-- object that outlives the `drop schema public cascade` the test suite performs
-- between tests. A bare `create role` would fail on the second test in a run.
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'playground') then
        -- No password, deliberately. A login role with a null password cannot
        -- authenticate under scram-sha-256, which is what the postgres image
        -- configures, so the playground ships switched off and is turned on by
        -- a deployment giving it one. The password is never in this file,
        -- because this file is in git.
        create role playground login password null;
    end if;
end
$$;

-- The floor under a connection that forgot to set its own. Every one of these
-- is a USERSET that a session could raise again, so none of them is the
-- enforcement; the grants below are.
alter role playground set statement_timeout                   = '10s';
alter role playground set idle_in_transaction_session_timeout = '30s';
alter role playground set lock_timeout                        = '2s';
alter role playground set default_transaction_read_only       = on;
-- Unqualified `event` must not resolve to something the reader did not name.
alter role playground set search_path                         = 'public';

-- Required rather than decorative: the test suite recreates `public` as the
-- superuser, which leaves it owner-only.
grant usage on schema public to playground;

-- Explicit, table by table. `alter default privileges` was considered and
-- rejected: under it a table becomes readable by being created rather than by
-- anyone deciding it should be, so the next migration would expose its tables
-- to a SQL box with no line in a diff saying so. Exposure should cost a line
-- here and a line in tests/test_playground.py.
--
-- Partitioned parents only. `price_daily_2026` and everything
-- `screener.partitions` creates at boot inherit no ACL, which is right twice
-- over: reading through the parent needs no child grant, and the catalogue then
-- shows `price_daily` once rather than once per year.
grant select on
    -- reference
    pillar, metric, sector_scheme, sector_node, data_source,
    -- identity
    security, security_symbol, security_sector, peer_group,
    -- scoring configuration
    weight_version, pillar_weight, scoring_logic_version, scoring_run,
    -- facts
    ingest_run, ingest_observation, fundamental_fact, price_daily,
    corporate_action,
    -- derived
    metric_daily, pillar_score_daily, snapshot_daily, event_flag_daily,
    peer_group_stat,
    -- alerting
    alert_rule, alert_event, alert_state,
    -- Reddit usernames and third-party comment bodies. Public data, and the one
    -- granted table with a person in it. Granted because the social data is
    -- most of what makes this page worth having, and not put behind a redacting
    -- view, which would drop `author` while leaving usernames quoted inside
    -- `body`.
    social_item,
    -- so "which version is this database on" is answerable here
    schema_migration
to playground;

-- Livestream transcripts, when they exist. Conditional because the schema is
-- created outside this repository and is not present on every deployment: it is
-- on a development machine today and not on the VPS, and an unconditional grant
-- would fail the migration there.
do $$
begin
    if exists (select 1 from pg_namespace where nspname = 'skybird') then
        grant usage on schema skybird to playground;
        grant select on all tables in schema skybird to playground;
    end if;
end
$$;

-- Not granted, and no `grant usage` on their schemas either, so they cannot even
-- be named:
--   auth.app_user  who may sign in
--   auth.session   token_hash is authentication material
--   audit.event    identities, conversation transcripts, and spend
-- There is no `revoke` for these, because nothing was ever granted.
