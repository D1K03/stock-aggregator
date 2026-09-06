"""Read-only SQL over the data this deployment allows.

One engine, two callers: the dashboard's editor and Steven's `sql` tool. Both
go through `run()`, so what is allowed cannot differ between them.

**The enforcement is a Postgres role, not a check in Python.** The application
connects as the cluster superuser, on which a SQL box would be `pg_read_file`
and `COPY FROM PROGRAM`. This connects as `playground`, which holds `select` on
a list of tables written down in `migrations/013_playground.sql` and nothing
else — not sign-in, not the audit trail, not whatever the next migration adds.
An application allowlist was rejected: it is a pattern match over a language
designed to be written many ways, and being wrong once on the other connection
is remote code execution.

Absence is a state, not a fault. With no `PLAYGROUND_DATABASE_URL` the role has
no password, `enabled()` is False, and the page says so rather than erroring.
"""

from screener.playground.catalog import Field, Table, catalog
from screener.playground.config import (
    DEFAULT_ROWS,
    MAX_CELL,
    MAX_ROWS,
    MAX_SQL,
    STATEMENT_TIMEOUT_MS,
    connecting_as,
    database_url,
    enabled,
)
from screener.playground.engine import (
    Column,
    Misconfigured,
    NotConfigured,
    QueryError,
    Result,
    Unavailable,
    run,
    select,
)
from screener.playground.provision import ensure_password

__all__ = [
    "DEFAULT_ROWS",
    "MAX_CELL",
    "MAX_ROWS",
    "MAX_SQL",
    "STATEMENT_TIMEOUT_MS",
    "Column",
    "Field",
    "Misconfigured",
    "NotConfigured",
    "QueryError",
    "Result",
    "Table",
    "Unavailable",
    "catalog",
    "connecting_as",
    "database_url",
    "enabled",
    "ensure_password",
    "run",
    "select",
]
