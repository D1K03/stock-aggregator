"""Where the playground connects, and every bound on what it will do."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from screener.config import env

# The dashboard's console. Steven's `sql` tool connects as `playground_bot`,
# which is this role minus the skybird schema -- see `017_a_role_for_steven.sql`.
# Neither is chosen here: both processes read `PLAYGROUND_DATABASE_URL` and are
# handed a different one, so the difference is a credential rather than a branch.
ROLE = "playground"
BOT_ROLE = "playground_bot"

# And claude.ai, over MCP -- `018_mcp.sql`. A third role rather than a borrowed
# one because it differs from both in the way that matters: the other two run
# inside this deployment, and what this one reads leaves the box.
MCP_ROLE = "playground_mcp"

# What the page asks for when it does not say, and the ceiling it is clamped to.
DEFAULT_ROWS = 200
MAX_ROWS = 500

# A pasted analytical query with a couple of CTEs, comfortably.
MAX_SQL = 4_000

# One `social_item.body` can be tens of thousands of characters, and a screen
# shows perhaps eighty of them.
MAX_CELL = 300

# Characters of serialised rows, counted as they are built, so the response size
# is a bound rather than an estimate. Without it `select * from price_daily` is
# this feature's memory risk on a threaded server.
MAX_PAYLOAD = 1_500_000

STATEMENT_TIMEOUT_MS = 10_000
IDLE_IN_TRANSACTION_TIMEOUT_MS = 30_000
LOCK_TIMEOUT_MS = 2_000
CONNECT_TIMEOUT = 3


# Which role the current call runs as, when it is not the deployment's default.
#
# 017 says the split between roles is "a credential, not a branch", and that is
# true where it can be: `api` and `bot` are separate containers holding separate
# passwords, so a bug in one cannot reach the other's tables.
#
# The MCP connector cannot have that, and it is worth being straight about why
# rather than pretending. It is served by the *same process* as the dashboard's
# SQL console — it has to be, because its OAuth consent screen needs the session
# cookie that process issues — so that container necessarily holds both
# passwords, and no arrangement of environment variables changes it. Here the
# separation really is a branch in Python.
#
# What the third role still buys, and the reason it exists: what the connector
# may read is decided in `018_mcp.sql` and enforced by Postgres, so widening it
# costs a migration and a line in a test rather than an argument. What it does
# not buy in this process is protection from a mistake in this repository.
_role: ContextVar[str | None] = ContextVar("playground_url", default=None)


@contextmanager
def connecting_as(url: str | None) -> Iterator[None]:
    """Run this block against a different read-only role.

    A ContextVar rather than an argument threaded through `run`, `select` and
    `catalog`, for the reason `bot.tools.acting` uses one: the caller that knows
    which role applies is several frames above the call that needs it, and a
    parameter would have to be passed by every layer in between and could be
    forgotten by any of them.
    """
    token = _role.set(url)
    try:
        yield
    finally:
        _role.reset(token)


def database_url() -> str | None:
    """The playground connection, or None when it is switched off.

    Its own reader rather than `screener.config.settings`, and None rather than
    a RuntimeError, because absence is a state and not a fault: it means no
    read-only role was given a password on this deployment. The right answer to
    a request is then "not configured here", not a 500.
    """
    return _role.get() or env.optional("PLAYGROUND_DATABASE_URL")


def enabled() -> bool:
    return database_url() is not None
