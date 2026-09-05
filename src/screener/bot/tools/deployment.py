"""Tools that answer questions about this deployment."""

from screener.bot.tools.registry import tool
from screener.health import checks
from screener.provenance import git_sha


@tool("status", "Build, database and migration state of this deployment.")
def status() -> str:
    """Compact key=value rather than JSON or prose.

    The model reads this perfectly well and it costs roughly a third of the
    tokens of the equivalent object, on every message that asks for it.
    """
    reason, migrations = checks.database()
    return f"build={git_sha()[:12]} db={reason} migrations={migrations}"
