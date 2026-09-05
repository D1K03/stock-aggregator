"""Where the playground connects, and every bound on what it will do."""

from screener.config import env

# The dashboard's console. Steven's `sql` tool connects as `playground_bot`,
# which is this role minus the skybird schema -- see `017_a_role_for_steven.sql`.
# Neither is chosen here: both processes read `PLAYGROUND_DATABASE_URL` and are
# handed a different one, so the difference is a credential rather than a branch.
ROLE = "playground"
BOT_ROLE = "playground_bot"

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


def database_url() -> str | None:
    """The playground connection, or None when it is switched off.

    Its own reader rather than `screener.config.settings`, and None rather than
    a RuntimeError, because absence is a state and not a fault: it means no
    read-only role was given a password on this deployment. The right answer to
    a request is then "not configured here", not a 500.
    """
    return env.optional("PLAYGROUND_DATABASE_URL")


def enabled() -> bool:
    return database_url() is not None
