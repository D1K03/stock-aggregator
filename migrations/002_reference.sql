create table pillar (
    id   smallint generated always as identity primary key,
    code text not null unique,
    name text not null
);

create table metric (
    id               smallint generated always as identity primary key,
    code             text not null unique,
    name             text not null,
    pillar_id        smallint not null references pillar(id),
    unit             text not null,
    higher_is_better boolean not null,
    cadence          text not null check (cadence in ('daily', 'quarterly', 'event')),
    is_active        boolean not null default true
);
create index metric_pillar_id_idx on metric (pillar_id);

create table sector_scheme (
    id   smallint generated always as identity primary key,
    code text not null unique,
    name text not null
);

create table sector_node (
    id        bigint generated always as identity primary key,
    scheme_id smallint not null references sector_scheme(id),
    parent_id bigint references sector_node(id),
    level     smallint not null check (level in (1, 2)),
    code      text not null,
    name      text not null,
    unique (scheme_id, code)
);
create index sector_node_scheme_id_idx on sector_node (scheme_id);
create index sector_node_parent_id_idx on sector_node (parent_id);

create table data_source (
    id   smallint generated always as identity primary key,
    code text not null unique,
    name text not null
);
