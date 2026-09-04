create table alert_rule (
    id             bigint generated always as identity primary key,
    code           text not null unique,
    name           text not null,
    enabled        boolean not null default true,
    condition_type text not null
                     check (condition_type in ('score_crossing', 'pillar_flip',
                                               'revision_cluster', 'insider_buying',
                                               'valuation_band')),
    params         jsonb not null,
    cooldown_days  smallint not null,
    min_coverage   numeric not null
);

-- The frozen record of what fired and why. JSONB is right here and nowhere else
-- in the schema: an immutable document, never queried across rows, answering
-- "why did this alert?" in a single row read.
--
-- The unique constraint makes the daily job idempotent. It also means a backfill
-- run would collide, which is the desired outcome — but it is a backstop, not the
-- mechanism: the alerting step must be skipped entirely when the run has
-- emits_alerts = false, not attempted and left to fail. Postgres cannot express
-- that as a cross-table check; the code owns it.
create table alert_event (
    id                     bigint generated always as identity primary key,
    alert_rule_id          bigint not null references alert_rule(id),
    security_id            bigint not null references security(id),
    as_of                  date not null,
    scoring_run_id         bigint not null references scoring_run(id),
    fired_at               timestamptz not null,
    blended_score          numeric not null,
    previous_blended_score numeric,
    pillar_scores          jsonb not null,
    raw_inputs             jsonb not null,
    driver                 text not null,
    event_flags            jsonb,
    delivery_status        text not null
                             check (delivery_status in ('pending', 'sent', 'failed')),
    delivery_attempts      smallint not null default 0,
    delivered_at           timestamptz,
    delivery_error         text,
    unique (alert_rule_id, security_id, as_of)
);
create index alert_event_security_idx on alert_event (security_id, as_of desc);
create index alert_event_rule_idx on alert_event (alert_rule_id);
create index alert_event_run_idx on alert_event (scoring_run_id);
create index alert_event_pending_idx
    on alert_event (fired_at) where delivery_status <> 'sent';

-- last_direction exists because a cooldown that suppresses the OPPOSITE crossing
-- is wrong: a score crossing up, then genuinely collapsing back, is exactly the
-- event worth hearing about. Cooldown suppresses repetition, not reversal.
create table alert_state (
    alert_rule_id    bigint not null references alert_rule(id),
    security_id      bigint not null references security(id),
    last_fired_at    timestamptz not null,
    last_fired_as_of date not null,
    cooldown_until   date not null,
    last_direction   smallint not null check (last_direction in (-1, 1)),
    primary key (alert_rule_id, security_id)
);
create index alert_state_security_idx on alert_state (security_id);
