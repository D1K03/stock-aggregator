create table weight_version (
    id         bigint generated always as identity primary key,
    code       text not null unique,
    note       text,
    created_at timestamptz not null default now()
);

-- Weights are stored raw and normalised at read time by dividing by the version's
-- sum. "Weights sum to 1" cannot be expressed cleanly as a table constraint and
-- would eventually be violated; normalising on use makes it impossible to lie.
create table pillar_weight (
    weight_version_id bigint not null references weight_version(id),
    pillar_id         smallint not null references pillar(id),
    weight            numeric not null check (weight >= 0),
    primary key (weight_version_id, pillar_id)
);
create index pillar_weight_pillar_idx on pillar_weight (pillar_id);

create table scoring_logic_version (
    id            smallint generated always as identity primary key,
    description   text not null,
    introduced_at timestamptz not null default now()
);

create table scoring_run (
    id                bigint generated always as identity primary key,
    as_of_range       daterange not null,
    -- Visible facts when scoring date D are those with
    -- observed_at <= D + cutoff_offset. An offset rather than a timestamp so
    -- live and backfill runs evaluate the identical expression.
    cutoff_offset     interval not null,
    logic_version_id  smallint not null references scoring_logic_version(id),
    weight_version_id bigint not null references weight_version(id),
    status            text not null check (status in ('live', 'backfill', 'experiment')),
    emits_alerts      boolean not null,
    git_sha           text not null,
    config_hash       bytea not null,
    started_at        timestamptz not null,
    finished_at       timestamptz,
    outcome           text not null check (outcome in ('running', 'ok', 'failed')),
    supersedes_run_id bigint references scoring_run(id),
    note              text,
    constraint scoring_run_one_live_per_date
        exclude using gist (as_of_range with &&) where (status = 'live')
);
create index scoring_run_logic_idx on scoring_run (logic_version_id);
create index scoring_run_weight_idx on scoring_run (weight_version_id);
create index scoring_run_supersedes_idx on scoring_run (supersedes_run_id);
