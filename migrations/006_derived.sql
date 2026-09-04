-- Yearly partitions, not monthly. Partitioning at all is non-negotiable
-- (retrofitting is a full rewrite), but at the starting universe size monthly
-- would mean ~100 near-empty partitions. Granularity is never switched: once the
-- universe passes ~1,000 tickers, start creating monthly partitions from the next
-- year boundary and leave earlier years coarse. Boundaries align, so nothing is
-- rebuilt. There is deliberately no default partition — attaching a new partition
-- alongside one requires a full scan.
create table metric_daily (
    as_of               date not null,
    scoring_run_id      bigint not null references scoring_run(id),
    security_id         bigint not null references security(id),
    metric_id           smallint not null references metric(id),
    raw_value           numeric not null,
    percentile          numeric not null check (percentile between 0 and 100),
    peer_group_id       bigint not null references peer_group(id),
    peer_count          int not null,
    fallback_level      smallint not null check (fallback_level in (0, 1, 2)),
    fundamental_fact_id bigint references fundamental_fact(id),
    period_end          date,
    primary key (as_of, scoring_run_id, security_id, metric_id)
) partition by range (as_of);

create index metric_daily_history_idx on metric_daily (security_id, metric_id, as_of desc);
create index metric_daily_run_idx on metric_daily (scoring_run_id);
create index metric_daily_fact_idx on metric_daily (fundamental_fact_id);
create index metric_daily_peer_group_idx on metric_daily (peer_group_id);
create index metric_daily_metric_idx on metric_daily (metric_id);

create table metric_daily_2026 partition of metric_daily
    for values from ('2026-01-01') to ('2027-01-01');

create table pillar_score_daily (
    as_of          date not null,
    scoring_run_id bigint not null references scoring_run(id),
    security_id    bigint not null references security(id),
    pillar_id      smallint not null references pillar(id),
    score          numeric not null,
    metric_count   smallint not null,
    coverage       numeric not null check (coverage between 0 and 1),
    primary key (as_of, scoring_run_id, security_id, pillar_id)
) partition by range (as_of);
create index pillar_score_daily_run_idx on pillar_score_daily (scoring_run_id);
create index pillar_score_daily_security_idx on pillar_score_daily (security_id, as_of desc);
create index pillar_score_daily_pillar_idx on pillar_score_daily (pillar_id);

create table pillar_score_daily_2026 partition of pillar_score_daily
    for values from ('2026-01-01') to ('2027-01-01');

-- blended_score is a materialised derivation, not ground truth: stored so the
-- nightly crossing diff need not aggregate pillar rows. The run carries the
-- weight version, so there is deliberately no weight_version_id column here.
create table snapshot_daily (
    as_of                date not null,
    scoring_run_id       bigint not null references scoring_run(id),
    security_id          bigint not null references security(id),
    blended_score        numeric not null,
    pillar_agreement     smallint not null,
    min_coverage         numeric not null,
    worst_fallback_level smallint not null,
    primary key (as_of, scoring_run_id, security_id)
) partition by range (as_of);
create index snapshot_daily_run_idx on snapshot_daily (scoring_run_id);
create index snapshot_daily_security_idx on snapshot_daily (security_id, as_of desc);

create table snapshot_daily_2026 partition of snapshot_daily
    for values from ('2026-01-01') to ('2027-01-01');

create table event_flag_daily (
    as_of          date not null,
    scoring_run_id bigint not null references scoring_run(id),
    security_id    bigint not null references security(id),
    flag_code      text not null,
    severity       smallint not null,
    detail         jsonb,
    primary key (as_of, scoring_run_id, security_id, flag_code)
) partition by range (as_of);
create index event_flag_daily_run_idx on event_flag_daily (scoring_run_id);
create index event_flag_daily_security_idx on event_flag_daily (security_id, as_of desc);
create index event_flag_daily_code_idx on event_flag_daily (flag_code, as_of desc);

create table event_flag_daily_2026 partition of event_flag_daily
    for values from ('2026-01-01') to ('2027-01-01');

-- A cache, not a source of truth: the exact distribution is recoverable from
-- metric_daily. deciles[1] and deciles[11] are the min and max.
create table peer_group_stat (
    as_of          date not null,
    scoring_run_id bigint not null references scoring_run(id),
    peer_group_id  bigint not null references peer_group(id),
    metric_id      smallint not null references metric(id),
    member_count   int not null,
    deciles        numeric[] not null check (array_length(deciles, 1) = 11),
    primary key (as_of, scoring_run_id, peer_group_id, metric_id)
);
create index peer_group_stat_run_idx on peer_group_stat (scoring_run_id);
create index peer_group_stat_group_idx on peer_group_stat (peer_group_id);
create index peer_group_stat_metric_idx on peer_group_stat (metric_id);
