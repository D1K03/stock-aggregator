create table ingest_run (
    id                   bigint generated always as identity primary key,
    source_id            smallint not null references data_source(id),
    endpoint             text not null,
    started_at           timestamptz not null,
    finished_at          timestamptz,
    status               text not null
                           check (status in ('running', 'ok', 'partial', 'failed')),
    securities_requested int,
    securities_ok        int,
    error                text
);
create index ingest_run_source_id_idx on ingest_run (source_id);

-- Deliberately NOT partitioned. A partitioned table's unique constraint must
-- include the partition key, which would force a composite primary key and
-- break the simple foreign keys the traceability chain depends on.
create table ingest_observation (
    id             bigint generated always as identity primary key,
    ingest_run_id  bigint not null references ingest_run(id),
    security_id    bigint not null references security(id),
    fetched_at     timestamptz not null,
    content_hash   bytea not null,
    blob_path      text not null,
    is_new_payload boolean not null,
    payload_bytes  int
);
create index ingest_observation_run_idx on ingest_observation (ingest_run_id);
create index ingest_observation_security_idx
    on ingest_observation (security_id, fetched_at desc);

create table fundamental_fact (
    id                    bigint generated always as identity primary key,
    security_id           bigint not null references security(id),
    metric_id             smallint not null references metric(id),
    period_end            date not null,
    period_type           text not null check (period_type in ('Q', 'A', 'TTM')),
    value                 numeric not null,
    currency              text check (length(currency) = 3),
    observed_at           timestamptz not null,
    ingest_observation_id bigint not null references ingest_observation(id),
    restates_id           bigint references fundamental_fact(id),
    unique (security_id, metric_id, period_end, period_type, observed_at)
);
create index fundamental_fact_pit_idx
    on fundamental_fact (security_id, metric_id, period_end, observed_at desc);
create index fundamental_fact_obs_idx on fundamental_fact (ingest_observation_id);
create index fundamental_fact_metric_idx on fundamental_fact (metric_id);
create index fundamental_fact_restates_idx on fundamental_fact (restates_id);

create table price_daily (
    security_id           bigint not null references security(id),
    trade_date            date not null,
    open                  numeric not null,
    high                  numeric not null,
    low                   numeric not null,
    close                 numeric not null,
    volume                bigint not null,
    observed_at           timestamptz not null,
    ingest_observation_id bigint not null references ingest_observation(id),
    primary key (security_id, trade_date)
) partition by range (trade_date);
create index price_daily_obs_idx on price_daily (ingest_observation_id);

create table price_daily_2026 partition of price_daily
    for values from ('2026-01-01') to ('2027-01-01');

create table corporate_action (
    id                    bigint generated always as identity primary key,
    security_id           bigint not null references security(id),
    effective_date        date not null,
    action_type           text not null
                            check (action_type in ('split', 'dividend', 'spinoff')),
    ratio                 numeric,
    amount                numeric,
    currency              text check (length(currency) = 3),
    observed_at           timestamptz not null,
    ingest_observation_id bigint not null references ingest_observation(id)
);
create index corporate_action_security_idx on corporate_action (security_id, effective_date);
create index corporate_action_obs_idx on corporate_action (ingest_observation_id);
