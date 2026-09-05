"""The commands themselves.

Module-level `app_commands.Command` objects rather than a bespoke registry:
`COMMANDS` is the single source of truth a future `/help` can read, and because
a `Command` exposes `.name`, `.description` and `.callback`, both the metadata
and the body are assertable without constructing a `Client` or a tree.
"""

import asyncio
import time

from discord import Interaction, app_commands

from screener.audit import record
from screener.bot.checks import permitted
from screener.provenance import git_sha


@app_commands.command(name="ping", description="Check the bot is alive and say which build it is")
@permitted()
async def ping(interaction: Interaction) -> None:
    """Round-trip latency and the running commit.

    The build SHA is what makes this more than a toy: it is read through
    `screener.provenance.git_sha()`, the same source `/status` uses, so the
    reply answers "is the bot I am talking to the build I just deployed" rather
    than only "is something listening".
    """
    # Measured around the reply rather than reported from the gateway
    # heartbeat: heartbeat latency says the socket is healthy, which is not the
    # same question as whether a command round-trips.
    started = time.perf_counter()
    await interaction.response.send_message(
        f"pong · build `{git_sha()[:12]}`", ephemeral=True
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    await interaction.edit_original_response(
        content=f"pong · {elapsed_ms:.0f}ms · build `{git_sha()[:12]}`"
    )
    await asyncio.to_thread(
        record,
        kind="command",
        operation="ping",
        actor=str(interaction.user.id),
        actor_kind="discord",
        duration_ms=int(elapsed_ms),
    )


COMMANDS: tuple[app_commands.Command, ...] = (ping,)
