"""The bot process: `python -m screener.bot`."""

import asyncio
import logging
import signal
import sys

import discord

from screener.bot.config import BotConfig
from screener.secrets import SecretsError, load_into_environ

logger = logging.getLogger(__name__)


async def _serve(config: BotConfig) -> None:
    """Connect, and stay connected until asked to stop.

    Deliberately not `Client.run()`. That helper catches `KeyboardInterrupt`
    and nothing else, so in a container — where the stop signal is SIGTERM —
    the process would be killed outright and the gateway session left for
    Discord to time out, showing the bot online for minutes after it died.
    Handling SIGTERM here closes the session first.
    """
    from screener.bot.client import ScreenerBot

    assert config.token is not None  # implied by config.enabled

    client = ScreenerBot(config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # close() makes start() return, which unwinds the context manager and
        # lets the process exit on its own rather than being torn down.
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(client.close()))

    async with client:
        await client.start(config.token)


class _NoVoiceWarning(logging.Filter):
    """Drops discord.py's startup complaint about voice support.

    It warns that voice will not work because PyNaCl is missing. That is about
    streaming audio into a voice *channel*, which this bot does not do and has
    no plans to. It does now answer voice messages, but a voice message is an
    attachment fetched over HTTPS and transcribed in another container, which
    needs none of what the warning is about — so the line is still misleading
    rather than useful, and `docker logs bot` should open on something that
    matters.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "voice will NOT be supported" not in record.getMessage()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("discord.client").addFilter(_NoVoiceWarning())

    try:
        load_into_environ()
    except SecretsError as exc:
        logger.error("%s", exc)
        return 1

    config = BotConfig.from_env()
    if not config.enabled:
        logger.error("DISCORD_BOT_TOKEN is not set; nothing to run")
        return 1
    if not config.allowed_user_ids:
        # Not fatal: the bot connects and refuses everyone, which is a safe
        # state and a legible one. Silently accepting everybody would not be.
        logger.warning(
            "ALLOWED_DISCORD_USER_IDS is empty; every command will be refused"
        )

    try:
        asyncio.run(_serve(config))
    except discord.LoginFailure:
        # Restarting will not fix a rejected token, and `restart:
        # unless-stopped` would turn that into a loop hammering Discord's login
        # endpoint. Exit non-zero and leave it to a person.
        logger.error("Discord rejected the token; not retrying")
        return 1
    except (discord.Forbidden, discord.NotFound):
        # Commands are synced inside `setup_hook`, which runs during login and
        # before the websocket opens, so a wrong guild id kills the process
        # here rather than producing a bot that is online with no commands.
        # That is the better failure, but only if it says what went wrong.
        logger.error(
            "could not register commands in guild %s. Is the bot in that "
            "server, and was it invited with the applications.commands scope?",
            config.guild_id,
        )
        return 1
    logger.info("bot stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
