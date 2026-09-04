-- Sign-in state for the status service, in its own schema.
--
-- Deliberately not `public`. None of this is part of the screener's data model:
-- no scoring query joins to it, nothing here is a fact or a score, and the two
-- have entirely different lifetimes. Keeping them apart also means the test
-- suite's `drop schema public cascade` says exactly what it means.
create schema if not exists auth;

-- Keyed on the GitHub numeric id rather than the login, because a login can be
-- changed by its owner and later reissued to someone else. The login is stored
-- alongside for display and for the allow-list check at sign-in.
create table auth.app_user (
    id           bigint generated always as identity primary key,
    github_id    bigint not null unique,
    login        text not null,
    created_at   timestamptz not null default now(),
    last_seen_at timestamptz not null default now()
);

-- The raw session token exists only in the user's cookie. What is stored is its
-- HMAC under the session secret, so a database read on its own does not yield a
-- usable session, and rotating the secret invalidates every session at once.
create table auth.session (
    id         bigint generated always as identity primary key,
    user_id    bigint not null references auth.app_user(id) on delete cascade,
    token_hash bytea not null unique,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    user_agent text
);
create index session_user_idx on auth.session (user_id);
-- Expired rows are deleted on use rather than by a scheduled job; this index is
-- what makes that sweep cheap.
create index session_expires_idx on auth.session (expires_at);
