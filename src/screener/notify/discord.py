"""Delivery to a Discord webhook."""

import logging
import time
from typing import Any

import httpx

from screener.notify.base import Alert, ChannelError
from screener.notify.config import DiscordConfig

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15.0

# Discord's own ceiling on an embed description. Truncating here keeps a long
# alert from being rejected wholesale, which would lose the part that mattered.
MAX_DESCRIPTION = 4096

COLOURS = {"info": 0x5865F2, "warning": 0xE67E22}


class DiscordWebhook:
    """A webhook POST. No bot, no gateway connection, no OAuth.

    A gateway bot would mean an always-connected process and a token with real
    scopes, for a payload that is one message a day. If one is ever wanted —
    slash commands, reactions on an alert — it arrives as another
    `NotificationChannel` and nothing above this changes.
    """

    name = "discord"

    def __init__(
        self,
        url: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        resolved = url if url is not None else DiscordConfig.from_env().webhook_url
        if resolved is None:
            raise ChannelError("DISCORD_WEBHOOK_URL is not set")
        self._url = resolved
        self._timeout = timeout
        # Test seam, as in screener.fetch. Production never passes one.
        self._transport = transport

    def _payload(self, alert: Alert) -> dict[str, Any]:
        embed: dict[str, Any] = {
            "title": alert.title[:256],
            "color": COLOURS.get(alert.severity, COLOURS["info"]),
        }
        if alert.body:
            embed["description"] = alert.body[:MAX_DESCRIPTION]
        if alert.url:
            embed["url"] = alert.url
        if alert.fields:
            embed["fields"] = [
                {"name": f.name[:256], "value": f.value[:1024], "inline": f.inline}
                for f in alert.fields[:25]
            ]
        return {"embeds": [embed]}

    def send(self, alert: Alert) -> None:
        """Deliver `alert`, or raise `ChannelError`.

        `wait=true` makes Discord validate and persist the message before
        answering, so a malformed payload comes back as a 400 rather than a
        202 followed by silence. Without it a delivery can be reported as
        successful and never appear in the channel.
        """
        try:
            with httpx.Client(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = client.post(
                    self._url, params={"wait": "true"}, json=self._payload(alert)
                )
                if response.status_code == 429:
                    # One retry, honouring the server's own figure. Discord
                    # rate-limits per webhook, so the wait is short and the
                    # alternative — dropping the alert — is the failure this
                    # whole layer exists to avoid.
                    delay = _retry_after(response)
                    logger.warning("discord rate-limited; retrying in %.1fs", delay)
                    time.sleep(delay)
                    response = client.post(
                        self._url, params={"wait": "true"}, json=self._payload(alert)
                    )
                if response.status_code >= 400:
                    raise ChannelError(
                        f"discord webhook returned HTTP {response.status_code}"
                    )
        except httpx.HTTPError as exc:
            raise ChannelError(f"discord webhook request failed: {exc}") from exc


def _retry_after(response: httpx.Response) -> float:
    """Seconds to wait, from the response, clamped to something sane.

    Discord sends `retry_after` in a JSON body and `Retry-After` as a header;
    which one arrives depends on whether the limit was per-route or global.
    """
    try:
        body = response.json()
        if isinstance(body, dict) and "retry_after" in body:
            return min(float(body["retry_after"]), 30.0)
    except ValueError:
        pass
    header = response.headers.get("Retry-After")
    if header:
        try:
            return min(float(header), 30.0)
        except ValueError:
            pass
    return 1.0
