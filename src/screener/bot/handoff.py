"""Carrying a dashboard conversation over to Discord.

A direct message, sent over Discord's REST API with the bot token. No gateway
connection is involved: this runs inside the status service, which is a
different process from the bot, and opening a second gateway session on one
token would have both of them answering every command.
"""

import logging

import httpx

from screener.bot.config import BotConfig

logger = logging.getLogger(__name__)

API = "https://discord.com/api/v10"
TIMEOUT = 15.0

# Discord asks bots to identify themselves. The fetch layer's browser
# fingerprint would be the wrong claim to make here.
HEADERS_UA = "DiscordBot (https://github.com/D1K03/stock-aggregator, 0.1.0)"


class HandoffError(RuntimeError):
    """The message could not be delivered."""


def send_dm(
    *,
    login: str,
    text: str,
    config: BotConfig | None = None,
    transport: httpx.BaseTransport | None = None,
) -> int:
    """Direct message the Discord account belonging to `login`.

    Returns the Discord user id it reached, so a caller can say who was
    messaged rather than only that something happened.
    """
    config = config or BotConfig.from_env()
    if config.token is None:
        raise HandoffError("DISCORD_BOT_TOKEN is not set")

    user_id = config.discord_id_for(login)
    if user_id is None:
        raise HandoffError(f"no Discord account is mapped for {login}")

    headers = {
        "Authorization": f"Bot {config.token}",
        "User-Agent": HEADERS_UA,
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=TIMEOUT, transport=transport) as client:
            # A DM channel has to exist before anything can be posted to it.
            # Discord returns the existing one rather than creating duplicates,
            # so this is safe to call every time.
            opened = client.post(
                f"{API}/users/@me/channels",
                headers=headers,
                json={"recipient_id": str(user_id)},
            )
            if opened.status_code >= 400:
                raise HandoffError(
                    f"could not open a DM channel: HTTP {opened.status_code}"
                )
            channel_id = opened.json().get("id")
            if not channel_id:
                raise HandoffError("Discord returned no channel id")

            sent = client.post(
                f"{API}/channels/{channel_id}/messages",
                headers=headers,
                json={"content": text[:2000]},
            )
            if sent.status_code >= 400:
                # 403 here almost always means the person has direct messages
                # from server members switched off, which is worth saying
                # plainly rather than reporting as a generic failure.
                if sent.status_code == 403:
                    raise HandoffError(
                        "Discord refused the message. Direct messages from "
                        "server members may be switched off."
                    )
                raise HandoffError(f"could not send the message: HTTP {sent.status_code}")
    except httpx.HTTPError as exc:
        raise HandoffError(f"Discord request failed: {exc}") from exc

    logger.info("handed off to discord user %s for %s", user_id, login)
    return user_id
