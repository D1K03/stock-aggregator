-- Two read-only roles, differing in one schema.
--
-- `013_playground.sql` created one role for both callers of `screener.playground`
-- -- the dashboard's SQL console and Steven's `sql` tool -- on the grounds that
-- one engine means the two cannot allow different things. That is still the
-- right instinct about the *bounds*: the timeouts, the row and cell caps, the
-- single-statement rule and the named cursor are the engine's, and both callers
-- still get all of them identically.
--
-- It is the wrong instinct about the *tables*, and 016 is where that showed.
-- Ellis wants live transcripts queryable on /playground. Steven is deliberately
-- built to work skybird's controls and be unable to read one back. Those are
-- both reasonable and they are not the same permission, so one role cannot hold
-- them both: 016 had to deny the schema to the console in order to deny it to
-- the bot.
--
-- So the console keeps `playground` and gets skybird back. Steven gets
-- `playground_bot`, which is `playground` minus that schema.
--
-- **The split is a credential, not a branch.** Neither role is chosen in
-- Python. Both processes read the same `PLAYGROUND_DATABASE_URL`; compose gives
-- the api container a URL for one role and the bot container a URL for the
-- other, and each holds only its own password. A bug in the bot cannot reach
-- the console's role, because that credential is not in that process -- which
-- is the difference between an enforcement and a check.

-- `create role` has no `if not exists`, and a role outlives the
-- `drop schema public cascade` the test suite performs between tests, exactly
-- as 013 explains for the first one.
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'playground_bot') then
        -- No password, deliberately, on the same terms as `playground`: a login
        -- role with a null password cannot authenticate under scram-sha-256, so
        -- Steven's `sql` tool ships switched off and a deployment turns it on.
        create role playground_bot login password null;
    end if;

    -- The floor under this role: a statement timeout, a lock timeout, a
    -- read-only default and a fixed search_path. None of them is the
    -- enforcement -- every one is a USERSET a session could raise again, and
    -- the grants are what actually hold -- but they keep an accident cheap.
    --
    -- Behind an existence check because `pg_db_role_setting` rows for a role
    -- with no `in database` clause are *shared* across the whole cluster, so
    -- writing them again from a second database is a write to a shared
    -- catalog and two doing it at once fail with "tuple concurrently updated".
    -- An advisory lock does not help: those are per-database, which was worth
    -- measuring rather than assuming. Not writing twice does.
    if not exists (
        select 1 from pg_db_role_setting
         where setrole = 'playground_bot'::regrole and setdatabase = 0
    ) then
        execute 'alter role playground_bot set statement_timeout                   = ''10s''';
        execute 'alter role playground_bot set idle_in_transaction_session_timeout = ''30s''';
        execute 'alter role playground_bot set lock_timeout                        = ''2s''';
        execute 'alter role playground_bot set default_transaction_read_only       = on';
        execute 'alter role playground_bot set search_path                         = ''public''';
    end if;
end
$$;

-- The same floor 013 puts under `playground`. Every one of these is a USERSET a
-- session could raise again, so none of them is the enforcement; the grants are.

grant usage on schema public to playground_bot;

-- The same table list 013 grants `playground`, and nothing added to it. Kept
-- table-by-table rather than `grant ... in schema public` for the reason 013
-- gives: exposure should cost a line here and a line in
-- `tests/test_playground.py`, so the next migration's tables are argued about
-- rather than inherited.
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
to playground_bot;

-- And the schema that is the whole point of the second role. Steven gets
-- nothing here -- no usage, so no spelling of a query reaches it -- while the
-- console gets it back, which is what 016 took away and why this migration
-- exists rather than a revert of that one.
--
-- `grant usage` on the schema and `select` on its tables, not
-- `alter default privileges`: a skybird table added later should have to appear
-- in a migration before it appears in a SQL console, which is the same rule 013
-- sets for `public`.
grant usage on schema skybird to playground;
grant select on skybird.platform, skybird.stream_session,
                skybird.transcript_segment to playground;
