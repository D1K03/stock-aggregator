# Playground

*Status: implemented. Written 2026-09-05.*

Read-only SQL over the data this deployment allows: a `/playground` page with a
tree of tables and an editor, and a `sql` tool for Steven. One engine behind
both, so what is permitted cannot differ between them.

## Why a second Postgres role

The application connects as `screener`, which **is the cluster superuser**.
Verified rather than assumed: `pg_read_file('/etc/hostname')` returns on that
connection. A SQL box wired to it would be arbitrary file read and, through
`COPY FROM PROGRAM`, remote code execution, reachable by anyone holding a
session cookie.

**D1 — The role is the enforcement, not a check in Python.** `playground` holds
`select` on the tables listed in `migrations/013_playground.sql` and nothing
else. A role never granted `auth.session` cannot read it however the query is
spelled — through a view, a CTE, a function, or a cast nobody thought of.

*Rejected:* an application allowlist, regex or `sqlparse` validator. It is a
pattern match over a language designed to be written many ways, on a connection
where being wrong once is RCE. Having both would mean the weaker one gets
trusted.

Proven against a live database:

```
refused  auth.session / auth.app_user  [42501] permission denied for schema auth
refused  audit.event                   [42501] permission denied for schema audit
refused  pg_read_file                  [42501] permission denied for function
refused  insert / delete / drop        [25006] read-only transaction
refused  price_daily_2026 direct       [42501] permission denied
ALLOWED  price_daily through its parent, security, social_item
superuser: False    readable relations: 28
```

**D2 — Explicit grants, not `alter default privileges`.** Under default
privileges a table becomes readable by being *created* rather than by anyone
deciding it should be. Exposure costs a line in the migration and a line in
`tests/test_playground.py`, and a test asserts every table is either granted or
explicitly denied, so a future migration cannot quietly add one to a SQL box.

**D3 — Partitioned parents only.** 16 of 44 tables in `public` are `_YYYY`
partitions created at boot, with more every January, so a hardcoded list would
go stale annually. A partition inherits no ACL, and reading through the parent
needs no grant on it — so `price_daily` is readable and appears once, while
naming a partition directly is a permission error.

**D4 — `skybird` conditionally.** The livestream schema is created outside this
repository and exists on a development machine but not on the VPS. An
unconditional grant would fail the migration there.

## The finding that decides the engine

`psycopg 3.3.5` uses the **simple query protocol when a query has no
parameters** (`_cursor_base.py:456-459`, whose comment says "it can execute more
than one statement in a single query"). A playground query never has parameters.
Demonstrated:

```
plain execute:        ACCEPTED   "select 1; select 2"   ← both ran
server-side cursor:   REFUSED [42601] cannot insert multiple commands
a write through it:   REFUSED [42601] syntax error at or near "insert"
```

**D5 — Every query goes through a named cursor.** It forces the extended
protocol, so no second statement; it wraps the text in `DECLARE ... CURSOR FOR`,
whose grammar accepts only a SELECT or VALUES and rejects data-modifying CTEs by
name; and it fetches in bounded batches. The cost is that `EXPLAIN` does not
work, which is accepted rather than worked around — a second statement path
would hand the multi-statement hole back to buy a query plan for a two-person
tool.

**D6 — One `cast(LiteralString, ...)`, argued for rather than assumed.**
`screener.migrate` holds the only other one, on a versioned file. This one is
not trusted content and the comment does not pretend it is: it records that the
module has stopped relying on the type system and put the enforcement where the
type system cannot see it. *Rejected:* `cur.execute(text.encode())`, which
type-checks with no cast at all because `bytes` is in psycopg's `Query` union —
it would pass pyright and would not be found by `grep -rn "cast(LiteralString"`,
which is the one command that locates every such place.

**D7 — Postgres's message is shown, on query errors only.** Split on SQLSTATE
rather than exception class, because `QueryCanceled` (a statement timeout, very
much about the query) subclasses `OperationalError`. Infrastructure classes are
named by type only, keeping `checks.py`'s reasoning that psycopg embeds host and
username in connection errors. Positions are corrected for both the prepended
newline and the `DECLARE` prefix so the caret lands under the character typed.

**D8 — Rows reach the surface, not the model.** The chart tool's bargain, with
one refinement: six cells or fewer go inline, because "how many securities are
there" wants the number in the sentence and a one-cell artifact is silly.

**D9 — The prompt budget rises from 1800 to 2200.** Measured 2,159 with 41
spare, the same tightness as before. The `sql` tool spec is 240 characters and
the prompt line 155. There is no companion `tables` tool: unknown names come
back with the list, which costs nothing per message.

## What is not built

`EXPLAIN`. CSV export. A query history table — the audit trail already records
every query Steven runs, with its text. Autocomplete, the one feature that would
need CodeMirror. A `PLAYGROUND_ENABLED` flag: the presence of the password is
the switch. A redacting view over `social_item`, which would drop `author` while
leaving usernames quoted inside `body`.

## Risks

`PLAYGROUND_DATABASE_URL` pointed at the application's own connection is the one
hole the design otherwise has, and every test would still pass because the tests
build their own URL. The engine checks `usesuper` on connect and refuses; the
self-test reports it.

`social_item` is granted and holds Reddit usernames and third-party comment
bodies. Public data, and the one granted table with a person in it.
