"""The authorization gate every command wears."""

import logging

from discord import Interaction, app_commands

from screener.bot.config import BotConfig

logger = logging.getLogger(__name__)


class NotPermitted(app_commands.CheckFailure):
    """The invoking account is not in the allow-list."""


def permitted() -> "app_commands.check":  # type: ignore[valid-type]
    """A decorator, rather than a call each command has to remember to make.

    Read fresh from the environment per invocation for the same reason
    `settings()` is uncached: secrets are loaded into `os.environ` at startup,
    and a value captured at import time would be whatever existed before that.
    """

    def predicate(interaction: Interaction) -> bool:
        config = BotConfig.from_env()
        if config.permits(interaction.user.id):
            return True
        # The id, not the name: names are chosen by their owner and change.
        logger.warning("refused a command from discord user %s", interaction.user.id)
        raise NotPermitted("not permitted")

    return app_commands.check(predicate)
