create table security (
    id             bigint generated always as identity primary key,
    name           text not null,
    mic            text not null,
    currency       text not null check (length(currency) = 3),
    country        text not null check (length(country) = 2),
    cik            text,
    figi           text,
    primary_symbol text not null,
    is_active      boolean not null default true,
    first_seen     date not null,
    last_seen      date,
    created_at     timestamptz not null default now()
);

create table security_symbol (
    id          bigint generated always as identity primary key,
    security_id bigint not null references security(id),
    symbol      text not null,
    mic         text not null,
    valid_from  date not null,
    valid_to    date,
    source      text not null,
    constraint security_symbol_no_overlap
        exclude using gist (
            security_id with =,
            daterange(valid_from, valid_to, '[)') with &&
        )
);
create index security_symbol_security_id_idx on security_symbol (security_id);
create unique index security_symbol_current_uq
    on security_symbol (symbol, mic) where valid_to is null;

create table security_sector (
    id             bigint generated always as identity primary key,
    security_id    bigint not null references security(id),
    sector_node_id bigint not null references sector_node(id),
    valid_from     date not null,
    valid_to       date,
    source         text not null,
    constraint security_sector_no_overlap
        exclude using gist (
            security_id with =,
            daterange(valid_from, valid_to, '[)') with &&
        )
);
create index security_sector_security_id_idx on security_sector (security_id);
create index security_sector_node_idx on security_sector (sector_node_id);
create unique index security_sector_current_uq
    on security_sector (security_id) where valid_to is null;

create table peer_group (
    id             bigint generated always as identity primary key,
    scheme_id      smallint not null references sector_scheme(id),
    sector_node_id bigint references sector_node(id),
    level          smallint not null check (level in (0, 1, 2)),
    code           text not null,
    unique (scheme_id, code)
);
create index peer_group_sector_node_idx on peer_group (sector_node_id);
