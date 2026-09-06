-- A third read-only role, and somewhere to keep the credentials that reach it.
--
-- 013 made one role for the dashboard's SQL console. 017 made a second for
-- Steven, differing in one schema, and set out the rule this migration follows:
-- **the split is a credential, not a branch.** Neither role is chosen in Python.
-- Every caller reads the same `PLAYGROUND_DATABASE_URL` and compose hands each
-- container a different one, so a bug in one process cannot reach another's
-- tables, because that password is not in that process.
--
-- The third caller is claude.ai, over MCP. It is different from the first two in
-- a way that matters: they run inside this deployment and it does not. What it
-- reads leaves the box. So it gets a role of its own rather than borrowing
-- either existing one, and the grant below is a decision to be argued with
-- rather than a list inherited from whichever role was copied.

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'playground_mcp') then
        -- No password, on the same terms as the other two: a login role with a
        -- null password cannot authenticate under scram-sha-256, so the
        -- connector ships switched off and a deployment turns it on by giving
        -- it one. The password is never in this file, because this file is in
        -- git.
        create role playground_mcp login password null;
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
         where setrole = 'playground_mcp'::regrole and setdatabase = 0
    ) then
        execute 'alter role playground_mcp set statement_timeout                   = ''10s''';
        execute 'alter role playground_mcp set idle_in_transaction_session_timeout = ''30s''';
        execute 'alter role playground_mcp set lock_timeout                        = ''2s''';
        execute 'alter role playground_mcp set default_transaction_read_only       = on';
        execute 'alter role playground_mcp set search_path                         = ''public''';
    end if;
end
$$;

-- The same floor 013 and 017 put under the other two. Every one of these is a
-- USERSET a session could raise again, so none of them is the enforcement; the
-- grants are.

grant usage on schema public to playground_mcp;

-- Table by table, as 013 argues: `alter default privileges` would make a table
-- readable by being created rather than by anyone deciding it should be, and a
-- table that reaches claude.ai should cost a line here and a line in
-- tests/test_mcp.py.
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
    -- Reddit usernames and third-party comment bodies. The one granted table
    -- with a person in it, granted for the third time and for the third reason:
    -- the social feed is most of what there is to reason across.
    social_item,
    -- so "how current is this data" is answerable without asking a human
    schema_migration
to playground_mcp;

-- Live transcripts, and this is the line to reverse if the answer changes.
--
-- 016 took skybird off the console in order to take it off Steven, who shares a
-- role with it; 017 gave it back to the console and withheld it from him. He is
-- built to start, pause and stop a capture and to be unable to read one back,
-- and that is a deliberate shape rather than a missing tool.
--
-- It does not carry over here, because it was an argument about *Steven*. The
-- reason this connector exists is to read the live feed and say what is in it,
-- and the transcript is the live feed; without this the feature is a screener
-- with a chat window. What it costs is honest and worth writing down: a
-- transcript is somebody's words, and granting this sends them to claude.ai
-- rather than keeping them on the VPS. That is a change in where the data goes,
-- not just in who may select it.
--
-- Unconditional, unlike 013's version of this block, because skybird is in this
-- repository now (014, 015) and every deployment has it.
grant usage on schema skybird to playground_mcp;
grant select on skybird.platform, skybird.stream_session,
                skybird.transcript_segment to playground_mcp;

-- Not granted, and no `usage` on their schemas, so they cannot even be named:
--   auth.app_user  who may sign in
--   auth.session   token_hash is authentication material
--   audit.event    identities, conversation transcripts, and spend
--   mcp.*          below — the credentials that reach this role
-- There is no `revoke` because nothing was ever granted.

-- The connector's own tables.
--
-- Its own schema rather than `public`, for the reason `auth` has one: these are
-- credentials, and a read-only role that can list every table in `public` should
-- not find the tokens that authorise it sitting among the price bars.
create schema if not exists mcp;

-- A client is whatever registered itself through RFC 7591 dynamic client
-- registration. Claude registers a new one per fresh connection, so this table
-- grows with reconnections rather than with users, and rows are disposable: a
-- client with no live token authorises nothing.
create table if not exists mcp.client (
    client_id     text primary key,
    client_name   text,
    -- Exact-match on redemption. An open redirect here would hand an
    -- authorization code to whoever asked for it.
    redirect_uris text[] not null,
    created_at    timestamptz not null default now()
);

-- One-time authorization codes. Short-lived by construction: the client
-- exchanges one within seconds of the consent screen, and anything older is a
-- replay.
create table if not exists mcp.authorization (
    code_hash      bytea primary key,
    client_id      text not null references mcp.client(client_id) on delete cascade,
    redirect_uri   text not null,
    -- PKCE. Stored as the challenge, verified against the verifier the client
    -- presents at the token endpoint; S256 only, which is what Claude sends and
    -- the only method the metadata advertises.
    code_challenge text not null,
    login          text not null,
    -- RFC 8707. Carried from the authorization request through to the token so
    -- the token can be audience-bound to this server and not another.
    resource       text not null,
    scope          text not null,
    expires_at     timestamptz not null,
    -- Claimed rather than deleted, so a replay can be told from a code that
    -- never existed. `where used_at is null` in the update is what makes
    -- redemption single-use under a threaded server.
    used_at        timestamptz
);
create index if not exists authorization_expires_idx on mcp.authorization (expires_at);

-- Access and refresh tokens, hashed under SESSION_SECRET the way `auth.session`
-- hashes a session, so the raw token exists once — in the response that issued
-- it — and is never written down. The hash is domain-separated by purpose, so a
-- value here is not also a valid browser cookie.
--
-- Rows are marked rather than deleted when a refresh is spent, and `family_id`
-- is why. Rotation on its own detects nothing: if a refresh token is stolen and
-- the thief redeems it first, the real client's next refresh simply fails and
-- looks like a bad network. Keeping the spent row means the *second*
-- presentation is recognisable as a replay, and the whole family can be revoked
-- — which is OAuth 2.1's actual remedy rather than half of it.
create table if not exists mcp.token (
    id                 bigint generated always as identity primary key,
    -- Every token descended from one consent shares this. Revoking is per
    -- family, because a compromised chain is compromised whichever end of it
    -- the attacker holds.
    family_id          text not null,
    token_hash         bytea not null unique,
    refresh_hash       bytea unique,
    client_id          text not null references mcp.client(client_id) on delete cascade,
    -- The GitHub login, re-checked against ALLOWED_GITHUB_LOGINS on every
    -- request rather than trusted from issue time, so removing somebody from
    -- the allow-list also disconnects their connector. A rename therefore fails
    -- closed, which is the right direction to fail.
    login              text not null,
    resource           text not null,
    scope              text not null,
    expires_at         timestamptz not null,
    refresh_expires_at timestamptz,
    created_at         timestamptz not null default now(),
    -- Set when the refresh is spent. A second attempt on the same value is a
    -- replay and revokes the family.
    used_at            timestamptz,
    revoked_at         timestamptz,
    last_used_at       timestamptz
);
create index if not exists token_expires_idx on mcp.token (expires_at);
create index if not exists token_login_idx on mcp.token (login);
create index if not exists token_family_idx on mcp.token (family_id);
