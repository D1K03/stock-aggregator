"""Chart PNGs for Discord, rasterised by the web service.

Discord shows an attached image but will not render an SVG, and nothing in this
process can draw one — the project's runtime dependencies are a HTTP client and
a database driver, and adding a drawing library would mean a second
implementation of a chart that already exists. Two implementations of the same
picture is how the version in Discord ends up subtly unlike the version on
screen, which is worse than having no chart in Discord at all: it makes you
doubt which one is right.

So the picture stays in one place. The dashboard builds the chart as an SVG
string; `/api/render` in the same web service rasterises that string with
resvg, in the same typeface, and hands back a PNG. This module is the client
for it.

Nothing here is on the public internet. Caddy routes every `/api/*` path to the
status service, so that handler is unreachable from outside; this reaches it
directly on the compose network.
"""

import logging
import os

import httpx

from screener.bot.tools import Chart

logger = logging.getLogger(__name__)

# Where the web container answers inside the compose network. Overridable
# because "web" is a service name, which only means anything in that network.
DEFAULT_RENDERER = "http://web:3000/api/render"

# Rasterising a chart is a few milliseconds of work. Anything approaching this
# means the web service is unwell, and a Discord reply that has already been
# composed should not wait on it.
TIMEOUT_SECONDS = 6.0

# Discord accepts ten attachments. Far fewer are ever wanted, and each is a
# round trip and an upload, so a model that asked for a chart every round does
# not turn one mention into ten.
MAX_CHARTS = 3


def renderer_url() -> str:
    return os.environ.get("CHART_RENDERER_URL", DEFAULT_RENDERER)


def chart_png(chart: Chart, *, client: httpx.Client | None = None) -> bytes | None:
    """One chart as PNG bytes, or `None` if it could not be drawn.

    Never raises. A missing picture costs the reader a picture; an exception
    here would cost them the answer, which they can read perfectly well without
    it.
    """
    owned = client is None
    client = client or httpx.Client(timeout=TIMEOUT_SECONDS)
    try:
        response = client.post(renderer_url(), json=chart.payload())
        response.raise_for_status()
        if not response.content.startswith(b"\x89PNG"):
            logger.warning("chart renderer returned %s, not a PNG", response.headers.get("content-type"))
            return None
        return response.content
    except Exception as exc:
        logger.warning("could not rasterise %s: %s", chart.ticker, type(exc).__name__)
        return None
    finally:
        if owned:
            client.close()


def chart_pngs(charts: tuple[Chart, ...]) -> list[tuple[str, bytes]]:
    """`(filename, png)` for each chart that rendered, in order.

    One client for the batch, because a reply with two charts is two requests
    to the same host and a fresh connection for each is pure latency.
    """
    if not charts:
        return []
    drawn: list[tuple[str, bytes]] = []
    with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
        for index, chart in enumerate(charts[:MAX_CHARTS]):
            png = chart_png(chart, client=client)
            if png is not None:
                # Named for the ticker, so a saved image says what it is.
                suffix = f"-{index}" if index else ""
                drawn.append((f"{chart.ticker.lower()}{suffix}.png", png))
    return drawn
