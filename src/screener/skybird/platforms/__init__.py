"""Which platform a URL belongs to, and how to embed its player.

Two small things, deliberately, and neither of them touches the network.
Pulling audio out of a live stream is yt-dlp's job and it already knows how to
do it for forty sites, so a module here only has to recognise a URL and build an
embed. That is what makes the third platform cheap: one module, one entry in
`PLATFORMS`, one row in `skybird.platform`, and no capture code at all.

The embed URL is built here, in Python, rather than in the browser. Twitch
refuses to play unless `parent` names the host serving the page, which is
configuration, and the `web` container deliberately holds none.
"""

from collections.abc import Sequence

from screener.skybird.platforms import twitch, youtube
from screener.skybird.platforms.base import (
    Matcher,
    Platform,
    StreamRef,
    UnsupportedPlatform,
    VideoEmbedder,
    with_scheme,
)

__all__ = [
    "PLATFORMS",
    "Matcher",
    "Platform",
    "StreamRef",
    "UnsupportedPlatform",
    "VideoEmbedder",
    "find",
    "resolve",
    "supported",
]

# The extension point. Order is the tie-breaker, so an adapter added later
# cannot quietly take a URL an earlier one was already matching.
PLATFORMS: tuple[Platform, ...] = (youtube.PLATFORM, twitch.PLATFORM)


def resolve(url: str, *, parents: Sequence[str] = ()) -> StreamRef:
    """The first adapter that recognises `url`, or `UnsupportedPlatform`."""
    candidate = with_scheme(url)
    if not candidate:
        raise UnsupportedPlatform("no URL given")
    for platform in PLATFORMS:
        ref = platform.match(candidate, parents)
        if ref is not None:
            return ref
    raise UnsupportedPlatform(
        f"{url.strip()!r} is not a stream I can capture — supported: {supported()}"
    )


def find(name: str) -> Platform | None:
    """The adapter behind a stored `platform` code, or None if it has gone."""
    for platform in PLATFORMS:
        if platform.name == name:
            return platform
    return None


def supported() -> str:
    """The platform names, for an error message a person can act on."""
    return ", ".join(platform.display_name for platform in PLATFORMS)
